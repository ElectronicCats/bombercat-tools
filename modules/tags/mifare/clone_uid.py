#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

# `mifare clone-uid` — Fase 6 of PLAN_Mejoras_MIFARE.md: verify, on the card
# currently on the reader, whether THIS hardware (PN7150 over the MifareClassic
# firmware) can rewrite block 0 — the UID. It classifies the card's magic type
# and, when asked, writes a source UID and reads it back to confirm a real 1:1
# clone. See docs/MIFARE_IMPROVEMENTS_Phase6.md for the full investigation.
#
# The honest frontier this command draws (and never crosses):
#
#   - gen2 / CUID / "direct-write" magic: block 0 is writable after a NORMAL
#     Crypto-1 auth + a standard `A0` write — no backdoor. The PN7150 reader
#     stack does exactly this (see MifareCommands.cpp::mifareWriteBlock), so the
#     UID CAN be rewritten and this command verifies it end to end.
#
#   - gen1a "backdoor" magic: block 0 is only unlocked by a 7-bit short frame
#     (`0x40`) with no CRC, sent right after HALT, then `0x43`. The PN7150's NCI
#     reader API only carries whole-byte DATA packets over an already-activated
#     tag (standard framing: 8-bit bytes + CRC_A + parity); it exposes no knob
#     for a sub-byte frame or CRC/parity suppression. So gen1a's unlock is NOT
#     reachable through this stack — this command never pretends otherwise; it
#     reports such a card as "block 0 not writable → gen1a backdoor required,
#     out of reach on this hardware".

import json
from typing import Dict, Optional, Tuple

import click

from ...utils.cli_options import device_options
from ...utils.detection_cli import print_field as _print_field, verbosity as _verbosity
from ...utils.output import (
    console,
    make_tracer,
    print_error,
    print_info,
    print_success,
    print_warning,
)
from .block0 import parse_block0
from .common import (
    _MIFARE_BLOCK_HEX_LEN,
    _MIFARE_KEY_HEX_LEN,
    _load_sector_key_pair,
    _mifare_validate_hex,
)
from .restore import _load_restore_dump
from .session import (
    _MIFARE_KEY_TYPE_OPTION,
    _MIFARE_TIMEOUT_OPTION,
    _auth_sector,
    _mifare_session,
)

# The default transport key A most blank magic cards (and factory sector 0) ship
# with. Used to authenticate sector 0 when no --key/--keys-file is given.
_DEFAULT_KEY = "FFFFFFFFFFFF"

# Magic-type labels the verdict uses (also the stable strings in --json output).
MAGIC_GEN2 = "gen2-direct-write"
MAGIC_LOCKED = "genuine-or-locked"

_CLONE_UID_WARNING = (
    "Writing block 0 rewrites the card's UID/BCC/SAK/ATQA — its identity. Only "
    "do this on a magic card (gen2/CUID) you own or have permission to clone. On "
    "a genuine MIFARE Classic block 0 is factory-OTP and the write fails (in the "
    "worst case bricking the card). Continue?"
)


def _bcc(uid_bytes: bytes) -> int:
    """MIFARE block-0 check byte: XOR of the four UID bytes."""
    return uid_bytes[0] ^ uid_bytes[1] ^ uid_bytes[2] ^ uid_bytes[3]


def craft_block0(uid_hex: str, sak_hex: str, atqa_hex: str, mfg_hex: str) -> str:
    """Assemble a 4-byte-UID block 0 from its parts, recomputing the BCC so the
    card's own anticollision check passes: UID(4) · BCC(1) · SAK(1) · ATQA(2) ·
    manufacturer(8) = 16 bytes → 32 hex chars. Inputs are assumed already
    validated by the caller (the command validates every field first)."""
    uid = bytes.fromhex(uid_hex)
    return (
        uid_hex.upper()
        + f"{_bcc(uid):02X}"
        + sak_hex.upper()
        + atqa_hex.upper()
        + mfg_hex.upper()
    )


def _read_block0(link) -> Tuple[Optional[str], str]:
    """Read the current block 0 (`mifare read 0`). Returns (hex32, reason)."""
    r = link.command("mifare read 0")
    if not r.ok:
        return None, f"block 0 read failed: {r.message}"
    current = r.data.get("mifare_data", "").partition(" ")[2]
    if len(current) != _MIFARE_BLOCK_HEX_LEN:
        return None, "block 0 read returned no data"
    return current.upper(), "ok"


def _probe_writable(link, current_block0: str) -> Tuple[bool, str]:
    """Non-destructive writability probe: write the CURRENT block 0 back to the
    card unchanged. Only a card whose block 0 is directly writable (a gen2/CUID
    magic) accepts it; a genuine factory-OTP block 0 NAKs and nothing changes.
    Returns (writable, reason). This is the same safe probe `mifare restore`
    uses before it will touch a UID."""
    probe = link.command(f"mifare write 0 {current_block0.upper()}")
    if not probe.ok:
        return False, probe.message
    return True, "ok"


def verify_uid_clone(
    link,
    key_a: Optional[str],
    key_b: Optional[str],
    timeout: float,
    target_uid: Optional[str] = None,
    sak_override: Optional[str] = None,
    atqa_override: Optional[str] = None,
) -> Dict[str, object]:
    """Drive the whole verification against the selected card and return a
    structured verdict (the render is separate, so this is unit-testable on the
    CardLink mock with no PN7150).

    Flow: authenticate sector 0 → read block 0 → non-destructive writability
    probe → classify (gen2 vs genuine/locked). If `target_uid` is given AND the
    card is writable, craft the new block 0 (new UID + recomputed BCC, SAK/ATQA/
    manufacturer preserved from the current card unless overridden), write it,
    read it back and compare byte-for-byte to prove a real 1:1 UID clone.

    The verdict dict keys are stable (they back --json):
      sector0_auth, current_uid, block0_current, block0_writable, magic_type,
      uid_rewrite_supported, target_uid, block0_written, verified, reason, detail.

    `reason` stays "ok" whenever the verification RAN cleanly — including a clean
    "not writable" conclusion on a genuine card, which is an answer, not a
    failure; `detail` then explains it. `reason` names a real error (auth/read/
    write/read-back failure) only when the verification could not complete.
    """
    verdict: Dict[str, object] = {
        "sector0_auth": None,
        "current_uid": None,
        "block0_current": None,
        "block0_writable": False,
        "magic_type": None,
        "uid_rewrite_supported": False,
        "target_uid": target_uid.upper() if target_uid else None,
        "block0_written": None,
        "verified": None,
        "reason": "ok",
        "detail": "",
    }

    # 1) Authenticate sector 0 (block 0's sector) so read/write can touch it.
    used_kt, reason = _auth_sector(link, 0, key_a, key_b, timeout, first=True)
    if used_kt is None:
        verdict["reason"] = f"sector 0 authentication failed: {reason}"
        return verdict
    verdict["sector0_auth"] = used_kt

    # 2) Read the current block 0 and dissect the UID/SAK/ATQA/mfg.
    current, reason = _read_block0(link)
    if current is None:
        verdict["reason"] = reason
        return verdict
    verdict["block0_current"] = current
    b0 = parse_block0(current)
    if b0 is None:
        verdict["reason"] = "block 0 is not a standard 4-byte-UID layout"
        return verdict
    verdict["current_uid"] = b0.uid

    # 3) Non-destructive writability probe → classify the magic type.
    writable, probe_reason = _probe_writable(link, current)
    verdict["block0_writable"] = writable
    verdict["uid_rewrite_supported"] = writable
    verdict["magic_type"] = MAGIC_GEN2 if writable else MAGIC_LOCKED
    if not writable:
        # Genuine (or wrong key): a clean, valid conclusion, not an error. A UID
        # rewrite would need the gen1a backdoor, which this hardware cannot
        # emit. Reported, never attempted.
        verdict["detail"] = (
            "block 0 not writable — genuine/locked card or wrong key. A UID "
            "rewrite would need the gen1a backdoor, which the PN7150 cannot "
            f"emit on this stack ({probe_reason})"
        )
        return verdict

    # 4) Capability probe only (no target UID): stop here, nothing changed.
    if not target_uid:
        return verdict

    # 5) Write the source UID and verify by read-back.
    sak = (sak_override or b0.sak).upper()
    atqa = (atqa_override or b0.atqa).upper()
    new_block0 = craft_block0(target_uid, sak, atqa, b0.manufacturer_data)
    w = link.command(f"mifare write 0 {new_block0}")
    if not w.ok:
        verdict["reason"] = f"block 0 write failed: {w.message}"
        return verdict
    verdict["block0_written"] = new_block0

    readback, reason = _read_block0(link)
    if readback is None:
        verdict["reason"] = f"read-back after write failed: {reason}"
        verdict["verified"] = False
        return verdict
    verdict["verified"] = readback == new_block0
    if not verdict["verified"]:
        verdict["reason"] = f"read-back mismatch: wrote {new_block0}, read {readback}"
    return verdict


def _print_verdict(target: str, v: Dict[str, object]) -> None:
    console.print("")
    _print_field("device", target)
    _print_field("sector 0 auth", str(v["sector0_auth"] or "[dim]failed[/dim]"))
    if v["current_uid"]:
        _print_field("current UID", str(v["current_uid"]))
    writable = v["block0_writable"]
    _print_field(
        "block 0 writable",
        "[green]yes[/green]" if writable else "[red]no[/red]",
    )
    if v["magic_type"] == MAGIC_GEN2:
        _print_field("magic type", "gen2 / CUID (direct-write)")
        _print_field("UID rewrite", "[green]SUPPORTED on this hardware[/green]")
    elif v["magic_type"] == MAGIC_LOCKED:
        _print_field("magic type", "genuine / locked (not direct-write)")
        _print_field(
            "UID rewrite",
            "[red]needs gen1a backdoor — out of reach on the PN7150[/red]",
        )
    if v["target_uid"]:
        _print_field("target UID", str(v["target_uid"]))
    if v["block0_written"]:
        _print_field("block 0 written", str(v["block0_written"]))
    if v["verified"] is not None:
        _print_field(
            "verified (read-back)",
            (
                "[green]YES — 1:1 UID clone confirmed[/green]"
                if v["verified"]
                else "[red]NO[/red]"
            ),
        )


@click.command("clone-uid")
@click.option(
    "--uid",
    "uid",
    default=None,
    metavar="HEX8",
    help="4-byte UID (8 hex chars) to write onto the card, then verify by "
    "read-back. Omit to run a non-destructive capability probe only (reports "
    "whether block 0 is writable without changing the UID).",
)
@click.option(
    "--from-dump",
    "from_dump",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    metavar="FILE",
    help="Take the source UID from a canonical `mifare dump --out` JSON's "
    "block 0 instead of --uid.",
)
@_MIFARE_KEY_TYPE_OPTION
@click.option(
    "--key",
    default=None,
    metavar="HEX12",
    help=f"Key to authenticate sector 0 (default: {_DEFAULT_KEY}, key A).",
)
@click.option(
    "-k",
    "--keys-file",
    "keys_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    metavar="FILE",
    help="Take sector 0's key A/B from a `sector:keyA:keyB` file "
    "(`mifare check --output-keys`) instead of --key.",
)
@click.option(
    "--sak",
    "sak",
    default=None,
    metavar="HH",
    help="Override SAK byte in the "
    "written block 0 (default: keep the card's current SAK).",
)
@click.option(
    "--atqa",
    "atqa",
    default=None,
    metavar="HHHH",
    help="Override the 2 ATQA "
    "bytes in the written block 0 (default: keep the card's current ATQA).",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Skip the confirmation before writing a new UID (scripted use).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the verdict as JSON.")
@_MIFARE_TIMEOUT_OPTION
@device_options
@click.pass_context
def mifare_clone_uid_cmd(
    ctx,
    uid,
    from_dump,
    key_type,
    key,
    keys_file,
    sak,
    atqa,
    yes,
    as_json,
    timeout,
    verbose,
    port,
    device_id,
):
    """Verify whether THIS hardware can rewrite a Mifare Classic UID (block 0).

    \b
    ⚠ AUTHORIZED USE ONLY. Only probe/clone cards you own or have explicit
    permission to test.

    Fase 6 of PLAN_Mejoras_MIFARE.md. Authenticates sector 0, reads block 0 and
    runs a non-destructive writability probe to classify the card:

    \b
      • gen2 / CUID (direct-write): block 0 writable with a normal auth + write.
        The PN7150 does exactly this, so the UID CAN be rewritten — pass --uid
        (or --from-dump) and this command writes it and reads it back to confirm
        a real 1:1 clone.
      • genuine / locked: block 0 is factory-OTP. Rewriting the UID would need
        the gen1a backdoor (a 7-bit CRC-less unlock frame) which the PN7150's
        NCI reader stack cannot emit — reported, never attempted.

    With no --uid/--from-dump the probe is read-only (the card is not changed).
    See docs/MIFARE_IMPROVEMENTS_Phase6.md for the hardware investigation.
    """
    if uid and from_dump:
        print_error("pass either --uid or --from-dump, not both")
        raise SystemExit(1)

    # Resolve the source UID (if any).
    target_uid: Optional[str] = None
    if from_dump:
        dump = _load_restore_dump(from_dump)
        sector0 = next((e for e in dump["sectors"] if e["sector"] == 0), None)
        if sector0 is None:
            print_error(f"{from_dump}: no sector 0 (block 0) to read a UID from")
            raise SystemExit(1)
        target_uid = sector0["blocks"][0][:8]
    elif uid:
        target_uid = uid

    # Validate every field before touching the device.
    for value, length, label in (
        (target_uid, 8, "--uid"),
        (key, _MIFARE_KEY_HEX_LEN, "--key"),
        (sak, 2, "--sak"),
        (atqa, 4, "--atqa"),
    ):
        if value is not None:
            msg = _mifare_validate_hex(value, length, label)
            if msg:
                print_error(msg)
                raise SystemExit(1)

    # Resolve sector 0's auth key(s).
    if keys_file and key:
        print_error("pass either --key or --keys-file, not both")
        raise SystemExit(1)
    if keys_file:
        key_a, key_b = _load_sector_key_pair(keys_file, 0)
        if not key_a and not key_b:
            print_error(f"sector 0 has no key A or key B in {keys_file}")
            raise SystemExit(1)
    else:
        chosen = (key or _DEFAULT_KEY).upper()
        key_a = chosen if key_type.upper() == "A" else None
        key_b = chosen if key_type.upper() == "B" else None

    # Confirm before a destructive UID write (a read-only probe never asks).
    if target_uid and not yes and not as_json:
        if not click.confirm(_CLONE_UID_WARNING, default=False):
            print_warning("aborted — UID not written")
            raise SystemExit(1)

    level = _verbosity(ctx, verbose)
    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        if not as_json:
            print_info(
                f"Verifying UID rewrite on {target}"
                + (f" → {target_uid.upper()}" if target_uid else " (probe only)")
            )
        verdict = verify_uid_clone(link, key_a, key_b, timeout, target_uid, sak, atqa)

    if as_json:
        print(json.dumps(verdict))
    else:
        _print_verdict(target, verdict)

    # A real error (auth/read/write/read-back failure) never completed the
    # verification → exit 1.
    if verdict["reason"] != "ok":
        if not as_json:
            print_error(str(verdict["reason"]))
        raise SystemExit(1)

    # The verification ran. Exit 0 means "UID rewrite works on this card": a
    # write must have verified by read-back; a probe-only run passes when block 0
    # is writable. A clean "not writable" conclusion is reported, then exit 1 so
    # scripts can gate on "is a 1:1 UID clone possible here?".
    if not as_json:
        if verdict["verified"]:
            print_success("UID clone verified: block 0 read back matches the source")
        elif target_uid:
            print_error("UID write could not be verified by read-back")
        elif verdict["block0_writable"]:
            print_success("block 0 is writable (gen2/CUID) — UID rewrite supported")
        else:
            print_warning(str(verdict["detail"]))
    if target_uid:
        if not verdict["verified"]:
            raise SystemExit(1)
    elif not verdict["block0_writable"]:
        raise SystemExit(1)
