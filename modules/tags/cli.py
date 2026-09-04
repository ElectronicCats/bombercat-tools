#!/usr/bin/env python3

# Electronic Cats
# `bombercat tags read|watch` — NFC tag detection over the DetectTags REPL.
# Turns the device's `:tag` events (or, on older .uf2 images, the legacy
# displayCardInfo() text) into short, scriptable output.
# docs/CLI_IMPROVEMENTS_DetectTags.md §3.1-3.2.
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
    print_success,
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

# `tags mifare ...` (MifareClassic firmware, MIFARE_CLASSIC_PLAN.md §3/Phase 4)
_MIFARE_KEY_HEX_LEN = 12  # 6-byte key, hex-encoded
_MIFARE_BLOCK_HEX_LEN = 32  # 16-byte block, hex-encoded
_MIFARE_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")
# Always printed right before the firmware opens its interactive session
# (probeMifareBlock() runs, then openMifareSession() — see MifareClassic.ino's
# handleTagDetected()), so seeing it is a reliable "the session is open now".
_MIFARE_PROBE_RE = re.compile(r"^:mifare\s")
_MIFARE_TAP_TIMEOUT = 15.0


@click.group("tags", context_settings={"help_option_names": ["-h", "--help"]})
def tags():
    """NFC tag detection commands (requires the DetectTags firmware)."""


@contextmanager
def _tags_session(
    port: Optional[str],
    device_id: Optional[int] = None,
    trace=None,
) -> Iterator[Tuple[str, DeviceLink]]:
    """Open a verified link for the `tags` commands, yield ``(target, link)``,
    and always close it. Thin, DetectTags-flavored wrapper around
    `detection_cli.device_session` — `resolve_port`/`DeviceLink` are passed
    in explicitly so tests can still monkeypatch this module's copies."""
    with device_session(
        resolve_port, DeviceLink, "tags", "DetectTags", port, device_id, trace
    ) as pair:
        yield pair


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


_CSV_FIELDS = ["uid", "tech", "protocol", "count", "first_s", "last_s"]


def _write_csv(path: str, rows: List[Dict[str, object]]) -> None:
    _write_csv_base(path, rows, _CSV_FIELDS)


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


# ── mifare ───────────────────────────────────────────────────────────────────
#
# `bombercat tags mifare auth|read|write|sector|keys` — Mifare Classic block
# access over the MifareClassic firmware's REPL (bombercat-firmware's
# MIFARE_CLASSIC_PLAN.md §3.1/Phase 3, CLI side is §4/Phase 4). `auth`/`read`/
# `write`/`sector` need a card already selected in the firmware's interactive
# "mifare session", which it opens on its own right after tapping a Mifare
# Classic card (see MifareClassic.ino's handleTagDetected()); rather than just
# failing when no card has been tapped yet, each of those commands asks the
# user to tap one and waits for the firmware's auto-probe ':mifare' event
# (proof the session is now open) before retrying once.
#
# `mifare dump` (read every sector of a card) and `mifare keys add/remove`
# (persisted custom keys) are listed in the plan's CLI design but not
# implemented here yet — out of scope for this pass.


def _mifare_validate_hex(value: str, expected_len: int, label: str) -> Optional[str]:
    if len(value) != expected_len or not _MIFARE_HEX_RE.match(value):
        return f"{label} must be exactly {expected_len} hex characters"
    return None


@contextmanager
def _mifare_session(
    port: Optional[str],
    device_id: Optional[int] = None,
    trace=None,
) -> Iterator[Tuple[str, DeviceLink]]:
    """Open a verified link for the `tags mifare` commands, yield ``(target,
    link)``, and always close it. Same `device_session` wrapper as
    `_tags_session`, naming the MifareClassic firmware in its error message."""
    with device_session(
        resolve_port, DeviceLink, "tags mifare", "MifareClassic", port, device_id, trace
    ) as pair:
        yield pair


def _wait_for_mifare_card(link, timeout: float) -> bool:
    """Block until the firmware's auto-probe ':mifare' event appears — proof
    it has opened the interactive session — or `timeout` seconds pass."""
    deadline = time.monotonic() + timeout
    for line in link.stream(yield_empty=True):
        if line and _MIFARE_PROBE_RE.match(line):
            return True
        if time.monotonic() > deadline:
            return False
    return False


def _run_mifare_command(link, line: str, timeout: float):
    """Send a `mifare ...` REPL line. If the firmware answers "no card
    selected" (no session open yet), ask the user to tap a card, wait up to
    `timeout`s for the auto-probe event, and retry once."""
    r = link.command(line)
    if not r.ok and "no card selected" in r.message:
        print_info(
            f"no card selected — tap a Mifare Classic card (up to {timeout:g}s)..."
        )
        if _wait_for_mifare_card(link, timeout):
            r = link.command(line)
    return r


_MIFARE_TIMEOUT_OPTION = click.option(
    "-t",
    "--timeout",
    default=_MIFARE_TAP_TIMEOUT,
    show_default=True,
    help="Seconds to wait for a card tap if none is selected yet.",
)

_MIFARE_KEY_TYPE_OPTION = click.option(
    "--key-type",
    type=click.Choice(["A", "B"], case_sensitive=False),
    default="A",
    show_default=True,
    help="Which key slot (A or B) to authenticate with.",
)


@tags.group("mifare", context_settings={"help_option_names": ["-h", "--help"]})
def mifare():
    """Mifare Classic auth/read/write/sector commands (requires the
    MifareClassic firmware).

    Tap a Mifare Classic card to let the firmware select it — `auth`, `read`,
    `write` and `sector` all wait for this automatically if no card is
    selected yet. The card then stays selected for ~10s of inactivity between
    commands before the firmware closes the session and re-arms discovery.
    """


@mifare.command("auth", context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--block", type=click.IntRange(0, 255), required=True, help="Block number."
)
@_MIFARE_KEY_TYPE_OPTION
@click.option("--key", required=True, metavar="HEX12", help="6-byte key, 12 hex chars.")
@_MIFARE_TIMEOUT_OPTION
@device_options
@click.pass_context
def mifare_auth_cmd(ctx, block, key_type, key, timeout, verbose, port, device_id):
    """Authenticate BLOCK's sector so a following `read`/`write` can access it."""
    err = _mifare_validate_hex(key, _MIFARE_KEY_HEX_LEN, "--key")
    if err:
        print_error(err)
        raise SystemExit(1)

    level = _verbosity(ctx, verbose)
    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = _run_mifare_command(
            link, f"mifare auth {block} {key_type.upper()} {key.upper()}", timeout
        )

    if not r.ok:
        print_error(f"auth failed: {r.message}")
        raise SystemExit(1)
    print_success(f"authenticated block {block} with key {key_type.upper()}")


@mifare.command("read", context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--block", type=click.IntRange(0, 255), required=True, help="Block number."
)
@click.option(
    "--json", "as_json", is_flag=True, help='Emit {"block": ..., "data": ...}.'
)
@_MIFARE_TIMEOUT_OPTION
@device_options
@click.pass_context
def mifare_read_cmd(ctx, block, as_json, timeout, verbose, port, device_id):
    """Read BLOCK from its already-authenticated sector (run `auth` first)."""
    level = _verbosity(ctx, verbose)
    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = _run_mifare_command(link, f"mifare read {block}", timeout)

    if not r.ok:
        print_error(f"read failed: {r.message}")
        raise SystemExit(1)
    _, _, data_hex = r.data.get("mifare_data", "").partition(" ")
    if as_json:
        print(json.dumps({"block": block, "data": data_hex}))
        return
    console.print("")
    _print_field("block", str(block))
    _print_field("data", data_hex or "[dim]—[/dim]")


@mifare.command("write", context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--block", type=click.IntRange(0, 255), required=True, help="Block number."
)
@click.option(
    "--data", required=True, metavar="HEX32", help="16-byte block, 32 hex chars."
)
@_MIFARE_TIMEOUT_OPTION
@device_options
@click.pass_context
def mifare_write_cmd(ctx, block, data, timeout, verbose, port, device_id):
    """Write DATA to BLOCK in its already-authenticated sector (run `auth` first)."""
    err = _mifare_validate_hex(data, _MIFARE_BLOCK_HEX_LEN, "--data")
    if err:
        print_error(err)
        raise SystemExit(1)

    level = _verbosity(ctx, verbose)
    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = _run_mifare_command(link, f"mifare write {block} {data.upper()}", timeout)

    if not r.ok:
        print_error(f"write failed: {r.message}")
        raise SystemExit(1)
    print_success(f"wrote block {block}")


@mifare.command("sector", context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--sector", type=click.IntRange(0, 255), required=True, help="Sector number."
)
@_MIFARE_KEY_TYPE_OPTION
@click.option("--key", required=True, metavar="HEX12", help="6-byte key, 12 hex chars.")
@click.option(
    "--json", "as_json", is_flag=True, help='Emit {"sector": ..., "data": ...}.'
)
@_MIFARE_TIMEOUT_OPTION
@device_options
@click.pass_context
def mifare_sector_cmd(
    ctx, sector, key_type, key, as_json, timeout, verbose, port, device_id
):
    """Authenticate and read every block of SECTOR in one call (self-contained)."""
    err = _mifare_validate_hex(key, _MIFARE_KEY_HEX_LEN, "--key")
    if err:
        print_error(err)
        raise SystemExit(1)

    level = _verbosity(ctx, verbose)
    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = _run_mifare_command(
            link, f"mifare sector {sector} {key_type.upper()} {key.upper()}", timeout
        )

    if not r.ok:
        print_error(f"sector read failed: {r.message}")
        raise SystemExit(1)
    data_hex = r.data.get("mifare_sector", "")
    if as_json:
        print(json.dumps({"sector": sector, "data": data_hex}))
        return
    console.print("")
    _print_field("sector", str(sector))
    _print_field("data", data_hex or "[dim]—[/dim]")


@mifare.command("keys", context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--json", "as_json", is_flag=True, help="Emit one JSON object per key.")
@device_options
@click.pass_context
def mifare_keys_cmd(ctx, as_json, verbose, port, device_id):
    """List the firmware's built-in default keys. No card needed."""
    level = _verbosity(ctx, verbose)
    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = link.command("mifare keys")

    if not r.ok:
        print_error(f"keys failed: {r.message}")
        raise SystemExit(1)

    keys = []
    i = 0
    while f"mifare_key{i}" in r.data:
        name, _, hex_value = r.data[f"mifare_key{i}"].partition(" ")
        keys.append((name, hex_value))
        i += 1

    if as_json:
        for name, hex_value in keys:
            print(json.dumps({"name": name, "key": hex_value}))
        return

    if not keys:
        print_dim("no keys reported")
        return
    table = Table(
        title=f"MifareClassic default keys @ {target}", header_style="cyan bold"
    )
    table.add_column("name", style="cyan")
    table.add_column("key")
    for name, hex_value in keys:
        table.add_row(name, hex_value)
    console.print(table)
