#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

# `mifare dump` — read every sector of a card in one session and save it to
# canonical JSON. Coexists with `mifare sector` (one sector, interactive);
# `dump` is batch/non-interactive and never aborts on a bad sector — see
# docs/CLI_IMPROVEMENTS_MifareDump.md §1, §5.

import json
import os
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
    print_dim,
    print_error,
    print_info,
    print_warning,
)
from .block0 import parse_block0
from .device_results import (
    DeviceResultsError,
    DeviceResultsUnsupported,
    DeviceStoredResult,
    run_device_stored_result,
    run_device_stored_results_all,
)
from .device_sweep import (
    DeviceDumpError,
    DeviceDumpNoSweep,
    DeviceDumpUnsupported,
    run_device_dump,
)
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


def _dump_reason(entry) -> str:
    """Why a sector could not be read, in the host's own wording.

    The device reports short tokens ("nokey", "readfail", "removed"); the
    host-driven dump says "no key available" for the same case. They are
    translated here so the JSON is the same whichever side read the card.
    """
    reason = (entry.reason if entry else None) or "not read"
    if reason == "nokey":
        return "no key available"
    return reason


def _run_device_dump(link, sectors, timeout, as_json, mode):
    """Run the on-device dump and return its result, or None when the host
    must fall back to its own per-sector dump (only possible when a keyfile
    is in hand)."""
    if not as_json:
        print_info(
            "Dumping on the device — it reads the card with the keys its own "
            "sweep recovered (one round-trip for the whole card)"
        )
    try:
        return run_device_dump(
            link,
            sectors,
            timeout=timeout,
            tap_timeout=timeout,
            quiet=as_json,
        )
    except DeviceDumpNoSweep:
        if mode == "device":
            print_error(
                "the device has no sweep results for this card — run "
                "`mifare check` first"
            )
            raise SystemExit(1)
        print_dim(
            "the device has no sweep results for this card — using the "
            "host-driven dump"
        )
        return None
    except DeviceDumpUnsupported as e:
        if mode == "device":
            print_error(
                f"the flashed firmware has no on-device dump ({e}) — update the "
                "firmware or use --mode host"
            )
            raise SystemExit(1)
        print_dim(
            "the device did not run the dump (older firmware?) — using the "
            "host-driven dump"
        )
        return None
    except DeviceDumpError as e:
        print_error(f"device dump failed: {e}")
        raise SystemExit(1)


def _run_stored_result(link, index: int) -> DeviceStoredResult:
    """Fetch a result the board stored while working on its own.

    The board re-emits it in the live dump's format, so the canonical dump
    the host rebuilds is the same one it would have got with the card on the
    reader — gaps included."""
    try:
        return run_device_stored_result(link, index)
    except DeviceResultsUnsupported as e:
        print_error(
            f"the flashed firmware does not keep results ({e}) — update the " "firmware"
        )
        raise SystemExit(1)
    except DeviceResultsError as e:
        print_error(f"the stored result could not be read: {e}")
        raise SystemExit(1)


def _run_stored_results_all(link) -> List[DeviceStoredResult]:
    """Fetch every stored result in one round-trip (`mifare results show all`)."""
    try:
        return run_device_stored_results_all(link)
    except DeviceResultsUnsupported as e:
        print_error(
            f"the flashed firmware does not keep results ({e}) — update the firmware"
        )
        raise SystemExit(1)
    except DeviceResultsError as e:
        print_error(f"the stored results could not be read: {e}")
        raise SystemExit(1)


def _stored_dump_dict(stored: DeviceStoredResult) -> Dict[str, object]:
    """Canonical dump dict for one stored result — the exact shape a single
    `--last` dump produces, so `mifare analyze` reads it the same way."""
    dump_result = stored.dump
    meta = stored.meta
    sectors = dump_result.total
    sector_results: List[Dict[str, object]] = []
    failed: List[Dict[str, object]] = []
    for s in range(sectors):
        entry = dump_result.sectors.get(s)
        blocks = dump_result.blocks_of(s)
        if blocks:
            sector_results.append(
                {"sector": s, "opened_with": entry.key_type or "A", "blocks": blocks}
            )
        else:
            failed.append({"sector": s, "reason": _dump_reason(entry)})
    uid: Optional[str] = None
    if dump_result.card0:
        b0 = parse_block0(dump_result.card0)
        if b0 is not None:
            uid = b0.uid
    if uid is None and meta is not None and meta.uid:
        uid = meta.uid
    dump: Dict[str, object] = {
        "uid": uid,
        "size": _dump_size_label(sectors),
        "sectors_read": len(sector_results),
        "sectors_total": sectors,
        "read_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_keyfile": "device-standalone",
        "sectors": sector_results,
        "failed_sectors": failed,
    }
    if meta is not None:
        dump["device_uptime_s"] = round(meta.uptime_s, 3)
        dump["device_seq"] = meta.seq
    return dump


def _dump_all_stored(ctx, verbose, port, device_id, out_dir, force, as_json):
    """`--last --all`: fetch every stored result in one round-trip, then write
    one canonical JSON per result (--out-dir), print them as a JSON array
    (--json), or show a summary table."""
    level = _verbosity(ctx, verbose)
    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        if not as_json:
            print_info(f"Fetching every result the board stored @ {target}")
        stored = _run_stored_results_all(link)

    if not stored:
        if as_json:
            print(json.dumps([]))
        else:
            print_info(
                f"no stored results @ {target} — the board has not read a card "
                "on its own yet"
            )
        return

    dumps = [_stored_dump_dict(s) for s in stored]

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        for i, d in enumerate(dumps):
            uid = (d.get("uid") or "").strip()
            seq = d.get("device_seq")
            name = f"mifare-{uid}" if uid else f"mifare-result{i}"
            if seq is not None:
                name += f"-seq{seq}"
            path = os.path.join(out_dir, f"{name}.json")
            _refuse_overwrite(path, force)
            try:
                _write_dump_json(path, d)
            except OSError as e:
                print_error(f"could not write {path}: {e}")
                raise SystemExit(1)
            if not as_json:
                print_info(f"wrote {path}")

    if as_json:
        print(json.dumps(dumps))
        return

    console.print("")
    table = Table(title=f"stored results @ {target}", header_style="cyan bold")
    table.add_column("#", justify="right")
    table.add_column("uid")
    table.add_column("sectors", justify="right")
    table.add_column("gaps", justify="right")
    table.add_column("uptime", justify="right")
    for i, d in enumerate(dumps):
        read = d["sectors_read"]
        total = d["sectors_total"]
        uptime = d.get("device_uptime_s")
        table.add_row(
            str(i),
            str(d.get("uid") or "—"),
            f"{read}/{total}",
            str(len(d["failed_sectors"])),
            f"{uptime:.1f}s" if uptime is not None else "—",
        )
    console.print(table)
    if not out_dir:
        print_info(
            "pass --out-dir DIR to save each as canonical JSON for `mifare analyze`"
        )


@click.command("dump")
@click.option(
    "-k",
    "--keys-file",
    "keys_file",
    required=False,
    type=click.Path(exists=True, dir_okay=False),
    metavar="FILE",
    help="A `sector:keyA:keyB` file (see `mifare check --output-keys`). A "
    "sector missing from it, or blank for both keys, is dumped as a gap. "
    "Required for the host-driven dump; not needed when the device dumps "
    "with the keys its own sweep recovered.",
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
    "--mode",
    "mode",
    type=click.Choice(["auto", "device", "host"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="Where the dump runs. 'auto' (default) uses the device's own dump "
    "when no --keys-file is given (the device reads the card with the keys "
    "its sweep recovered, in ONE round-trip) and the host-driven dump "
    "otherwise; 'device' forces the device dump; 'host' forces the "
    "host-driven dump (one USB round-trip per sector).",
)
@click.option(
    "--last",
    "last",
    is_flag=True,
    help="Do not read a card: fetch the result the BOARD stored while it worked "
    "on its own (standalone mode). The dump comes back with the gaps it had, "
    "so it can be analyzed offline. --index picks which stored result "
    "(0 = the newest).",
)
@click.option(
    "--index",
    "index",
    type=click.IntRange(0, 7),
    default=0,
    show_default=True,
    help="Which stored result to fetch with --last (0 = the newest).",
)
@click.option(
    "--all",
    "fetch_all",
    is_flag=True,
    help="With --last, fetch EVERY stored result in one round-trip "
    "(`mifare results show all`) instead of a single --index. Use --out-dir "
    "to write one canonical JSON per result; otherwise a summary is printed "
    "(or a JSON array with --json).",
)
@click.option(
    "--out-dir",
    "out_dir",
    type=click.Path(file_okay=False, writable=True),
    default=None,
    metavar="DIR",
    help="With --last --all, the directory to write one canonical JSON per "
    "stored result into (created if needed). Files are named by uid/seq.",
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
    mode,
    last,
    index,
    fetch_all,
    out_dir,
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

    With --last no card is read at all: the result the board stored while it
    worked on its own (standalone mode) is fetched instead, gaps included.
    """
    if out_file:
        _refuse_overwrite(out_file, force)
    if mfd_file:
        _refuse_overwrite(mfd_file, force)
    if eml_file:
        _refuse_overwrite(eml_file, force)

    mode = (mode or "auto").lower()
    if fetch_all and not last:
        print_error("--all fetches stored results; it needs --last")
        raise SystemExit(1)
    if fetch_all and (out_file or mfd_file or eml_file):
        print_error(
            "--all writes one file per result; use --out-dir, not " "--out/--mfd/--eml"
        )
        raise SystemExit(1)
    if out_dir and not fetch_all:
        print_error("--out-dir only applies to --last --all")
        raise SystemExit(1)
    if fetch_all:
        _dump_all_stored(ctx, verbose, port, device_id, out_dir, force, as_json)
        return
    if last and keys_file:
        print_error(
            "--last fetches a stored result; it cannot be combined with --keys-file"
        )
        raise SystemExit(1)
    if last and mode == "host":
        print_error(
            "--last fetches a stored result; it cannot be combined with --mode host"
        )
        raise SystemExit(1)
    # The on-device dump uses the keys the DEVICE's sweep recovered, so it
    # needs no keyfile. The host-driven dump needs one: it is the only place
    # the keys come from.
    use_device = mode == "device" or (mode == "auto" and not keys_file)
    if mode == "host" and not keys_file:
        print_error(
            "the host-driven dump needs a `sector:keyA:keyB` file "
            "(--keys-file, see `mifare check --output-keys`)"
        )
        raise SystemExit(1)

    table = {}
    if keys_file:
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
    stored_meta = None

    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        if not as_json:
            if last:
                print_info(f"Fetching the result the board stored @ {target}")
            else:
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
                dump_result = None
                if last:
                    # No card is read: the board hands back the result it
                    # stored while it worked on its own, holes included.
                    stored = _run_stored_result(link, index)
                    dump_result = stored.dump
                    stored_meta = stored.meta
                    if dump_result.total:
                        sectors = dump_result.total
                        if progress and task is not None:
                            progress.update(task, total=sectors)
                elif use_device:
                    # ONE round-trip: the device reads the card with the keys
                    # its sweep recovered and answers with every sector.
                    dump_result = _run_device_dump(
                        link, sectors, timeout, as_json, mode
                    )
                    if dump_result is None:
                        # The device could not dump it (older firmware, or no
                        # sweep results) — fall back to the host-driven dump.
                        # Without a keyfile there are no keys to read with, so
                        # say so instead of reporting an empty card.
                        if not keys_file:
                            print_error(
                                "the device could not dump the card and no "
                                "--keys-file was given — run `mifare check "
                                "--output-keys keys.txt` first, or update the "
                                "firmware"
                            )
                            raise SystemExit(1)
                        use_device = False
                if dump_result is not None:
                    for s in range(sectors):
                        entry = dump_result.sectors.get(s)
                        blocks = dump_result.blocks_of(s)
                        if blocks:
                            sector_results.append(
                                {
                                    "sector": s,
                                    "opened_with": entry.key_type or "A",
                                    "blocks": blocks,
                                }
                            )
                        else:
                            failed.append(
                                {
                                    "sector": s,
                                    "reason": _dump_reason(entry),
                                }
                            )
                        if progress:
                            progress.update(
                                task,
                                advance=1,
                                sector=s,
                                ok=len(sector_results),
                                failed=len(failed),
                            )
                    # The block 0 the device read, dissected with the host's
                    # own block0.py — the same rules the firmware applied.
                    if dump_result.card0:
                        b0 = parse_block0(dump_result.card0)
                        if b0 is not None:
                            uid = b0.uid
                    elif stored_meta is not None and stored_meta.uid:
                        # A stored result whose block 0 was never read: the
                        # UID the board reported at the time is still worth
                        # keeping (it is what identifies the card).
                        uid = stored_meta.uid
                else:
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
        # Where the keys came from: the keyfile, or the device's own sweep.
        "source_keyfile": (
            "device-standalone" if last else ("device" if use_device else keys_file)
        ),
        "sectors": sector_results,
        "failed_sectors": failed,
    }
    if stored_meta is not None:
        # The board has no real-time clock: its timestamp is its own uptime
        # (how long after the boot the card was read). The "read_at" above is
        # the HOST's clock, i.e. when the result was collected.
        dump["device_uptime_s"] = round(stored_meta.uptime_s, 3)
        dump["device_seq"] = stored_meta.seq

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
    if last and stored_meta is not None:
        print_info(
            f"stored result #{stored_meta.index} (seq {stored_meta.seq}, read "
            f"{stored_meta.uptime_s:.1f}s after the board booted)"
        )
    if interrupted:
        print_warning("dump incomplete — interrupted before finishing")

    if decode and sector_results:
        console.print("\n[cyan bold]Decoded sectors[/cyan bold]")
        for entry in sector_results:
            _print_decoded_sector(entry)

    raise SystemExit(0 if complete else 1)
