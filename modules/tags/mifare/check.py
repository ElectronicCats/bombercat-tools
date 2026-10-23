#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

# `mifare check` — dictionary attack: try known keys against every sector
# and report which ones open. docs/CLI_IMPROVEMENTS_MifareCheck.md §6.3.

import json
import time
from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import click
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from ...utils.cli_options import device_options
from ...utils.detection_cli import (
    print_field as _print_field,
    refuse_overwrite as _refuse_overwrite,
    verbosity as _verbosity,
)
from ...utils.output import (
    console,
    make_tracer,
    print_dim,
    print_error,
    print_info,
    print_subtitle,
    print_success,
    print_warning,
)
from .access_bits import _KEY_A, parse_access_bits
from .block0 import parse_block0
from .device_sweep import (
    DEVICE_SWEEP_MAX_KEYS,
    DeviceSweepError,
    DeviceSweepUnsupported,
    run_device_sweep,
)
from .dicts import REGISTRY, CandidatePlan, UnknownDictError, select as _select_dicts
from . import uidkeys
from .keyfile import default_keyfile, load_keys
from .common import (
    _MIFARE_BLOCK_HEX_LEN,
    _MIFARE_CHECK_MAX_SECTORS,
    _MIFARE_HEX_RE,
    _MIFARE_KEY_HEX_LEN,
    _MIFARE_TRAILER_AC_LEN,
    _sector_first_block,
)
from .session import (
    _MIFARE_TIMEOUT_OPTION,
    _mifare_session,
    _run_mifare_command,
)


_TIER_LABEL = {0: "default", 1: "app", 2: "vendor"}


def _print_dict_registry() -> None:
    """Render the registered `--dict` families (name, tier, key count, sector
    bias, description). Pure host-side; no device involved."""
    table = Table(
        title="mifare check — named key dictionaries", header_style="cyan bold"
    )
    table.add_column("name", style="cyan")
    table.add_column("tier")
    table.add_column("keys", justify="right")
    table.add_column("sectors")
    table.add_column("description")
    for nd in REGISTRY.values():
        try:
            count = str(len(nd.load()))
        except OSError:
            count = "[red]missing[/red]"
        sectors = ", ".join(str(s) for s in sorted(nd.sectors)) if nd.sectors else "—"
        table.add_row(
            nd.name,
            _TIER_LABEL.get(nd.tier, str(nd.tier)),
            count,
            sectors,
            nd.description,
        )
    console.print(table)
    print_info(
        "add with --dict <names> (comma-separated, or 'all'); "
        "one-off files with --dict-file <path>"
    )


def _print_uid_schemes() -> None:
    """Render the registered `--uid-derived` schemes (name, whether they need a
    secret master key, sectors covered, description). Pure host-side."""
    table = Table(
        title="mifare check — UID-derived key schemes", header_style="cyan bold"
    )
    table.add_column("name", style="cyan")
    table.add_column("generable")
    table.add_column("sectors")
    table.add_column("description")
    for sc in uidkeys.SCHEMES.values():
        generable = (
            "[red]no (secret)[/red]" if sc.requires_secret else "[green]yes[/green]"
        )
        sectors = ", ".join(str(s) for s in sorted(sc.sectors)) if sc.sectors else "—"
        table.add_row(sc.name, generable, sectors, sc.description)
    console.print(table)
    print_info(
        "enable with --uid-derived <names> (comma-separated, or 'all' for every "
        "public scheme); needs the card's UID (read from block 0). Secret-key "
        "schemes are out of scope and refuse."
    )


def _read_uid_via_block0(link, key_type: str, key: str) -> Optional[str]:
    """Read block 0 of sector 0 with a key already known to open it and return
    the 4-byte UID (8 hex chars), or None if it couldn't be read/parsed. Pure
    read — no cryptography; UID-derived schemes need it as their input."""
    r = link.command(f"mifare sector 0 {key_type} {key.upper()}")
    if not r.ok:
        return None
    data_hex = r.data.get("mifare_sector", "")
    if len(data_hex) < _MIFARE_BLOCK_HEX_LEN:
        return None
    b0 = parse_block0(data_hex[:_MIFARE_BLOCK_HEX_LEN])
    return b0.uid if b0 and len(b0.uid) == uidkeys._UID4_HEX_LEN else None


def _write_keyfile(path: str, keys: List[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for key in keys:
            f.write(key + "\n")


def _write_sector_keyfile(
    path: str, sectors: int, found: Dict[Tuple[int, str], Optional[str]]
) -> None:
    """Write the per-sector `sector:keyA:keyB` file consumed by
    `mifare sector --keys-file`. A key type that wasn't recovered is left
    blank (e.g. `3::FFFFFFFFFFFF`), so the sector line is always present even
    when only one of its two keys is known."""
    with open(path, "w", encoding="utf-8") as f:
        for s in range(sectors):
            key_a = found.get((s, "A")) or ""
            key_b = found.get((s, "B")) or ""
            f.write(f"{s}:{key_a}:{key_b}\n")


# ── on-device sweep policy (Fase 4, tarea 4.1) ────────────────────────────────
#
# The firmware can run the sweep itself — ONE `mifare check` round-trip — but
# only with ITS OWN candidate universe: the compact embedded dictionary (32
# keys) plus the persistent key-store, or an explicit list of at most 64 keys
# passed with `--keys`. The host sweep knows more (the 2477-key bundled
# dictionary, the named families, the UID-derived schemes) but pays one USB
# round-trip per candidate key.
#
# `--mode` picks between the two:
#   auto   (default) use the device sweep only when the candidate universe is
#          the SAME on both sides — an explicit `--keys` list of <=64 keys — so
#          only the round-trips change and the result stays identical.
#          Otherwise (the bundled dictionary, `--dict`, `--uid-derived`, a
#          single key type) keep the host sweep: the device's table is smaller,
#          and silently narrowing the coverage would turn a key the host
#          recovers into a false "unknown".
#   device force the device sweep: its own universe (embedded dictionary +
#          key-store) or the explicit `--keys` list.
#   host   force the host sweep — today's behaviour, unchanged.
def _plan_device_sweep(
    mode: str,
    host_only: List[str],
    key_type: str,
    explicit_keys: Optional[List[str]],
) -> Tuple[bool, Optional[List[str]]]:
    """Decide whether the sweep runs on the device.

    Returns `(use_device, device_keys)`: `device_keys` is the list to pass to
    the device with `--keys` (None = let the device use its own dictionary +
    key-store). Refuses (exit 1) the combinations the device cannot honour
    when the user explicitly asked for the device sweep.
    """
    if mode == "host":
        return False, None

    if host_only:
        if mode == "device":
            print_error(
                "the on-device sweep cannot honour "
                + ", ".join(host_only)
                + " — those are host-side candidate sources; use --mode host"
            )
            raise SystemExit(1)
        return False, None

    if mode == "device":
        if explicit_keys and len(explicit_keys) > DEVICE_SWEEP_MAX_KEYS:
            print_error(
                f"the on-device sweep takes at most {DEVICE_SWEEP_MAX_KEYS} keys "
                f"(--keys) — use --mode host for a bigger dictionary"
            )
            raise SystemExit(1)
        return True, explicit_keys or None

    # auto: only when both sides can try the very same candidates.
    if key_type.lower() != "both":
        return False, None  # the device always sweeps A and B
    if not explicit_keys:
        return False, None  # the bundled dictionary is bigger than the device's
    if len(explicit_keys) > DEVICE_SWEEP_MAX_KEYS:
        return False, None
    return True, explicit_keys


@dataclass
class _SweepResult:
    """What a sweep found, whichever side ran it."""

    found: Dict[Tuple[int, str], Optional[str]] = field(default_factory=dict)
    known: "OrderedDict[str, None]" = field(default_factory=OrderedDict)
    attempts: int = 0
    naive_attempts: int = 0
    uid: Optional[str] = None
    interrupted: bool = False
    # True when the sweep only covered PART of the card — the device's own
    # sweep was interrupted before it reached every sector. The keys it
    # reports are then a partial result, and the summary has to say so.
    partial: bool = False
    # True when the DEVICE ran the trailer-read pass itself (Fase 4, tarea
    # 4.4) and already recovered every Key B it could read back — the host
    # must then not repeat the pass over USB.
    device_trailer: bool = False
    # Where the BOARD spent the wall-clock (device mode only), in ms:
    # auth_ms / reselect_ms / trailer_ms.
    auth_ms: Optional[int] = None
    reselect_ms: Optional[int] = None
    trailer_ms: Optional[int] = None
    # True when the device answered from its own cached sweep (it had already
    # swept this card on detection, so it did not sweep it again).
    cached: Optional[bool] = None


def _run_host_sweep(
    link,
    target: str,
    plan,
    key_types: List[str],
    sectors: int,
    schemes,
    uid_max: int,
    timeout: float,
    as_json: bool,
    start_time: float,
    total: int,
) -> _SweepResult:
    """The host-driven sweep: ONE `mifare auth` USB round-trip per candidate.

    Unchanged behaviour (the Fase 0 baseline: 2514 auths on card_1k with
    key-type A) — this is the fallback the on-device sweep replaces when it
    can't run, and the reference the device sweep must agree with.
    """
    result = _SweepResult()
    found = result.found
    known = result.known
    attempts = 0
    # Honest baseline for the "auths saved" stat: how many authentications a
    # naive sweep of the SAME candidate universe would cost — every (sector,
    # key type) slot trying its full pool to the end, with no reuse shortcut
    # and no early cut. Accumulated per slot below; attempts is what we
    # actually issued. See the Timing summary / JSON `stats`.
    naive_attempts = 0
    uid: Optional[str] = None  # filled once sector 0 opens (UID-derived schemes)

    if not as_json:
        print_info(
            f"Checking {target} — {sectors} sector(s) x {len(key_types)} key "
            f"type(s), {plan.size} keys — Ctrl-C for partial results"
        )
    total_attempts = total * plan.size
    progress = (
        None
        if as_json
        else Progress(
            SpinnerColumn(style="cyan"),
            TextColumn(
                "[cyan]sector {task.fields[sector]:>2} key {task.fields[key_type]}"
                "[/cyan]"
            ),
            BarColumn(bar_width=24, complete_style="cyan", finished_style="cyan"),
            TextColumn("[dim]{task.fields[tried]}/{task.fields[key_total]} keys[/dim]"),
            TextColumn("[dim]{task.fields[rate]:.0f} keys/s[/dim]"),
            TimeRemainingColumn(),
            TimeElapsedColumn(),
            TextColumn(
                "[dim]{task.fields[recovered]}/{task.fields[pairs]} recovered[/dim]"
            ),
            console=console,
            transient=True,
        )
    )
    first = True
    try:
        with progress or nullcontext():
            task = (
                progress.add_task(
                    "",
                    total=total_attempts,
                    recovered=0,
                    pairs=total,
                    sector=0,
                    key_type="",
                    tried=0,
                    key_total=plan.size,
                    rate=0.0,
                )
                if progress
                else None
            )
            for s in range(sectors):
                block = _sector_first_block(s)
                uid_cands = (
                    uidkeys.candidates_for(uid, schemes, s, uid_max)
                    if schemes and uid
                    else []
                )
                for kt in key_types:
                    # The universe a naive sweep would try for this slot:
                    # UID-derived candidates + the full dictionary for this
                    # sector, deduped — WITHOUT the reuse shortcut and run to
                    # completion. This is the baseline "auths saved" is
                    # measured against (equals sectors x universe when no
                    # --dict/--uid-derived, matching the Fase 0 pins).
                    naive_pool = list(
                        dict.fromkeys(uid_cands + list(plan.for_sector(s)))
                    )
                    naive_attempts += len(naive_pool)
                    # What we actually try: confirmed keys (reuse) first, then
                    # that pool; deduped, first occurrence wins, with an early
                    # cut on the first hit.
                    candidates = list(dict.fromkeys(list(known) + naive_pool))
                    key = None
                    tried = 0
                    for candidate in candidates:
                        line = f"mifare auth {block} {kt} {candidate}"
                        if first:
                            r = _run_mifare_command(link, line, timeout)
                            first = False
                        else:
                            r = link.command(line)
                        tried += 1
                        attempts += 1
                        if progress:
                            elapsed = time.monotonic() - start_time
                            progress.update(
                                task,
                                advance=1,
                                sector=s,
                                key_type=kt,
                                tried=tried,
                                rate=attempts / elapsed if elapsed > 0 else 0.0,
                            )
                        if r.ok:
                            key = candidate
                            break
                    found[(s, kt)] = key
                    if key:
                        known.setdefault(key, None)
                    if progress:
                        recovered = sum(1 for v in found.values() if v)
                        skipped = len(candidates) - tried
                        if skipped:
                            progress.update(task, advance=skipped)
                        progress.update(task, recovered=recovered)

                # Once sector 0 is open, read block 0 for the UID so the
                # UID-derived schemes can generate candidates for the rest.
                # (Feedback is printed after the progress bar closes, below.)
                if schemes and uid is None and s == 0:
                    kt0, key0 = next(
                        (
                            (kt, found[(0, kt)])
                            for kt in ("A", "B")
                            if found.get((0, kt))
                        ),
                        (None, None),
                    )
                    if key0:
                        uid = _read_uid_via_block0(link, kt0, key0)
    except KeyboardInterrupt:
        result.interrupted = True

    result.attempts = attempts
    result.naive_attempts = naive_attempts
    result.uid = uid

    if schemes and not as_json:
        if uid:
            print_info(
                f"UID {uid} — UID-derived candidates via "
                f"{', '.join(sc.name for sc in schemes)}"
                + (f" (max {uid_max}/sector)" if uid_max else "")
            )
        else:
            print_dim(
                "UID unavailable (sector 0 not opened / block 0 unreadable) "
                "— UID-derived schemes skipped"
            )
    return result


def _sweep_on_device(
    link,
    target: str,
    sectors: int,
    device_keys: Optional[List[str]],
    key_types: List[str],
    as_json: bool,
    timeout: float,
    mode: str,
    fresh: bool = False,
) -> Optional[_SweepResult]:
    """The on-device sweep: ONE `mifare check` round-trip, the device sweeps.

    Returns None when the flashed firmware has no `mifare check` command and
    the caller should fall back to the host-driven sweep (only in `auto`
    mode; in `device` mode that is a hard error).
    """
    if not as_json:
        universe = (
            f"{len(device_keys)} key(s)"
            if device_keys
            else "the device's dictionary + key-store"
        )
        print_info(
            f"Checking {target} — on-device sweep, {sectors} sector(s) x "
            f"{len(key_types)} key type(s), {universe} — the device runs the "
            "dictionary itself (one round-trip)"
        )
    try:
        with Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[cyan]device sweep[/cyan]"),
            TextColumn("[dim]{task.elapsed:.0f}s[/dim]"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task("", total=None)
            sweep = run_device_sweep(
                link,
                sectors,
                device_keys,
                tap_timeout=timeout,
                quiet=as_json,
                fresh=fresh,
            )
    except DeviceSweepUnsupported as e:
        if mode == "device":
            print_error(
                f"the flashed firmware has no on-device sweep ({e}) — update the "
                "firmware or use --mode host"
            )
            raise SystemExit(1)
        if not as_json:
            print_dim(
                "the device did not run the sweep (older firmware?) — using the "
                "host-driven sweep"
            )
        return None
    except DeviceSweepError as e:
        print_error(f"device sweep failed: {e}")
        raise SystemExit(1)

    result = _SweepResult()
    for s in range(sectors):
        key_a, key_b = sweep.keys.get(s, (None, None))
        # Only the requested key types are reported: the device always sweeps
        # A and B, but `--key-type A` means "report A".
        if "A" in key_types:
            result.found[(s, "A")] = key_a
            if key_a:
                result.known.setdefault(key_a, None)
        if "B" in key_types:
            result.found[(s, "B")] = key_b
            if key_b:
                result.known.setdefault(key_b, None)
    result.attempts = sweep.attempts
    result.naive_attempts = sweep.naive
    # The device runs the trailer-read pass itself when its firmware reports
    # the `trailer=` counter (Fase 4, tarea 4.4): it recovered the same Key B
    # the host would have read over USB, so the host must not repeat the
    # pass. An older firmware has no such counter and the host does it.
    result.device_trailer = sweep.did_trailer_read()
    # The board's own wall-clock split (device mode only).
    result.auth_ms = sweep.auth_ms
    result.reselect_ms = sweep.reselect_ms
    result.trailer_ms = sweep.trailer_ms
    result.cached = sweep.cached
    # A sweep the device interrupted is a PARTIAL result: the sectors it did
    # not reach are unknown because the sweep stopped, not because the keys
    # are not there. Say it out loud instead of presenting a partial card as
    # the whole one.
    if sweep.aborted:
        result.partial = True
        if not as_json:
            print_warning(
                "the device's sweep was interrupted before it covered every "
                "sector — the keys below are a partial result (the card may "
                "well open with the keys it did not get to try)"
            )

    # The device reports its own counters; if they disagree with what came
    # down the wire, say so rather than picking one silently.
    if sweep.recovered and sweep.recovered != sweep.recovered_count():
        print_warning(
            f"the device reported {sweep.recovered} recovered key(s) but "
            f"{sweep.recovered_count()} arrived — trusting the wire"
        )
    if sweep.total and sweep.total != sectors * 2:
        print_warning(
            f"the device swept {sweep.total} key slots, expected "
            f"{sectors * 2} — trusting the wire"
        )
    if not as_json and key_types != ["A", "B"]:
        print_dim(
            "the device sweep always tries Key A and B — --key-type only "
            "filters the report"
        )
    return result


# Trailer-read key-B recovery. This is NOT the Crypto-1 nested attack (that
# needs raw nonce/parity capture the PN7150 never surfaces — it runs Crypto-1
# in-chip and only reports auth pass/fail). Instead, once the dictionary has
# opened a sector with key A, we read its trailer with that key: on cards whose
# access bits leave key B *readable with key A* (the transport/default configs
# 000 and 001 — NXP MF1S50yyX Table 8), key B comes back in cleartext in the
# trailer's last 6 bytes. Key A itself never reads back under any access
# condition, so only key B is recoverable this way.
#
# Offsets into a `mifare sector` dump (4 blocks x 32 hex chars); the trailer is
# the 4th block, laid out key A (bytes 0-5) | access+GPB (6-9) | key B (10-15).
_TRAILER_HEX_START = 3 * _MIFARE_BLOCK_HEX_LEN
_TRAILER_AC_HEX = slice(
    _TRAILER_HEX_START + _MIFARE_KEY_HEX_LEN,
    _TRAILER_HEX_START + _MIFARE_KEY_HEX_LEN + 6,
)
_TRAILER_KEYB_HEX = slice(
    _TRAILER_HEX_START + _MIFARE_KEY_HEX_LEN + _MIFARE_TRAILER_AC_LEN,
    _TRAILER_HEX_START + _MIFARE_BLOCK_HEX_LEN,
)


def _recover_key_b_via_trailer(
    link, sector: int, key_a: str
) -> Tuple[Optional[str], str]:
    """Read SECTOR's key B off the card using its known key A, no cryptography.

    Returns ``(key_b, reason)``: the 12-hex key B and ``"ok"`` on success, or
    ``(None, reason)`` explaining why not — so the caller can tell "the card
    protects key B" (nothing more we can do without a real nested attack) from
    "the read failed" (worth retrying). Key A itself never reads back under
    any access condition, so only key B is ever recoverable this way."""
    r = link.command(f"mifare sector {sector} A {key_a.upper()}")
    if not r.ok:
        return None, f"trailer read failed ({r.message or 'auth/read error'})"
    data_hex = r.data.get("mifare_sector", "")
    if len(data_hex) < _MIFARE_BLOCK_HEX_LEN * 4:
        return None, "trailer read returned no data"
    access = parse_access_bits(data_hex[_TRAILER_AC_HEX])
    if access is None:
        return None, "trailer access bits unreadable"
    if access.trailer.key_b_read != _KEY_A:
        return None, "key B is protected by the access bits (not readable)"
    key_b = data_hex[_TRAILER_KEYB_HEX].upper()
    if not _MIFARE_HEX_RE.match(key_b) or key_b == "0" * _MIFARE_KEY_HEX_LEN:
        return None, "card returned zeros for key B"
    return key_b, "ok"


def _trailer_read_pass(
    link,
    found: Dict[Tuple[int, str], Optional[str]],
    known: "OrderedDict[str, None]",
    key_types: List[str],
    sectors: int,
    as_json: bool,
) -> None:
    """Trailer-read fallback for the sectors opened with key A but not key B.

    Shared by both sweep modes (host-driven and on-device): the device sweep
    recovers keys the same way, it just does it without the USB round-trips —
    reading key B back off the card is still a host-side step, because it
    needs the access bits of the sector's trailer.
    """
    if "B" not in key_types:
        return
    targets = [
        (s, key_a)
        for s in range(sectors)
        if found.get((s, "B")) is None and (key_a := found.get((s, "A"))) is not None
    ]
    if not targets:
        return
    if not as_json:
        print_info(
            f"trailer read: {len(targets)} sector(s) opened key A but "
            "not key B — reading key B off the card (no Crypto-1 attack)"
        )
    for s, key_a in targets:
        key_b, reason = _recover_key_b_via_trailer(link, s, key_a)
        if key_b:
            found[(s, "B")] = key_b
            known.setdefault(key_b, None)
            if not as_json:
                print_success(f"sector {s} key B recovered via trailer read: {key_b}")
        elif not as_json:
            print_dim(f"  sector {s} key B not recovered — {reason}")


@click.command("check")
@click.option(
    "--keys",
    "keyfiles",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False),
    metavar="FILE",
    help="Key dictionary (.keys/.dic/.md), repeatable. Defaults to the "
    "bundled dictionary (2477 known keys); passing --keys replaces it — "
    "include the bundled file yourself alongside others if you want both.",
)
@click.option(
    "--dict",
    "dict_names",
    multiple=True,
    metavar="NAMES",
    help="Named key families to add on top, comma-separated and repeatable "
    f"(e.g. --dict mad,transport). Known: {', '.join(REGISTRY)}, or 'all'. "
    "Their higher-probability keys are tried before the base dictionary; MAD "
    "keys are tried first on the MAD sectors. See --list-dicts.",
)
@click.option(
    "--dict-file",
    "dict_files",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False),
    metavar="FILE",
    help="Extra .dic/.keys file to add as candidates (repeatable), at the "
    "lowest/generic priority. Unlike --keys, this adds to the base dictionary "
    "instead of replacing it.",
)
@click.option(
    "--list-dicts",
    is_flag=True,
    help="List the registered named key families and exit (no device needed).",
)
@click.option(
    "--uid-derived",
    "uid_schemes",
    multiple=True,
    metavar="NAMES",
    help="Also try keys derived from the card UID by a PUBLIC diversification "
    f"scheme, comma-separated and repeatable. Known: {', '.join(uidkeys.SCHEMES)}, "
    "or 'all'. Off by default; needs the UID (read from block 0). Only public "
    "algorithms — secret-key (AES/HMAC) schemes are out of scope. See "
    "--list-uid-schemes.",
)
@click.option(
    "--uid-max",
    type=click.IntRange(0, None),
    default=0,
    show_default=True,
    metavar="N",
    help="Cap UID-derived candidates tried per sector (0 = no cap). Bounds the "
    "extra authentications over the slow I2C link.",
)
@click.option(
    "--list-uid-schemes",
    is_flag=True,
    help="List the registered UID-derived key schemes and exit (no device).",
)
@click.option(
    "--sectors",
    type=click.IntRange(1, _MIFARE_CHECK_MAX_SECTORS),
    default=16,
    show_default=True,
    help=f"Number of sectors to check (16 = 1K). Max {_MIFARE_CHECK_MAX_SECTORS} "
    "— 4K's 16-block sectors (32-39) aren't supported yet.",
)
@click.option(
    "--key-type",
    "key_type",
    type=click.Choice(["A", "B", "both"], case_sensitive=False),
    default="both",
    show_default=True,
    help="Which key slot(s) to try.",
)
@click.option(
    "--mode",
    "mode",
    type=click.Choice(["auto", "device", "host"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="Where the sweep runs. 'auto' (default) uses the device's own sweep "
    "when both sides can try the same candidates (an explicit --keys list of "
    f"<= {DEVICE_SWEEP_MAX_KEYS} keys) and the host-driven sweep otherwise; "
    "'device' forces the device sweep (one round-trip, its embedded "
    "dictionary + key-store); 'host' forces the host sweep (the full bundled "
    "dictionary, one USB round-trip per candidate).",
)
@click.option(
    "--fresh",
    "fresh",
    is_flag=True,
    help="Sweep the card again even when the device still holds the results "
    "of an earlier sweep. By default the device answers from its cache "
    "(the firmware already swept the card when it detected it), which "
    "repeats that result — use --fresh when that sweep was interrupted or "
    "when you just want a new answer.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit one JSON object on stdout.")
@click.option(
    "--out",
    "out_file",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    metavar="FILE",
    help="Write the recovered keys as a keyfile (one 12-hex key per line, "
    "mfoc/proxmark-compatible).",
)
@click.option(
    "-o",
    "--output-keys",
    "output_keys_file",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    metavar="FILE",
    help="Write recovered keys as one `sector:keyA:keyB` line per sector, "
    "for `mifare sector --keys-file`. A key type not recovered is left blank.",
)
@click.option(
    "--force", is_flag=True, help="Overwrite --out/--output-keys if they exist."
)
@_MIFARE_TIMEOUT_OPTION
@device_options
@click.pass_context
def mifare_check_cmd(
    ctx,
    keyfiles,
    dict_names,
    dict_files,
    list_dicts,
    uid_schemes,
    uid_max,
    list_uid_schemes,
    sectors,
    key_type,
    mode,
    fresh,
    as_json,
    out_file,
    output_keys_file,
    force,
    timeout,
    verbose,
    port,
    device_id,
):
    """Dictionary attack: try known keys against every sector and report
    which ones open.

    Authorized use only — test only cards you own or with the owner's
    explicit permission. A sector that opens with a dictionary key (e.g. the
    default `FFFFFFFFFFFF`) is evidence that card is exposed.

    Exit code: 0 if every requested (sector, key type) was recovered, 1
    otherwise (some unknown, or the sweep was interrupted early).
    """
    if list_dicts:
        _print_dict_registry()
        raise SystemExit(0)

    if list_uid_schemes:
        _print_uid_schemes()
        raise SystemExit(0)

    try:
        schemes = uidkeys.select(uid_schemes)
    except (uidkeys.UnknownUidSchemeError, uidkeys.SecretKeyRequiredError) as e:
        print_error(str(e))
        raise SystemExit(1)

    if out_file:
        _refuse_overwrite(out_file, force)
    if output_keys_file:
        _refuse_overwrite(output_keys_file, force)

    base = load_keys(keyfiles or [str(default_keyfile())])
    try:
        named = _select_dicts(dict_names)
    except UnknownDictError as e:
        print_error(str(e))
        raise SystemExit(1)
    extra = load_keys(dict_files) if dict_files else []
    plan = CandidatePlan(base, named, extra)
    if not plan:
        print_error("no keys loaded — check --keys/--dict/--dict-file")
        raise SystemExit(1)

    key_types = ["A", "B"] if key_type.lower() == "both" else [key_type.upper()]
    total = sectors * len(key_types)

    level = _verbosity(ctx, verbose)

    # The on-device sweep can only use the device's own candidate universe;
    # the host-only sources below make the request inexpressible on it.
    host_only: List[str] = []
    if named:
        host_only.append("--dict")
    if extra:
        host_only.append("--dict-file")
    if schemes:
        host_only.append("--uid-derived")
    explicit_keys = base if keyfiles else None
    use_device, device_keys = _plan_device_sweep(
        mode.lower(), host_only, key_type, explicit_keys
    )

    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        if not as_json and (named or extra):
            sources = [nd.name for nd in named]
            sources += [f"file:{f}" for f in dict_files]
            print_info(f"dictionaries: base + {', '.join(sources)}")

        # The clock starts with the sweep itself, not with the connection: the
        # reported time and rate are the sweep's.
        start_time = time.monotonic()
        sweep_mode = "host"
        result: Optional[_SweepResult] = None
        if use_device:
            result = _sweep_on_device(
                link,
                target,
                sectors,
                device_keys,
                key_types,
                as_json,
                timeout,
                mode,
                fresh,
            )
            if result is not None:
                sweep_mode = "device"
        if result is None:
            # Either the host sweep was chosen, or the flashed firmware has no
            # on-device sweep and we fell back to it.
            result = _run_host_sweep(
                link,
                target,
                plan,
                key_types,
                sectors,
                schemes,
                uid_max,
                timeout,
                as_json,
                start_time,
                total,
            )

        found = result.found
        known = result.known
        attempts = result.attempts
        naive_attempts = result.naive_attempts
        interrupted = result.interrupted

        # Trailer-read fallback (still inside the open session): for sectors
        # the dictionary opened with key A but not key B, try to read key B
        # straight off the card. Not the Crypto-1 nested attack — that needs
        # nonce capture this reader can't do — but it recovers key B for free
        # on cards that leave it readable. See _recover_key_b_via_trailer.
        #
        # Skipped when the DEVICE already did it (Fase 4, tarea 4.4): the
        # on-device sweep reads the trailer itself, so repeating it here would
        # cost one USB round-trip per sector for nothing.
        if not interrupted and not result.device_trailer:
            _trailer_read_pass(link, found, known, key_types, sectors, as_json)

    if interrupted and not as_json:
        print_warning("interrupted — showing partial results")

    recovered = sum(1 for v in found.values() if v)
    exposed_sectors = len({s for (s, _kt), v in found.items() if v})

    # Auths saved by known-key reuse + per-sector early cut vs. a naive sweep of
    # the same candidate universe (Fase 1). Honest even on interrupt: both
    # counters stop together, so the ratio reflects the work actually done.
    auths_saved = naive_attempts - attempts
    saved_pct = (auths_saved / naive_attempts * 100.0) if naive_attempts else 0.0

    if out_file:
        try:
            _write_keyfile(out_file, list(known))
        except OSError as e:
            print_error(f"could not write {out_file}: {e}")
            raise SystemExit(1)
        if not as_json:
            print_info(f"wrote {out_file}")

    if output_keys_file:
        try:
            _write_sector_keyfile(output_keys_file, sectors, found)
        except OSError as e:
            print_error(f"could not write {output_keys_file}: {e}")
            raise SystemExit(1)
        if not as_json:
            print_info(f"wrote {output_keys_file}")

    if as_json:
        rows = []
        for s in range(sectors):
            key_a = found.get((s, "A")) if "A" in key_types else None
            key_b = found.get((s, "B")) if "B" in key_types else None
            rows.append({"sector": s, "key_a": key_a, "key_b": key_b})
        stats = {
            "attempts": attempts,
            "naive_attempts": naive_attempts,
            "auths_saved": auths_saved,
            "saved_pct": round(saved_pct, 1),
            "interrupted": interrupted,
            "partial": result.partial,
            "mode": sweep_mode,
        }
        # The board's own wall-clock split (device mode only): where the time
        # went inside the sweep. Absent in host mode (the host knows its own
        # timings) and on a firmware that does not report them.
        if result.auth_ms is not None:
            stats["auth_ms"] = result.auth_ms
            stats["reselect_ms"] = result.reselect_ms
            stats["trailer_ms"] = result.trailer_ms
        if result.cached is not None:
            stats["cached"] = result.cached
        print(
            json.dumps(
                {
                    "sectors": rows,
                    "recovered": recovered,
                    "total": total,
                    "stats": stats,
                }
            )
        )
        raise SystemExit(0 if recovered == total else 1)

    console.print("")
    table = Table(title=f"MifareClassic check @ {target}", header_style="cyan bold")
    table.add_column("Sector", justify="right")
    table.add_column("Key A")
    table.add_column("Key B")
    for s in range(sectors):
        row = []
        for kt in ("A", "B"):
            if kt not in key_types:
                row.append("[dim]—[/dim]")
                continue
            key = found.get((s, kt))
            row.append(f"[green]{key}[/green]" if key else r"[dim]\[unknown][/dim]")
        table.add_row(str(s), *row)
    console.print(table)

    console.print("")
    print_info(
        f"{recovered}/{total} keys recovered — card exposes {exposed_sectors}/"
        f"{sectors} sectors with known keys"
    )

    duration = time.monotonic() - start_time
    console.print("")
    print_subtitle("Timing summary")
    _print_field("total time", f"{duration:.1f}s")
    _print_field("sweep mode", "on-device" if sweep_mode == "device" else "host")
    _print_field("attempts", str(attempts))
    _print_field(
        "avg rate", f"{attempts / duration:.1f} keys/s" if duration > 0 else "—"
    )
    _print_field("keys identified", f"{recovered}/{total}")
    _print_field("failures", str(total - recovered))
    _print_field("sectors exposed", f"{exposed_sectors}/{sectors}")
    _print_field("naive sweep", f"{naive_attempts} auths")
    _print_field("auths saved", f"{auths_saved} ({saved_pct:.1f}% vs. naive sweep)")
    if result.auth_ms is not None:
        # Where the BOARD spent the time (device mode): the same number of
        # authentications, but the split between the authentication itself and
        # the re-select that follows a failure.
        _print_field("on-device auth time", f"{result.auth_ms} ms")
        _print_field("on-device reselect time", f"{result.reselect_ms} ms")
        if attempts:
            _print_field(
                "per auth",
                f"{result.auth_ms / attempts:.0f} ms auth + "
                f"{(result.reselect_ms or 0) / attempts:.0f} ms reselect",
            )
        if result.cached:
            print_dim(
                "the device reused its own sweep (it had already swept this "
                "card on detection) — no authentication was repeated"
            )
    if interrupted:
        _print_field("status", "[yellow]interrupted — partial results[/yellow]")
    if result.partial:
        _print_field(
            "status",
            "[yellow]partial — the device's sweep stopped before it covered "
            "every sector[/yellow]",
        )

    raise SystemExit(0 if recovered == total else 1)
