#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

# `mifare check` — dictionary attack: try known keys against every sector
# and report which ones open. docs/CLI_IMPROVEMENTS_MifareCheck.md §6.3.

import json
import time
from collections import OrderedDict
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
from .keyfile import default_keyfile, load_keys
from .common import (
    _MIFARE_BLOCK_HEX_LEN,
    _MIFARE_CHECK_MAX_SECTORS,
    _MIFARE_HEX_RE,
    _MIFARE_KEY_HEX_LEN,
    _MIFARE_TRAILER_AC_LEN,
    _MIFARE_TRAILER_KEY_LEN,
    _sector_first_block,
)
from .session import (
    _MIFARE_TIMEOUT_OPTION,
    _mifare_session,
    _run_mifare_command,
)


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
    _TRAILER_HEX_START + _MIFARE_TRAILER_KEY_LEN,
    _TRAILER_HEX_START + _MIFARE_TRAILER_KEY_LEN + 6,
)
_TRAILER_KEYB_HEX = slice(
    _TRAILER_HEX_START + _MIFARE_TRAILER_KEY_LEN + _MIFARE_TRAILER_AC_LEN,
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
    sectors,
    key_type,
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
    if out_file:
        _refuse_overwrite(out_file, force)
    if output_keys_file:
        _refuse_overwrite(output_keys_file, force)

    dictionary = load_keys(keyfiles or [str(default_keyfile())])
    if not dictionary:
        print_error("no keys loaded — check --keys")
        raise SystemExit(1)

    key_types = ["A", "B"] if key_type.lower() == "both" else [key_type.upper()]
    total = sectors * len(key_types)

    level = _verbosity(ctx, verbose)
    found: Dict[Tuple[int, str], Optional[str]] = {}
    known: "OrderedDict[str, None]" = OrderedDict()
    interrupted = False

    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        if not as_json:
            print_info(
                f"Checking {target} — {sectors} sector(s) x {len(key_types)} key "
                f"type(s), {len(dictionary)} keys — Ctrl-C for partial results"
            )
        total_attempts = total * len(dictionary)
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
                TextColumn(
                    "[dim]{task.fields[tried]}/{task.fields[key_total]} keys[/dim]"
                ),
                TextColumn("[dim]{task.fields[rate]:.0f} keys/s[/dim]"),
                TimeRemainingColumn(),
                TimeElapsedColumn(),
                TextColumn(
                    "[dim]{task.fields[recovered]}/{task.fields[pairs]} "
                    "recovered[/dim]"
                ),
                console=console,
                transient=True,
            )
        )
        first = True
        attempts = 0
        start_time = time.monotonic()
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
                        key_total=len(dictionary),
                        rate=0.0,
                    )
                    if progress
                    else None
                )
                for s in range(sectors):
                    block = _sector_first_block(s)
                    for kt in key_types:
                        candidates = list(known) + [
                            k for k in dictionary if k not in known
                        ]
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
        except KeyboardInterrupt:
            interrupted = True

        # Trailer-read fallback (still inside the open session): for sectors
        # the dictionary opened with key A but not key B, try to read key B
        # straight off the card. Not the Crypto-1 nested attack — that needs
        # nonce capture this reader can't do — but it recovers key B for free
        # on cards that leave it readable. See _recover_key_b_via_trailer.
        if not interrupted and "B" in key_types:
            targets = [
                (s, key_a)
                for s in range(sectors)
                if found.get((s, "B")) is None
                and (key_a := found.get((s, "A"))) is not None
            ]
            if targets and not as_json:
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
                        print_success(
                            f"sector {s} key B recovered via trailer read: {key_b}"
                        )
                elif not as_json:
                    print_dim(f"  sector {s} key B not recovered — {reason}")

    if interrupted and not as_json:
        print_warning("interrupted — showing partial results")

    recovered = sum(1 for v in found.values() if v)
    exposed_sectors = len({s for (s, _kt), v in found.items() if v})

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
        print(json.dumps({"sectors": rows, "recovered": recovered, "total": total}))
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
    _print_field("attempts", str(attempts))
    _print_field(
        "avg rate", f"{attempts / duration:.1f} keys/s" if duration > 0 else "—"
    )
    _print_field("keys identified", f"{recovered}/{total}")
    _print_field("failures", str(total - recovered))
    _print_field("sectors exposed", f"{exposed_sectors}/{sectors}")
    if interrupted:
        _print_field("status", "[yellow]interrupted — partial results[/yellow]")

    raise SystemExit(0 if recovered == total else 1)
