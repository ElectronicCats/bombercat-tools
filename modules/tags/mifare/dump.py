#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

# `mifare dump` — read every sector of a card in one session and save it to
# canonical JSON. Coexists with `mifare sector` (one sector, interactive);
# `dump` is batch/non-interactive and never aborts on a bad sector — see
# docs/CLI_IMPROVEMENTS_MifareDump.md §1, §5.

import json
from datetime import datetime, timezone
from contextlib import nullcontext
from typing import Dict, List, Optional

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
    refuse_overwrite as _refuse_overwrite,
    verbosity as _verbosity,
)
from ...utils.output import (
    console,
    make_tracer,
    print_error,
    print_info,
    print_warning,
)
from .block0 import parse_block0
from .keyfile import SectorKeyfileError, load_sector_keys
from .common import (
    _MIFARE_BLOCK_HEX_LEN,
    _MIFARE_CHECK_MAX_SECTORS,
)
from .session import _mifare_session, _MIFARE_TIMEOUT_OPTION, _read_one_sector
from .display import _substitute_trailer_keys, _print_decoded_sector


def _dump_size_label(sectors: int) -> str:
    """Card-size label for the `sectors_total` requested — same 1K/2K
    convention as `mifare check`'s `--sectors` help text (4-block sectors
    only; 4K's 16-block sectors aren't supported, see `_sector_first_block`)."""
    return {16: "1K", 32: "2K"}.get(sectors, f"{sectors} sectors")


def _write_dump_json(path: str, dump: Dict[str, object]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dump, f, indent=2)
        f.write("\n")


def _dump_blocks_hex(
    sector_results: List[Dict[str, object]], sectors: int
) -> List[str]:
    """Full sectors*4 block list, hex-encoded, in card order. Sectors missing
    from `sector_results` (gaps) are filled with zeroed blocks — required by
    the raw --mfd/--eml formats, which can't distinguish "not read" from
    real zeros (see docs/CLI_IMPROVEMENTS_MifareDump.md §6.2)."""
    zero_block = "0" * _MIFARE_BLOCK_HEX_LEN
    blocks = [zero_block] * (sectors * 4)
    for r in sector_results:
        s = r["sector"]
        for i, block in enumerate(r["blocks"]):
            blocks[s * 4 + i] = block
    return blocks


@click.command("dump")
@click.option(
    "-k",
    "--keys-file",
    "keys_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    metavar="FILE",
    help="A `sector:keyA:keyB` file (see `mifare check --output-keys`). A "
    "sector missing from it, or blank for both keys, is dumped as a gap.",
)
@click.option(
    "--sectors",
    type=click.IntRange(1, _MIFARE_CHECK_MAX_SECTORS),
    default=16,
    show_default=True,
    help=f"Number of sectors to dump (16 = 1K). Max {_MIFARE_CHECK_MAX_SECTORS} "
    "— 4K's 16-block sectors (32-39) aren't supported yet.",
)
@click.option(
    "--out",
    "out_file",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    metavar="FILE",
    help="Write the dump as canonical JSON (uid, per-sector blocks with real "
    "keys substituted into the trailer, and failed_sectors for any gap).",
)
@click.option(
    "--mfd",
    "mfd_file",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    metavar="FILE",
    help="Write a raw binary dump (Proxmark/mfoc `hf mf restore` compatible). "
    "Unread sectors are filled with zeros — not a canonical format, see --out.",
)
@click.option(
    "--eml",
    "eml_file",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    metavar="FILE",
    help="Write a hex-per-line dump (Proxmark `hf mf eload` compatible). "
    "Unread sectors are filled with zeros — not a canonical format, see --out.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite --out/--mfd/--eml if they already exist.",
)
@click.option(
    "--json", "as_json", is_flag=True, help="Emit the dump as JSON on stdout."
)
@click.option(
    "--decode",
    is_flag=True,
    help="After the status table, print each read sector's raw blocks with an "
    "ASCII column and the dissected block 0 (UID/BCC/SAK/ATQA). Human view "
    "only — the JSON/file output is unchanged. Ignored with --json.",
)
@_MIFARE_TIMEOUT_OPTION
@device_options
@click.pass_context
def mifare_dump_cmd(
    ctx,
    keys_file,
    sectors,
    out_file,
    mfd_file,
    eml_file,
    force,
    as_json,
    decode,
    timeout,
    verbose,
    port,
    device_id,
):
    """Read every sector of a card in one session and dump it to file/stdout.

    Unlike `mifare sector` (one sector, interactive), `dump` reads the whole
    card in a single batch pass. A sector with no usable key, or whose read
    fails, is recorded as a gap in `failed_sectors` — it never aborts the
    rest of the card. Ctrl-C dumps whatever was read so far.
    """
    if out_file:
        _refuse_overwrite(out_file, force)
    if mfd_file:
        _refuse_overwrite(mfd_file, force)
    if eml_file:
        _refuse_overwrite(eml_file, force)

    try:
        table = load_sector_keys(keys_file)
    except OSError as e:
        print_error(f"could not read {keys_file}: {e}")
        raise SystemExit(1)
    except SectorKeyfileError as e:
        print_error(str(e))
        raise SystemExit(1)

    level = _verbosity(ctx, verbose)
    sector_results: List[Dict[str, object]] = []
    failed: List[Dict[str, object]] = []
    interrupted = False
    uid: Optional[str] = None

    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        if not as_json:
            print_info(
                f"Dumping {target} — {sectors} sector(s) — Ctrl-C for partial "
                "results"
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
                    progress.add_task("", total=sectors, sector=0, ok=0, failed=0)
                    if progress
                    else None
                )
                for s in range(sectors):
                    key_a, key_b = table.get(s, (None, None))
                    data_hex, used_kt, reason = _read_one_sector(
                        link, s, key_a, key_b, timeout, first=first
                    )
                    first = False
                    if data_hex is not None:
                        blocks = [
                            data_hex[i : i + _MIFARE_BLOCK_HEX_LEN]
                            for i in range(0, len(data_hex), _MIFARE_BLOCK_HEX_LEN)
                        ]
                        shown_a, ac, shown_b = _substitute_trailer_keys(
                            blocks[3], key_a, key_b
                        )
                        blocks[3] = f"{shown_a}{ac}{shown_b}"
                        sector_results.append(
                            {"sector": s, "opened_with": used_kt, "blocks": blocks}
                        )
                        if s == 0:
                            b0 = parse_block0(blocks[0])
                            if b0 is not None:
                                uid = b0.uid
                    else:
                        failed.append({"sector": s, "reason": reason})
                    if progress:
                        progress.update(
                            task,
                            advance=1,
                            sector=s,
                            ok=len(sector_results),
                            failed=len(failed),
                        )
        except KeyboardInterrupt:
            interrupted = True

    if interrupted and not as_json:
        print_warning("interrupted — showing partial results")

    dump = {
        "uid": uid,
        "size": _dump_size_label(sectors),
        "sectors_read": len(sector_results),
        "sectors_total": sectors,
        "read_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_keyfile": keys_file,
        "sectors": sector_results,
        "failed_sectors": failed,
    }

    if out_file:
        try:
            _write_dump_json(out_file, dump)
        except OSError as e:
            print_error(f"could not write {out_file}: {e}")
            raise SystemExit(1)
        if not as_json:
            print_info(f"wrote {out_file}")

    if mfd_file or eml_file:
        blocks_hex = _dump_blocks_hex(sector_results, sectors)
        if mfd_file:
            try:
                with open(mfd_file, "wb") as f:
                    f.write(b"".join(bytes.fromhex(b) for b in blocks_hex))
            except OSError as e:
                print_error(f"could not write {mfd_file}: {e}")
                raise SystemExit(1)
            if not as_json:
                print_info(f"wrote {mfd_file}")
        if eml_file:
            try:
                with open(eml_file, "w", encoding="utf-8") as f:
                    f.write("\n".join(blocks_hex) + "\n")
            except OSError as e:
                print_error(f"could not write {eml_file}: {e}")
                raise SystemExit(1)
            if not as_json:
                print_info(f"wrote {eml_file}")

    complete = not interrupted and len(sector_results) == sectors

    if as_json:
        print(json.dumps(dump))
        raise SystemExit(0 if complete else 1)

    console.print("")
    table_out = Table(title=f"MifareClassic dump @ {target}", header_style="cyan bold")
    table_out.add_column("Sector", justify="right")
    table_out.add_column("Status")
    table_out.add_column("Opened with")
    table_out.add_column("Reason")
    read_by_sector = {r["sector"]: r for r in sector_results}
    failed_by_sector = {f["sector"]: f for f in failed}
    for s in range(sectors):
        if s in read_by_sector:
            r = read_by_sector[s]
            table_out.add_row(str(s), "[green]OK[/green]", r["opened_with"], "")
        elif s in failed_by_sector:
            table_out.add_row(
                str(s), "[red]FAILED[/red]", "—", failed_by_sector[s]["reason"]
            )
        else:
            table_out.add_row(str(s), "[dim]—[/dim]", "—", "not read")
    console.print(table_out)

    console.print("")
    print_info(
        f"{len(sector_results)}/{sectors} sectors read"
        + (f" — uid {uid}" if uid else "")
    )
    if interrupted:
        print_warning("dump incomplete — interrupted before finishing")

    if decode and sector_results:
        console.print("\n[cyan bold]Decoded sectors[/cyan bold]")
        for entry in sector_results:
            _print_decoded_sector(entry)

    raise SystemExit(0 if complete else 1)
