#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

# `mifare restore` — write a `mifare dump` JSON back to a magic card (gen2/CUID).
# docs/CLI_IMPROVEMENTS_MifareRestore.md §3 (F1: probe + block-0 confirmation +
# per-sector data-block writes; F2: trailer writes with access-bit validation
# (§3.4) + --skip-trailers).

import json
from datetime import datetime, timezone
from contextlib import nullcontext
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
from ...utils.detection_cli import verbosity as _verbosity
from ...utils.output import (
    console,
    make_tracer,
    print_error,
    print_info,
    print_warning,
)
from .access_bits import _NEVER, SectorAccessBits, parse_access_bits
from .common import (
    _MIFARE_BLOCK_HEX_LEN,
    _MIFARE_CHECK_MAX_SECTORS,
    _MIFARE_DATA_BLOCK_INDICES,
    _MIFARE_KEY_HEX_LEN,
    _MIFARE_TRAILER_AC_LEN,
    _MIFARE_TRAILER_BLOCK_INDEX,
    _mifare_validate_hex,
    _sector_first_block,
)
from .session import _mifare_session, _MIFARE_TIMEOUT_OPTION, _auth_sector


_MIFARE_ZERO_KEY = "0" * _MIFARE_KEY_HEX_LEN
# The 3 access-condition bytes (C1/C2/C3) sit right after key A in a trailer
# block: hex chars 12-17 (byte 9, the GPB, is not an access condition).
_MIFARE_TRAILER_AC_COND = slice(_MIFARE_KEY_HEX_LEN, _MIFARE_KEY_HEX_LEN + 6)

_RESTORE_BLOCK0_WARNING = (
    "Writing block 0 rewrites the card's UID/BCC/SAK/ATQA — its identity. Only "
    "do this on a magic card (gen2/CUID) you own or have permission to clone; on "
    "a genuine MIFARE Classic block 0 is factory-OTP and the write will fail (in "
    "the worst case bricking it). Continue?"
)


def _load_restore_dump(path: str) -> Dict[str, object]:
    """Load and shape-check a `mifare dump` JSON for `restore`. Requires a
    `sectors` list of `{sector, blocks}` entries, each with exactly 4 blocks of
    32 hex chars — the same canonical shape `mifare dump --out` produces."""
    try:
        with open(path, encoding="utf-8") as f:
            dump = json.load(f)
    except OSError as e:
        print_error(f"could not read {path}: {e}")
        raise SystemExit(1)
    except json.JSONDecodeError as e:
        print_error(f"{path} is not valid JSON: {e}")
        raise SystemExit(1)

    if not isinstance(dump, dict) or not isinstance(dump.get("sectors"), list):
        print_error(f"{path} is not a mifare dump JSON (no 'sectors' list)")
        raise SystemExit(1)
    for entry in dump["sectors"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("sector"), int)
            or not isinstance(entry.get("blocks"), list)
        ):
            print_error(f"{path}: malformed sector entry: {entry!r}")
            raise SystemExit(1)
        blocks = entry["blocks"]
        if len(blocks) != 4 or any(
            not isinstance(b, str)
            or _mifare_validate_hex(b, _MIFARE_BLOCK_HEX_LEN, "block")
            for b in blocks
        ):
            print_error(
                f"{path}: sector {entry['sector']} must have 4 blocks of "
                f"{_MIFARE_BLOCK_HEX_LEN} hex chars each"
            )
            raise SystemExit(1)
    return dump


def _restore_sector_keys(blocks: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """Pull the write keys out of a dump sector's trailer (block 3): key A is
    the first 6 bytes, key B the last 6. An all-zero slot means "unknown" — the
    card reads key A (and often key B) back as zeros — so it's returned as None
    rather than attempted as a real key."""
    trailer = blocks[_MIFARE_TRAILER_BLOCK_INDEX]
    key_a = trailer[:_MIFARE_KEY_HEX_LEN]
    key_b = trailer[_MIFARE_KEY_HEX_LEN + _MIFARE_TRAILER_AC_LEN :]
    return (
        key_a if key_a.upper() != _MIFARE_ZERO_KEY else None,
        key_b if key_b.upper() != _MIFARE_ZERO_KEY else None,
    )


def _restore_block0(link, block0_hex: str) -> Tuple[bool, str]:
    """Non-destructively probe sector 0's block 0, then write it (§3.1).

    Reads the current block 0 and writes it back *unchanged*: only if that echo
    write returns +OK is the card's block 0 actually writable (a magic card),
    so it's safe to write the dump's UID over it. A genuine card (factory-OTP
    block 0) or a wrong key fails the probe, and we abort without ever changing
    the UID. Returns ``(written, note)``."""
    r = link.command("mifare read 0")
    if not r.ok:
        return False, f"block 0 probe read failed: {r.message}"
    current = r.data.get("mifare_data", "").partition(" ")[2]
    if len(current) != _MIFARE_BLOCK_HEX_LEN:
        return False, "block 0 probe read returned no data"

    probe = link.command(f"mifare write 0 {current.upper()}")
    if not probe.ok:
        return False, (
            "block 0 is not writable — genuine card or wrong key; refusing to "
            f"write UID ({probe.message})"
        )
    w = link.command(f"mifare write 0 {block0_hex.upper()}")
    if not w.ok:
        return False, f"block 0 write failed: {w.message}"
    return True, "ok"


def _trailer_freezes_sector(access: SectorAccessBits) -> bool:
    """True if this trailer's access bits leave the trailer itself unwritable
    by any key — no way to rewrite the keys or the access bits ever again, so
    the sector's config is permanently frozen (§3.4). A faithful clone may want
    this (the source card had it), so it's a warning, not a refusal."""
    t = access.trailer
    return t.key_a_write == _NEVER and t.key_b_write == _NEVER and t.ac_write == _NEVER


def _restore_trailer(link, base: int, trailer_hex: str) -> Tuple[bool, str]:
    """Write a sector's trailer (block 3) last, after validating its access
    bits (§3.4). Returns ``(written, note)``.

    Malformed/invalid access bits (the C-bits and their stored inverses
    disagree) are *refused*: many chips fall back to the most restrictive
    config on invalid bits, which would permanently lock the sector — so the
    trailer is left as the card had it and the sector is reported as a failure.
    A valid but self-locking config is written (it may be an intentional part
    of the clone) with a warning."""
    access = parse_access_bits(trailer_hex[_MIFARE_TRAILER_AC_COND])
    if access is None or not access.valid:
        return (
            False,
            "trailer not written: invalid access bits (would risk locking the sector)",
        )
    warning = (
        "trailer access bits leave it permanently unwritable — sector config frozen"
        if _trailer_freezes_sector(access)
        else ""
    )
    w = link.command(
        f"mifare write {base + _MIFARE_TRAILER_BLOCK_INDEX} {trailer_hex.upper()}"
    )
    if not w.ok:
        return False, f"trailer write failed: {w.message}"
    return True, warning


def _restore_one_sector(
    link,
    sector: int,
    blocks: List[str],
    timeout: float,
    first: bool,
    write_block0: bool,
    skip_trailers: bool,
) -> Tuple[int, bool, str, str]:
    """Authenticate SECTOR (key A, falling back to key B) and write its data
    blocks, then (unless `skip_trailers`) its trailer last (§3.3c). Block 0 of
    sector 0 is written only if `write_block0`, behind the non-destructive
    probe of `_restore_block0`.

    Returns ``(written, block0_written, reason, warning)``: `written` counts
    the blocks written (data + trailer), `reason` is "ok" or the failure
    message, `warning` is a non-fatal note (e.g. a self-locking trailer) or "".
    A write that's denied stops this sector and returns its reason — the caller
    keeps going with the next sector (partials are first class, §3.3.4)."""
    base = _sector_first_block(sector)
    key_a, key_b = _restore_sector_keys(blocks)
    if not key_a and not key_b:
        return 0, False, "no write key in dump trailer", ""

    used_kt, reason = _auth_sector(link, sector, key_a, key_b, timeout, first)
    if used_kt is None:
        return 0, False, reason, ""

    written = 0
    block0_written = False
    for i in _MIFARE_DATA_BLOCK_INDICES:
        if sector == 0 and i == 0:
            if not write_block0:
                continue
            ok_b0, note = _restore_block0(link, blocks[0])
            if not ok_b0:
                return written, False, note, ""
            block0_written = True
            written += 1
            continue
        w = link.command(f"mifare write {base + i} {blocks[i].upper()}")
        if not w.ok:
            return written, block0_written, w.message, ""
        written += 1

    if not skip_trailers:
        ok_t, note_t = _restore_trailer(link, base, blocks[_MIFARE_TRAILER_BLOCK_INDEX])
        if not ok_t:
            return written, block0_written, note_t, ""
        written += 1
        return written, block0_written, "ok", note_t
    return written, block0_written, "ok", ""


@click.command("restore")
@click.option(
    "--dump",
    "dump_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    metavar="FILE",
    help="A canonical `mifare dump --out` JSON to write back to the card.",
)
@click.option(
    "--sectors",
    type=click.IntRange(1, _MIFARE_CHECK_MAX_SECTORS),
    default=None,
    metavar="N",
    help="Restore only sectors 0..N-1 (default: every sector in the dump).",
)
@click.option(
    "--write-block0",
    is_flag=True,
    help="Also write sector 0's block 0 (UID/BCC/SAK/ATQA) — the card's "
    "identity. Off by default; only works on a magic card (gen2/CUID), never "
    "on a genuine card. A non-destructive probe runs first (see --help notes).",
)
@click.option(
    "--skip-trailers",
    is_flag=True,
    help="Don't write sector trailers (keys + access bits), only data blocks. "
    "Recommended for a first run: it clones the card's contents while leaving "
    "the target's trailers untouched, so a mismatched access-bit layout can't "
    "lock a sector. Off by default (trailers ARE written).",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Skip the interactive confirmation before writing block 0 (for "
    "scripted use). Still requires --write-block0.",
)
@click.option(
    "--json", "as_json", is_flag=True, help="Emit the restore result as JSON."
)
@_MIFARE_TIMEOUT_OPTION
@device_options
@click.pass_context
def mifare_restore_cmd(
    ctx,
    dump_file,
    sectors,
    write_block0,
    skip_trailers,
    yes,
    as_json,
    timeout,
    verbose,
    port,
    device_id,
):
    """Write a `mifare dump` JSON back to a magic card, block by block.

    \b
    ⚠ AUTHORIZED USE ONLY. Clone only cards you own or have explicit
    permission to clone, and only onto a magic card (gen2/CUID) — never a
    genuine MIFARE Classic. On a genuine card block 0 is factory-OTP: writing
    it fails, and in the worst case bricks the card permanently.

    Reads the canonical JSON `mifare dump --out` produces and writes each
    sector's data blocks back after authenticating with the sector's own keys
    (from the dump trailer). Sector trailers (keys + access bits) are written
    last, after the data blocks — pass --skip-trailers to leave them alone
    (recommended for a first run). A trailer whose access bits are invalid is
    refused (writing them could permanently lock the sector); a valid but
    self-locking layout is written with a warning. Sector 0's block 0 (the UID)
    is skipped unless --write-block0, and even then only after a
    non-destructive probe proves the card's block 0 is actually writable. A
    sector whose key can't write is recorded as a failure — it never aborts the
    rest. Ctrl-C stops and reports what was written so far.
    """
    dump = _load_restore_dump(dump_file)
    entries = sorted(dump["sectors"], key=lambda e: e["sector"])
    limit = sectors if sectors is not None else dump.get("sectors_total")
    if isinstance(limit, int):
        entries = [e for e in entries if e["sector"] < limit]
    if not entries:
        print_error("nothing to restore: no sectors in the dump within range")
        raise SystemExit(1)

    if write_block0 and not yes and not as_json:
        if not click.confirm(_RESTORE_BLOCK0_WARNING, default=False):
            print_warning("aborted — block 0 not written")
            raise SystemExit(1)

    level = _verbosity(ctx, verbose)
    results: List[Dict[str, object]] = []
    interrupted = False
    block0_written = False

    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        if not as_json:
            print_info(
                f"Restoring {len(entries)} sector(s) to {target} — Ctrl-C for "
                "partial results"
            )
        progress = (
            None
            if as_json
            else Progress(
                SpinnerColumn(style="cyan"),
                TextColumn("[cyan]sector {task.fields[sector]:>2}[/cyan]"),
                BarColumn(bar_width=24, complete_style="cyan", finished_style="cyan"),
                TextColumn(
                    "[dim]{task.fields[ok]} ok · {task.fields[failed]} failed[/dim]"
                ),
                TimeRemainingColumn(),
                TimeElapsedColumn(),
                console=console,
                transient=True,
            )
        )
        first = True
        try:
            with progress or nullcontext():
                task = (
                    progress.add_task("", total=len(entries), sector=0, ok=0, failed=0)
                    if progress
                    else None
                )
                ok_count = 0
                failed_count = 0
                for entry in entries:
                    s = entry["sector"]
                    written, wrote_b0, reason, warning = _restore_one_sector(
                        link,
                        s,
                        entry["blocks"],
                        timeout,
                        first,
                        write_block0,
                        skip_trailers,
                    )
                    first = False
                    block0_written = block0_written or wrote_b0
                    entry_result: Dict[str, object] = {
                        "sector": s,
                        "written": written,
                        "block0": wrote_b0,
                        "reason": reason,
                    }
                    if warning:
                        entry_result["warning"] = warning
                    results.append(entry_result)
                    if reason == "ok":
                        ok_count += 1
                    else:
                        failed_count += 1
                    if progress:
                        progress.update(
                            task,
                            advance=1,
                            sector=s,
                            ok=ok_count,
                            failed=failed_count,
                        )
        except KeyboardInterrupt:
            interrupted = True

    if interrupted and not as_json:
        print_warning("interrupted — showing partial results")

    ok_sectors = [r for r in results if r["reason"] == "ok"]
    failed_sectors = [r for r in results if r["reason"] != "ok"]
    result = {
        "target_sectors": len(entries),
        "sectors_written": len(ok_sectors),
        "block0_written": block0_written,
        "trailers_skipped": skip_trailers,
        "interrupted": interrupted,
        "source_dump": dump_file,
        "written_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sectors": results,
        "failed_sectors": failed_sectors,
    }
    complete = not interrupted and not failed_sectors

    if as_json:
        print(json.dumps(result))
        raise SystemExit(0 if complete else 1)

    console.print("")
    table_out = Table(
        title=f"MifareClassic restore @ {target}", header_style="cyan bold"
    )
    table_out.add_column("Sector", justify="right")
    table_out.add_column("Status")
    table_out.add_column("Blocks")
    table_out.add_column("Reason")
    for r in results:
        if r["reason"] == "ok":
            warn = r.get("warning")
            table_out.add_row(
                str(r["sector"]),
                "[green]OK[/green]",
                str(r["written"]),
                f"[yellow]⚠ {warn}[/yellow]" if warn else "",
            )
        else:
            table_out.add_row(
                str(r["sector"]),
                "[red]FAILED[/red]",
                str(r["written"]),
                r["reason"],
            )
    console.print(table_out)

    console.print("")
    print_info(
        f"{len(ok_sectors)}/{len(entries)} sectors written"
        + (" — block 0 written" if block0_written else "")
        + (" — trailers skipped" if skip_trailers else "")
    )
    for r in results:
        if r.get("warning"):
            print_warning(f"sector {r['sector']}: {r['warning']}")
    if interrupted:
        print_warning("restore incomplete — interrupted before finishing")

    raise SystemExit(0 if complete else 1)
