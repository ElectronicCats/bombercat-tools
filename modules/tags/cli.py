#!/usr/bin/env python3

# Electronic Cats
# `bombercat tags read|watch` — NFC tag detection over the DetectTags REPL.
# Turns the device's `:tag` events (or, on older .uf2 images, the legacy
# displayCardInfo() text) into short, scriptable output.
# docs/CLI_IMPROVEMENTS_DetectTags.md §3.1-3.2.
# Distributed as-is; no warranty is given.

import csv
import json
import os
import re
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional, Tuple

import click
import serial
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ..core.bombercat import DeviceError, DeviceLink, resolve_port
from ..utils.cli_options import device_options
from ..utils.output import (
    console,
    make_tracer,
    print_dim,
    print_error,
    print_info,
    print_warning,
)
from .aggregator import _RESERVED_KEYS, TagAggregator
from .parser import Tag, TagParser

# How long `tags info` listens for a ':tag' event before concluding the
# firmware doesn't emit them (docs/CLI_IMPROVEMENTS_DetectTags.md §3.4/§3.5).
_INFO_PROBE_SECONDS = 2.0

# Firmware chatter the CLI's own (non -v) output hides by default: boot/idle
# noise printed on every loop iteration, not a detection.
_NOISE_RE = re.compile(
    r"^(Restarting\.\.\.|Waiting for a Card\.\.\.|Card removed!)\s*$"
)

# Firmware that prints no UID (legacy NFC-B/F) keys `watch`'s dedupe table by
# `tech:protocol:ts_ms`, which never repeats — an unattended `watch` running
# for hours/days would otherwise grow this dict without bound. Cap it as an
# LRU: oldest key evicted once full (M15).
_MAX_DEDUPE_KEYS = 10_000


@click.group("tags", context_settings={"help_option_names": ["-h", "--help"]})
def tags():
    """NFC tag detection commands (requires the DetectTags firmware)."""


def _verbosity(ctx: click.Context, local: int) -> int:
    """Combine the root `-v` (before the verb) with a command's own `-v`
    (after it) — either position means the same thing, so the higher count
    wins rather than the two adding up."""
    root = (ctx.obj or {}).get("verbose", 0)
    return max(root, local)


@contextmanager
def _tags_session(
    port: Optional[str],
    device_id: Optional[int] = None,
    trace=None,
) -> Iterator[Tuple[str, DeviceLink]]:
    """Open a verified link for the `tags` commands, yield ``(target, link)``,
    and always close it. Mirrors `nfcgate.cli._device_session`, with a
    DetectTags-flavored hint on a failed handshake."""
    link: Optional[DeviceLink] = None
    try:
        target = resolve_port(port, device_id)
        link = DeviceLink(target, trace=trace).open()
        if not link.ping():
            print_error(
                f"{target} did not answer the handshake. "
                "`tags` needs the DetectTags firmware — check what's flashed "
                "with:  bombercat status"
            )
            raise SystemExit(1)
        yield target, link
    except DeviceError as e:
        print_error(str(e))
        raise SystemExit(1)
    except (serial.SerialException, OSError) as e:
        print_error(f"{type(e).__name__}: {e}")
        raise SystemExit(1)
    finally:
        if link is not None:
            link.close()


def _tag_to_dict(tag: Tag) -> Dict[str, object]:
    d: Dict[str, object] = {
        "uid": tag.uid,
        "tech": tag.tech,
        "protocol": tag.protocol,
        "ts_ms": tag.ts_ms,
    }
    for key, value in tag.extra.items():
        d["x_" + key if key in _RESERVED_KEYS else key] = value
    return d


def _print_field(label: str, value: str) -> None:
    console.print(f"  [cyan]{label:<13}[/cyan] {value}")


def _emit_tag(tag: Tag, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_tag_to_dict(tag)))
        return
    console.print("")
    console.print("  [green bold]Tag detected[/green bold]")
    _print_field("uid", tag.pretty_uid)
    _print_field("technology", tag.tech or "[dim]—[/dim]")
    _print_field("protocol", tag.protocol or "[dim]—[/dim]")
    for key, value in tag.extra.items():
        _print_field(key.replace("_", " "), value)


# ── read ─────────────────────────────────────────────────────────────────────


@tags.command("read", context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "-t",
    "--timeout",
    default=15.0,
    show_default=True,
    help="Seconds to wait for a tag.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit one JSON object on stdout.")
@device_options
@click.pass_context
def read_cmd(ctx, timeout, as_json, verbose, port, device_id):
    """Wait for one tag and print its UID.

    Exit code: 0 tag read, 1 timeout or link error.
    """
    level = _verbosity(ctx, verbose)
    tag: Optional[Tag] = None
    with _tags_session(port, device_id, trace=make_tracer(level)) as (target, link):
        if not as_json:
            print_info(f"Waiting for a tag on {target} — Ctrl-C to abort")
        parser = TagParser()
        deadline = time.monotonic() + timeout
        try:
            for line in link.stream(yield_empty=True):
                if line:
                    tag = parser.feed(line)
                    if tag is not None:
                        break
                if time.monotonic() > deadline:
                    break
        except KeyboardInterrupt:
            print_warning("aborted")
            raise SystemExit(1)

    if tag is None:
        print_error(f"no tag detected in {timeout:g}s")
        raise SystemExit(1)
    _emit_tag(tag, as_json)


# ── watch ────────────────────────────────────────────────────────────────────


def _watch_line(tag: Tag, repeat: int) -> str:
    ts = time.strftime("%H:%M:%S")
    suffix = f" (x{repeat})" if repeat > 1 else ""
    return (
        f"[{ts}] {tag.tech or '?':<8}{tag.protocol or '?':<11}{tag.pretty_uid}{suffix}"
    )


@tags.command("watch", context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--dedupe",
    is_flag=True,
    help="Collapse repeat detections of the same UID into a counter instead "
    "of reprinting them.",
)
@click.option(
    "--quiet-noise/--no-quiet-noise",
    default=True,
    show_default=True,
    help="Hide firmware boot/idle chatter (Restarting…, Waiting for a Card…, "
    "Card removed!).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit one JSON object per line.")
@device_options
@click.pass_context
def watch_cmd(ctx, dedupe, quiet_noise, as_json, verbose, port, device_id):
    """Stream tag detections continuously. Ctrl-C to stop and print a summary."""
    level = _verbosity(ctx, verbose)
    detections = 0
    counts: "OrderedDict[str, int]" = OrderedDict()
    capped = False
    start = time.monotonic()
    with _tags_session(port, device_id, trace=make_tracer(level)) as (target, link):
        if not as_json:
            print_info(f"Watching {target} — Ctrl-C to stop")
        parser = TagParser()
        try:
            for line in link.stream():
                if not line:
                    continue
                tag = parser.feed(line)
                if tag is None:
                    if (
                        not quiet_noise
                        and not as_json
                        and _NOISE_RE.match(line.strip())
                    ):
                        console.print(f"[dim]{line}[/dim]")
                    continue

                detections += 1
                key = tag.uid or f"{tag.tech}:{tag.protocol}:{tag.ts_ms}"
                if key not in counts and len(counts) >= _MAX_DEDUPE_KEYS:
                    counts.popitem(last=False)
                    if not capped:
                        print_warning(
                            f"dedupe table capped at {_MAX_DEDUPE_KEYS} "
                            "entries — oldest UIDs are being evicted"
                        )
                        capped = True
                counts[key] = counts.get(key, 0) + 1
                counts.move_to_end(key)
                if dedupe and counts[key] > 1:
                    if not as_json:
                        console.print(
                            f"[dim]  ↳ {tag.pretty_uid} seen again "
                            f"(x{counts[key]})[/dim]"
                        )
                    continue
                if as_json:
                    print(json.dumps(_tag_to_dict(tag)))
                else:
                    console.print(_watch_line(tag, counts[key]))
        except KeyboardInterrupt:
            pass

    if not as_json:
        duration = time.monotonic() - start
        console.print("")
        print_info(
            f"{detections} detections, {len(counts)} unique UIDs, {duration:.0f}s"
        )


# ── scan ─────────────────────────────────────────────────────────────────────


def _write_json(path: str, rows: List[Dict[str, object]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)
        f.write("\n")


_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _csv_safe(value: object) -> object:
    """Neutralize CSV/formula injection (device-controlled fields like `tech`,
    `protocol`, and `extra` are free text): a cell starting with =/+/-/@ is
    interpreted as a formula by Excel/LibreOffice on open."""
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def _write_csv(path: str, rows: List[Dict[str, object]]) -> None:
    fieldnames = ["uid", "tech", "protocol", "count", "first_s", "last_s"]
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _csv_safe(v) for k, v in row.items()})


def _refuse_overwrite(path: str, force: bool) -> None:
    """These outputs are audit evidence (a scan export may reflect card/UID
    data captured live) — refuse a silent overwrite unless --force."""
    if not force and os.path.exists(path):
        print_error(f"{path} already exists — pass --force to overwrite")
        raise SystemExit(1)


def _write_export(path: str, rows: List[Dict[str, object]], writer) -> None:
    try:
        writer(path, rows)
    except OSError as e:
        print_error(f"could not write {path}: {e}")
        raise SystemExit(1)
    print_info(f"wrote {path}")


@tags.command("scan", context_settings={"help_option_names": ["-h", "--help"]})
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
    "--json",
    "json_file_legacy",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    hidden=True,  # deprecated alias for --json-out (L4): `--json` is a boolean
    # flag on `read`/`watch` but took a FILE here, which was confusing.
)
@click.option(
    "--csv",
    "csv_file_legacy",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    hidden=True,  # deprecated alias for --csv-out (L4)
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite --json-out/--csv-out output files if they already exist.",
)
@device_options
@click.pass_context
def scan_cmd(
    ctx,
    timeout,
    json_file,
    csv_file,
    json_file_legacy,
    csv_file_legacy,
    force,
    verbose,
    port,
    device_id,
):
    """Sample tag detections for a while and print an aggregated summary.

    Repeat detections of the same UID collapse into one row with a count and
    a first/last time seen. Ctrl-C ends the sample early.
    """
    if json_file_legacy:
        print_warning("`scan --json FILE` is deprecated — use `--json-out FILE`.")
        json_file = json_file or json_file_legacy
    if csv_file_legacy:
        print_warning("`scan --csv FILE` is deprecated — use `--csv-out FILE`.")
        csv_file = csv_file or csv_file_legacy

    if json_file:
        _refuse_overwrite(json_file, force)
    if csv_file:
        _refuse_overwrite(csv_file, force)

    level = _verbosity(ctx, verbose)
    aggregator = TagAggregator()
    with _tags_session(port, device_id, trace=make_tracer(level)) as (target, link):
        print_info(f"Scanning {target} for {timeout:g}s — Ctrl-C to stop early")
        parser = TagParser()
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
                        tag = parser.feed(line)
                        if tag is not None:
                            aggregator.add(tag)
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
        f"{len(aggregator)} unique tags"
    )

    rows = aggregator.to_dict()
    if rows:
        table = Table(header_style="cyan bold")
        table.add_column("UID")
        table.add_column("Tech")
        table.add_column("Protocol")
        table.add_column("Count", justify="right")
        table.add_column("First", justify="right")
        table.add_column("Last", justify="right")
        for row in rows:
            pretty = Tag(uid=row["uid"], tech=row["tech"]).pretty_uid
            table.add_row(
                pretty,
                row["tech"] or "[dim]—[/dim]",
                row["protocol"] or "[dim]—[/dim]",
                str(row["count"]),
                f"{row['first_s']:.1f}s",
                f"{row['last_s']:.1f}s",
            )
        console.print(table)
    else:
        print_dim("no tags detected")

    if json_file:
        _write_export(json_file, rows, _write_json)
    if csv_file:
        _write_export(csv_file, rows, _write_csv)


# ── info ─────────────────────────────────────────────────────────────────────


@tags.command("info", context_settings={"help_option_names": ["-h", "--help"]})
@device_options
@click.pass_context
def info_cmd(ctx, verbose, port, device_id):
    """Report what this DetectTags image can do.

    Shows the firmware version and state, and — the useful part — whether it
    emits structured ':tag' events or the CLI has fallen back to parsing
    legacy text (docs/CLI_IMPROVEMENTS_DetectTags.md §3.4).
    """
    level = _verbosity(ctx, verbose)
    with _tags_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = link.info()
        if not r.ok:
            print_error(f"info failed: {r.message}")
            raise SystemExit(1)

        parser = TagParser()
        deadline = time.monotonic() + _INFO_PROBE_SECONDS
        for line in link.stream(yield_empty=True):
            if line:
                parser.feed(line)
            if parser.structured or time.monotonic() > deadline:
                break

    table = Table(
        title=f"DetectTags @ {target}", header_style="cyan bold", show_header=False
    )
    table.add_column("field", style="cyan")
    table.add_column("value")
    table.add_row("version", r.data.get("fw") or "[dim]—[/dim]")
    if parser.structured:
        events = "structured (':tag' events)"
    else:
        events = "legacy text  (no ':tag' events — reflash for exact parsing)"
    table.add_row("events", events)
    table.add_row("state", r.data.get("state") or "[dim]—[/dim]")
    console.print(table)
