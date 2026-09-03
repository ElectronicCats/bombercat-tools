#!/usr/bin/env python3

# Electronic Cats
# `bombercat readers read|watch|scan|info` — NFC reader/terminal detection
# over the DetectReaders REPL. Turns the device's `:reader` events into
# short, scriptable output. Twin of modules/tags/cli.py; no legacy text mode
# (DetectReaders was born with structured events).
# docs/IMPLEMENTATION_PLAN_DetectReaders_CLI.md §3.2.
# Distributed as-is; no warranty is given.

import json
import re
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional, Tuple

import click
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ..core.bombercat import DeviceLink, resolve_port
from ..utils.cli_options import device_options
from ..utils.detection_cli import (
    device_session,
    print_field as _print_field,
    refuse_overwrite as _refuse_overwrite,
    verbosity as _verbosity,
    write_csv as _write_csv_base,
    write_export as _write_export,
    write_json as _write_json,
)
from ..utils.output import (
    console,
    make_tracer,
    print_dim,
    print_error,
    print_info,
    print_warning,
)
from .aggregator import _RESERVED_KEYS, ReaderAggregator
from .parser import Reader, ReaderParser

# How long `readers info` listens for a ':reader' event before concluding
# none has been seen yet.
_INFO_PROBE_SECONDS = 2.0

# Firmware chatter the CLI's own (non -v) output hides by default: boot/idle
# noise printed while armed and re-arming, not a detection.
_NOISE_RE = re.compile(
    r"^(Waiting for a Reader \.\.\.|Re-arm: .*|Re-armed\. .*|"
    r"Re-arm completed with errors .*)\s*$"
)


@click.group("readers", context_settings={"help_option_names": ["-h", "--help"]})
def readers():
    """NFC reader/terminal detection commands (requires the DetectReaders firmware)."""


@contextmanager
def _readers_session(
    port: Optional[str],
    device_id: Optional[int] = None,
    trace=None,
) -> Iterator[Tuple[str, DeviceLink]]:
    """Open a verified link for the `readers` commands, yield ``(target, link)``,
    and always close it. Thin, DetectReaders-flavored wrapper around
    `detection_cli.device_session` — `resolve_port`/`DeviceLink` are passed
    in explicitly so tests can still monkeypatch this module's copies."""
    with device_session(
        resolve_port, DeviceLink, "readers", "DetectReaders", port, device_id, trace
    ) as pair:
        yield pair


def _reader_to_dict(reader: Reader) -> Dict[str, object]:
    d: Dict[str, object] = {
        "ts_ms": reader.ts_ms,
        "tech": reader.tech,
        "protocol": reader.protocol,
        "intf": reader.intf,
        "apdu": reader.apdu,
        "aid": reader.aid,
        "label": reader.label,
        "n": reader.n,
    }
    for key, value in reader.extra.items():
        d["x_" + key if key in _RESERVED_KEYS else key] = value
    return d


def _emit_reader(reader: Reader, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_reader_to_dict(reader)))
        return
    console.print("")
    console.print("  [green bold]Reader detected[/green bold]")
    _print_field("label", reader.label or "[dim]—[/dim]")
    _print_field("technology", reader.tech or "[dim]—[/dim]")
    _print_field("protocol", reader.protocol or "[dim]—[/dim]")
    _print_field("interface", reader.intf or "[dim]—[/dim]")
    _print_field("fingerprint", reader.fingerprint)
    _print_field("apdu", reader.pretty_apdu)
    if reader.aid:
        _print_field("aid", reader.aid)
    _print_field("n", str(reader.n) if reader.n is not None else "[dim]—[/dim]")
    for key, value in reader.extra.items():
        _print_field(key.replace("_", " "), value)


# ── read ─────────────────────────────────────────────────────────────────────


@readers.command("read", context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "-t",
    "--timeout",
    default=15.0,
    show_default=True,
    help="Seconds to wait for a reader.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit one JSON object on stdout.")
@device_options
@click.pass_context
def read_cmd(ctx, timeout, as_json, verbose, port, device_id):
    """Wait for one reader/terminal to probe the emulated card.

    Exit code: 0 reader read, 1 timeout or link error.
    """
    level = _verbosity(ctx, verbose)
    reader: Optional[Reader] = None
    with _readers_session(port, device_id, trace=make_tracer(level)) as (target, link):
        if not as_json:
            print_info(f"Waiting for a reader on {target} — Ctrl-C to abort")
        parser = ReaderParser()
        deadline = time.monotonic() + timeout
        try:
            for line in link.stream(yield_empty=True):
                if line:
                    reader = parser.feed(line)
                    if reader is not None:
                        break
                if time.monotonic() > deadline:
                    break
        except KeyboardInterrupt:
            print_warning("aborted")
            raise SystemExit(1)

    if reader is None:
        print_error(f"no reader detected in {timeout:g}s")
        raise SystemExit(1)
    _emit_reader(reader, as_json)


# ── watch ────────────────────────────────────────────────────────────────────


def _watch_line(reader: Reader, repeat: int) -> str:
    ts = time.strftime("%H:%M:%S")
    suffix = f" (x{repeat})" if repeat > 1 else ""
    return (
        f"[{ts}] {reader.label or '?':<14}{reader.tech or '?':<8}"
        f"{reader.protocol or '?':<11}{reader.pretty_apdu}{suffix}"
    )


@readers.command("watch", context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--dedupe",
    is_flag=True,
    help="Collapse repeat detections of the same fingerprint into a counter "
    "instead of reprinting them. Two distinct terminals sharing the same "
    "label (e.g. two EMV readers) share a fingerprint too, so the counter "
    "can mix them — see `readers watch -h` in the docs for details.",
)
@click.option(
    "--quiet-noise/--no-quiet-noise",
    default=True,
    show_default=True,
    help="Hide firmware boot/idle chatter (Waiting for a Reader…, Re-arm…).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit one JSON object per line.")
@device_options
@click.pass_context
def watch_cmd(ctx, dedupe, quiet_noise, as_json, verbose, port, device_id):
    """Stream reader detections continuously. Ctrl-C to stop and print a summary."""
    level = _verbosity(ctx, verbose)
    detections = 0
    counts: "OrderedDict[str, int]" = OrderedDict()
    start = time.monotonic()
    with _readers_session(port, device_id, trace=make_tracer(level)) as (target, link):
        if not as_json:
            print_info(f"Watching {target} — Ctrl-C to stop")
        parser = ReaderParser()
        try:
            for line in link.stream():
                if not line:
                    continue
                reader = parser.feed(line)
                if reader is None:
                    if (
                        not quiet_noise
                        and not as_json
                        and _NOISE_RE.match(line.strip())
                    ):
                        console.print(f"[dim]{line}[/dim]")
                    continue

                detections += 1
                key = reader.fingerprint
                counts[key] = counts.get(key, 0) + 1
                counts.move_to_end(key)
                if dedupe and counts[key] > 1:
                    if not as_json:
                        console.print(
                            f"[dim]  ↳ {reader.label or 'unknown'} seen again "
                            f"(x{counts[key]})[/dim]"
                        )
                    continue
                if as_json:
                    print(json.dumps(_reader_to_dict(reader)))
                else:
                    console.print(_watch_line(reader, counts[key]))
        except KeyboardInterrupt:
            pass

    if not as_json:
        duration = time.monotonic() - start
        console.print("")
        print_info(
            f"{detections} detections, {len(counts)} unique fingerprints, "
            f"{duration:.0f}s"
        )


# ── scan ─────────────────────────────────────────────────────────────────────


_CSV_FIELDS = [
    "label",
    "tech",
    "protocol",
    "intf",
    "apdu",
    "aid",
    "count",
    "first_s",
    "last_s",
]


def _write_csv(path: str, rows: List[Dict[str, object]]) -> None:
    _write_csv_base(path, rows, _CSV_FIELDS)


@readers.command("scan", context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "-t",
    "--timeout",
    default=30.0,
    show_default=True,
    help="Seconds to sample for.",
)
@click.option(
    "--json-out",
    "json_file",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    metavar="FILE",
    help="Write the aggregate as a JSON array to FILE.",
)
@click.option(
    "--csv-out",
    "csv_file",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    metavar="FILE",
    help="Write the aggregate as CSV to FILE.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite --json-out/--csv-out output files if they already exist.",
)
@device_options
@click.pass_context
def scan_cmd(ctx, timeout, json_file, csv_file, force, verbose, port, device_id):
    """Sample reader detections for a while and print an aggregated summary.

    Repeat detections of the same fingerprint collapse into one row with a
    count and a first/last time seen. Ctrl-C ends the sample early.
    """
    if json_file:
        _refuse_overwrite(json_file, force)
    if csv_file:
        _refuse_overwrite(csv_file, force)

    level = _verbosity(ctx, verbose)
    aggregator = ReaderAggregator()
    with _readers_session(port, device_id, trace=make_tracer(level)) as (target, link):
        print_info(f"Scanning {target} for {timeout:g}s — Ctrl-C to stop early")
        parser = ReaderParser()
        start = time.monotonic()
        deadline = start + timeout
        progress = Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[cyan]scanning[/cyan]"),
            BarColumn(bar_width=24, complete_style="cyan", finished_style="cyan"),
            TextColumn("[dim]{task.fields[detections]} detections[/dim]"),
            console=console,
            transient=True,
        )
        try:
            with progress:
                task = progress.add_task("", total=timeout, detections=0)
                for line in link.stream(yield_empty=True):
                    if line:
                        reader = parser.feed(line)
                        if reader is not None:
                            aggregator.add(reader)
                    now = time.monotonic()
                    progress.update(
                        task,
                        completed=min(now - start, timeout),
                        detections=aggregator.total_detections,
                    )
                    if now > deadline:
                        break
        except KeyboardInterrupt:
            pass

    console.print("")
    print_info(
        f"Scan @ {target} — {timeout:g}s, {aggregator.total_detections} detections, "
        f"{len(aggregator)} unique readers"
    )

    rows = aggregator.to_dict()
    if rows:
        # APDU is left out of the table (it can run to hundreds of hex chars
        # and would blow out the column width) — it's still in the JSON/CSV
        # exports below.
        table = Table(header_style="cyan bold")
        table.add_column("Label")
        table.add_column("Tech")
        table.add_column("Protocol")
        table.add_column("Interface")
        table.add_column("Count", justify="right")
        table.add_column("First", justify="right")
        table.add_column("Last", justify="right")
        for row in rows:
            table.add_row(
                row["label"] or "[dim]—[/dim]",
                row["tech"] or "[dim]—[/dim]",
                row["protocol"] or "[dim]—[/dim]",
                row["intf"] or "[dim]—[/dim]",
                str(row["count"]),
                f"{row['first_s']:.1f}s",
                f"{row['last_s']:.1f}s",
            )
        console.print(table)
    else:
        print_dim("no readers detected")

    if json_file:
        _write_export(json_file, rows, _write_json)
    if csv_file:
        _write_export(csv_file, rows, _write_csv)


# ── info ─────────────────────────────────────────────────────────────────────


@readers.command("info", context_settings={"help_option_names": ["-h", "--help"]})
@device_options
@click.pass_context
def info_cmd(ctx, verbose, port, device_id):
    """Report what this DetectReaders image can do.

    Shows the firmware version and state, and whether a ':reader' event has
    been seen during a short probe window.
    """
    level = _verbosity(ctx, verbose)
    with _readers_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = link.info()
        if not r.ok:
            print_error(f"info failed: {r.message}")
            raise SystemExit(1)

        parser = ReaderParser()
        deadline = time.monotonic() + _INFO_PROBE_SECONDS
        for line in link.stream(yield_empty=True):
            if line:
                parser.feed(line)
            if parser.structured or time.monotonic() > deadline:
                break

    table = Table(
        title=f"DetectReaders @ {target}", header_style="cyan bold", show_header=False
    )
    table.add_column("field", style="cyan")
    table.add_column("value")
    table.add_row("version", r.data.get("fw") or "[dim]—[/dim]")
    if parser.structured:
        events = "structured (':reader' events)"
    else:
        events = "no ':reader' events seen yet"
    table.add_row("events", events)
    table.add_row("state", r.data.get("state") or "[dim]—[/dim]")
    console.print(table)
