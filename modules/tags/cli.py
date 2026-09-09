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
from datetime import datetime, timezone
from contextlib import contextmanager, nullcontext
from typing import Dict, Iterator, List, Optional, Tuple

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
    print_subtitle,
    print_title,
    print_success,
    print_warning,
)
from .access_bits import _KEY_A, _NEVER, SectorAccessBits, parse_access_bits
from .aggregator import _RESERVED_KEYS, TagAggregator
from .block0 import Block0, parse_block0
from .keyfile import (
    SectorKeyfileError,
    default_keyfile,
    load_keys,
    load_sector_keys,
)
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
# `mifare check` (dictionary attack, host-side sweep over `auth` — see
# docs/CLI_IMPROVEMENTS_MifareCheck.md) needs no pre-existing session either;
# it opens one itself the same way.
#
# `mifare dump` (read every sector of a card) and `mifare keys add/remove`
# (persisted custom keys) are listed in the plan's CLI design but not
# implemented here yet — out of scope for this pass.


_MIFARE_TRAILER_KEY_LEN = 12  # 6-byte key, hex-encoded
_MIFARE_TRAILER_AC_LEN = 8  # 4-byte access conditions, hex-encoded


def _substitute_trailer_keys(
    trailer: str, key_a: Optional[str], key_b: Optional[str]
) -> Tuple[str, str, str]:
    """Split a trailer block into `(key_a, ac, key_b)`, substituting the real
    keys from a keyfile in place of the zeros a card reads back for them. A
    card never reads Key A back (and often not Key B either); the
    access-conditions bytes in the middle always stay exactly as read."""
    shown_a = key_a or trailer[:_MIFARE_TRAILER_KEY_LEN]
    ac = trailer[
        _MIFARE_TRAILER_KEY_LEN : _MIFARE_TRAILER_KEY_LEN + _MIFARE_TRAILER_AC_LEN
    ]
    shown_b = key_b or trailer[_MIFARE_TRAILER_KEY_LEN + _MIFARE_TRAILER_AC_LEN :]
    return shown_a, ac, shown_b


def _sector_display_lines(
    sector: int,
    data_hex: str,
    key_a: Optional[str] = None,
    key_b: Optional[str] = None,
) -> Tuple[List[Tuple[str, str]], Optional[SectorAccessBits]]:
    """Split a sector's dump (4 blocks x 32 hex chars) into the `(label,
    line)` rows `mifare sector` prints — labeled with the sector's real block
    numbers (the same `sector * 4` mapping `_sector_first_block` uses; 1K/2K
    sectors 0-31 only, 4K's 16-block sectors 32-39 aren't supported yet) —
    plus the decoded access bits for those blocks (None if the trailer wasn't
    a full block).

    Block 0 is colored magenta only for sector 0 (the only sector where it's
    factory UID data, not user data). A card never reads its Key A back (and
    often not Key B either) — the trailer's key bytes come back as zeros.
    `key_a`/`key_b`, when given (from `--keys-file`), are substituted into the
    display so the real keys show instead of those zeros; the
    access-conditions bytes always stay as read."""
    blocks = [
        data_hex[i : i + _MIFARE_BLOCK_HEX_LEN]
        for i in range(0, len(data_hex), _MIFARE_BLOCK_HEX_LEN)
    ]
    blocks += [""] * (4 - len(blocks))
    block0, block1, block2, trailer = blocks[:4]
    base = _sector_first_block(sector)

    line0 = f"[magenta]{block0}[/magenta]" if sector == 0 and block0 else block0

    shown_a, ac, shown_b = _substitute_trailer_keys(trailer, key_a, key_b)
    line3 = (
        f"[green]{shown_a}[/green][dark_orange3]{ac}[/dark_orange3]"
        f"[green]{shown_b}[/green]"
    )

    lines = [
        (f"block {base}", line0),
        (f"block {base + 1}", block1),
        (f"block {base + 2}", block2),
        (f"block {base + 3} (trailer)", line3),
    ]
    access = parse_access_bits(ac[:6]) if len(ac) == _MIFARE_TRAILER_AC_LEN else None
    return lines, access


def _print_access_bits(access: SectorAccessBits, base: int) -> None:
    """Render a sector's decoded access conditions under its block dump."""
    print_subtitle("Access conditions")
    for i, blk in enumerate(access.blocks):
        _print_field(
            f"block {base + i}",
            f"read {blk.read} · write {blk.write} · increment {blk.increment} · "
            f"decr/transfer/restore {blk.decrement}",
        )
    t = access.trailer
    _print_field(
        f"block {base + 3} (trailer)",
        f"key A: write {t.key_a_write} · AC: read {t.ac_read}, write {t.ac_write} "
        f"· key B: read {t.key_b_read}, write {t.key_b_write}",
    )
    if not access.valid:
        print_warning(
            "access bits fail their own inverse check — bytes may be corrupt "
            "or this trailer wasn't actually read"
        )


def _print_block0(b0: Block0) -> None:
    """Render a dissected block 0 under the raw hex the caller already
    printed - `mifare read --block 0` and `mifare sector --sector 0` both
    have it, one straight off the card, the other as the first 32 hex chars
    of the sector dump."""
    print_subtitle("Block 0 (UID sector)")
    _print_field("uid", b0.uid)
    _print_field("bcc", f"{b0.bcc}  ({'OK' if b0.bcc_valid else 'MISMATCH ⚠'})")
    _print_field("sak", f"{b0.sak}  -> {b0.sak_name}")
    _print_field("atqa", f"{b0.atqa}  -> {b0.atqa_name}")
    _print_field("mfg data", b0.manufacturer_data)
    if not b0.bcc_valid:
        print_warning(
            "BCC does not match the UID — possibly a magic/clone card with a "
            "hand-written block 0"
        )


def _ascii_render(block_hex: str) -> str:
    """Render a 32-hex-char block (16 bytes) as printable ASCII, non-printable
    bytes shown as '.' — the classic hex-dump right column. Returns "" for a
    block that isn't full/valid hex (a gap), so the caller can skip it."""
    try:
        raw = bytes.fromhex(block_hex)
    except ValueError:
        return ""
    return "".join(chr(b) if 32 <= b < 127 else "." for b in raw)


def _print_decoded_sector(entry: Dict[str, object]) -> None:
    """Print one read sector for `mifare dump --decode`: the raw block hex
    (same coloring as `mifare sector`) with an ASCII column beside each data
    block, plus the dissected block 0 for sector 0. The trailer keeps its
    key/access-bit coloring but gets no ASCII column (its bytes are keys, not
    text). `entry` is a `sector_results` item — its trailer already has the
    real keys substituted in, so no keyfile is needed here."""
    sector = entry["sector"]
    blocks = entry["blocks"]
    data_hex = "".join(blocks)
    print_subtitle(f"Sector {sector}")
    lines, _access = _sector_display_lines(sector, data_hex)
    for i, (label, line) in enumerate(lines):
        if i in _MIFARE_DATA_BLOCK_INDICES:
            _print_field(label, f"{line}  [dim]|{_ascii_render(blocks[i])}|[/dim]")
        else:
            _print_field(label, line)
    if sector == 0:
        b0 = parse_block0(blocks[0])
        if b0 is not None:
            _print_block0(b0)


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
    b0 = parse_block0(data_hex) if block == 0 else None
    if as_json:
        out = {"block": block, "data": data_hex}
        if b0 is not None:
            out["block0"] = b0.to_dict()
        print(json.dumps(out))
        return
    console.print("")
    _print_field("block", str(block))
    _print_field("data", data_hex or "[dim]—[/dim]")
    if b0 is not None:
        _print_block0(b0)


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


def _load_sector_key_pair(
    keys_file: str, sector: int
) -> Tuple[Optional[str], Optional[str]]:
    """Read `keys_file` and return ``(keyA, keyB)`` for `sector`, exiting with
    a clear error if the file is malformed or doesn't list that sector."""
    try:
        table = load_sector_keys(keys_file)
    except OSError as e:
        print_error(f"could not read {keys_file}: {e}")
        raise SystemExit(1)
    except SectorKeyfileError as e:
        print_error(str(e))
        raise SystemExit(1)
    if sector not in table:
        print_error(f"sector {sector} not found in {keys_file}")
        raise SystemExit(1)
    return table[sector]


def _read_one_sector(
    link,
    sector: int,
    key_a: Optional[str],
    key_b: Optional[str],
    timeout: float,
    first: bool,
) -> Tuple[Optional[str], Optional[str], str]:
    """Authenticate SECTOR with key A (falling back to key B) and read its 4
    blocks in one REPL call. `first` marks the very first command of the
    session — it waits for a card tap via `_run_mifare_command`; later calls
    send straight to `link.command` since a card is already selected.

    Returns `(data_hex, used_kt, reason)`: `data_hex` is the 128 hex-char
    sector dump, or None if nothing was read; `used_kt` is "A" or "B"
    (whichever key opened the sector), or None; `reason` is "ok" on success
    or the firmware's failure message otherwise.
    """
    attempts = [(kt, k) for kt, k in (("A", key_a), ("B", key_b)) if k]
    if not attempts:
        return None, None, "no key available"

    r = None
    for kt, k in attempts:
        line = f"mifare sector {sector} {kt} {k.upper()}"
        if first:
            r = _run_mifare_command(link, line, timeout)
            first = False
        else:
            r = link.command(line)
        if r.ok:
            return r.data.get("mifare_sector", ""), kt, "ok"
    assert r is not None
    return None, None, r.message


@mifare.command("sector", context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--sector", type=click.IntRange(0, 255), required=True, help="Sector number."
)
@_MIFARE_KEY_TYPE_OPTION
@click.option("--key", metavar="HEX12", help="6-byte key, 12 hex chars.")
@click.option(
    "-k",
    "--keys-file",
    "keys_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    metavar="FILE",
    help="A `sector:keyA:keyB` file from `mifare check --output-keys`. Uses "
    "the sector's key A (falling back to key B) instead of --key, and shows "
    "the real keys in the trailer line.",
)
@click.option(
    "--json", "as_json", is_flag=True, help='Emit {"sector": ..., "data": ...}.'
)
@_MIFARE_TIMEOUT_OPTION
@device_options
@click.pass_context
def mifare_sector_cmd(
    ctx, sector, key_type, key, keys_file, as_json, timeout, verbose, port, device_id
):
    """Authenticate and read every block of SECTOR in one call (self-contained).

    Pass either --key (a single 12-hex key of --key-type) or --keys-file (a
    `sector:keyA:keyB` file from `mifare check --output-keys`, which tries the
    sector's key A then key B and shows the real keys in the trailer line).
    """
    if keys_file and key:
        print_error("pass either --key or --keys-file, not both")
        raise SystemExit(1)
    if not keys_file and not key:
        print_error("one of --key or --keys-file is required")
        raise SystemExit(1)

    file_key_a: Optional[str] = None
    file_key_b: Optional[str] = None
    if keys_file:
        file_key_a, file_key_b = _load_sector_key_pair(keys_file, sector)
        key_a, key_b = file_key_a, file_key_b
        if not key_a and not key_b:
            print_error(f"sector {sector} has no key A or key B in {keys_file}")
            raise SystemExit(1)
    else:
        err = _mifare_validate_hex(key, _MIFARE_KEY_HEX_LEN, "--key")
        if err:
            print_error(err)
            raise SystemExit(1)
        key_a = key.upper() if key_type.upper() == "A" else None
        key_b = key.upper() if key_type.upper() == "B" else None

    level = _verbosity(ctx, verbose)
    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        data_hex, _used_kt, reason = _read_one_sector(
            link, sector, key_a, key_b, timeout, first=True
        )

    if reason != "ok":
        # reason is already a complete sentence from the firmware (e.g.
        # "authentication failed" or "sector read failed: key authenticated
        # but a block read was denied (access bits)") — don't re-wrap it in
        # another "sector read failed:" prefix, or the two collide into
        # nonsense like "sector read failed: sector read failed".
        print_error(f"sector {sector}: {reason}")
        raise SystemExit(1)
    assert data_hex is not None
    b0 = parse_block0(data_hex[:32]) if sector == 0 else None
    if as_json:
        out = {"sector": sector, "data": data_hex}
        if b0 is not None:
            out["block0"] = b0.to_dict()
        print(json.dumps(out))
        return
    console.print("")
    _print_field("sector", str(sector))
    if data_hex:
        lines, access = _sector_display_lines(sector, data_hex, file_key_a, file_key_b)
        for label, line in lines:
            _print_field(label, line)
        if access is not None:
            _print_access_bits(access, _sector_first_block(sector))
    else:
        console.print("  [dim]—[/dim]")
    if b0 is not None:
        _print_block0(b0)


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


# ── check ────────────────────────────────────────────────────────────────────
#
# `mifare check` — dictionary attack: try known keys against every sector
# and report which ones open. docs/CLI_IMPROVEMENTS_MifareCheck.md §6.3.


def _sector_first_block(sector: int) -> int:
    """Block that opens SECTOR's keys — 1K/2K (4-block sectors) mapping only.
    4K's sectors 32-39 (16 blocks each) aren't supported yet; isolating the
    mapping here means extending it later won't touch the sweep loop."""
    return sector * 4


# Limit of the "block = 4 * sector" mapping above (1K = 16, 2K = 32 sectors).
_MIFARE_CHECK_MAX_SECTORS = 32


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


@mifare.command("check", context_settings={"help_option_names": ["-h", "--help"]})
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


# ── dump ─────────────────────────────────────────────────────────────────────
#
# `mifare dump` — read every sector of a card in one session and save it to
# canonical JSON. Coexists with `mifare sector` (one sector, interactive);
# `dump` is batch/non-interactive and never aborts on a bad sector — see
# docs/CLI_IMPROVEMENTS_MifareDump.md §1, §5.


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


@mifare.command("dump", context_settings={"help_option_names": ["-h", "--help"]})
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
        print_title("Decoded sectors")
        for entry in sector_results:
            _print_decoded_sector(entry)

    raise SystemExit(0 if complete else 1)


# ── restore ───────────────────────────────────────────────────────────────────
#
# `mifare restore` — write a `mifare dump` JSON back to a magic card (gen2/CUID).
# docs/CLI_IMPROVEMENTS_MifareRestore.md §3 (F1: probe + block-0 confirmation +
# per-sector data-block writes; F2: trailer writes with access-bit validation
# (§3.4) + --skip-trailers).

# In a 4-block sector, index 3 is the trailer (keys + access bits); indices
# 0-2 are data blocks. Block 0 of sector 0 is the manufacturer/UID block, gated
# behind --write-block0.
_MIFARE_TRAILER_BLOCK_INDEX = 3
_MIFARE_DATA_BLOCK_INDICES = (0, 1, 2)
_MIFARE_ZERO_KEY = "0" * _MIFARE_KEY_HEX_LEN
# The 3 access-condition bytes (C1/C2/C3) sit right after key A in a trailer
# block: hex chars 12-17 (byte 9, the GPB, is not an access condition).
_MIFARE_TRAILER_AC_COND = slice(_MIFARE_TRAILER_KEY_LEN, _MIFARE_TRAILER_KEY_LEN + 6)

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
    key_b = trailer[_MIFARE_TRAILER_KEY_LEN + _MIFARE_TRAILER_AC_LEN :]
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


@mifare.command("restore", context_settings={"help_option_names": ["-h", "--help"]})
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


# ── code ──────────────────────────────────────────────────────────────────────
#
# `mifare code` — offline text → block hex. Turns a plain string into the
# 32-hex-char blocks `mifare write` expects, laid out over the data blocks of
# the sector whose keys you hold. Touches no device: it only formats, the
# writing is still `mifare write` (or `mifare restore` for a whole dump).

_MIFARE_BLOCK_BYTES = _MIFARE_BLOCK_HEX_LEN // 2  # 16 bytes per block
_MIFARE_PAD_HEX_LEN = 2  # filler byte, hex-encoded


def _sector_data_blocks(sector: int) -> List[int]:
    """Block numbers of SECTOR that can hold user data: the three blocks
    before the trailer, minus block 0 of sector 0 (the factory
    UID/manufacturer block — `mifare restore --write-block0` territory, not
    somewhere to park a string)."""
    base = _sector_first_block(sector)
    return [
        base + i for i in _MIFARE_DATA_BLOCK_INDICES if not (sector == 0 and i == 0)
    ]


def _encode_block_hex(payload: bytes, pad: int) -> List[str]:
    """Split `payload` into 16-byte blocks, padding the last one with `pad` up
    to the block boundary — a card is only ever written a whole block at a
    time, so a partial tail isn't a thing."""
    padded = payload + bytes([pad]) * (-len(payload) % _MIFARE_BLOCK_BYTES)
    return [
        padded[i : i + _MIFARE_BLOCK_BYTES].hex().upper()
        for i in range(0, len(padded), _MIFARE_BLOCK_BYTES)
    ]


@mifare.command("code", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("text")
@click.option(
    "--sector",
    type=click.IntRange(0, _MIFARE_CHECK_MAX_SECTORS - 1),
    default=None,
    help="Lay the blocks out over this sector's data blocks and label them "
    "with their real block numbers. Without it, blocks are listed unplaced.",
)
@click.option(
    "-k",
    "--keys-file",
    "keys_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    metavar="FILE",
    help="A `sector:keyA:keyB` file (see `mifare check --output-keys`). Needs "
    "--sector; prints the ready-to-run `auth`/`write` lines for that sector.",
)
@click.option(
    "--pad",
    default="00",
    show_default=True,
    metavar="HEX2",
    help="Filler byte for the tail of the last block, 2 hex chars.",
)
@click.option(
    "--json", "as_json", is_flag=True, help="Emit the encoding as one JSON object."
)
def mifare_code_cmd(text, sector, keys_file, pad, as_json):
    """Encode TEXT as the block hex a Mifare Classic sector stores.

    Offline — no card, no device. TEXT is UTF-8 encoded and padded out to
    whole 16-byte blocks (32 hex chars each), the shape `mifare write --data`
    takes. With --sector the blocks are placed on that sector's data blocks
    (its trailer is never touched, nor block 0 of sector 0), and with
    --keys-file the exact `auth`/`write` command lines are printed too.
    """
    err = _mifare_validate_hex(pad, _MIFARE_PAD_HEX_LEN, "--pad")
    if err:
        print_error(err)
        raise SystemExit(1)
    if keys_file and sector is None:
        print_error("--keys-file needs --sector")
        raise SystemExit(1)

    payload = text.encode("utf-8")
    if not payload:
        print_error("TEXT is empty — nothing to encode")
        raise SystemExit(1)

    blocks = _encode_block_hex(payload, int(pad, 16))

    targets: List[Optional[int]] = [None] * len(blocks)
    if sector is not None:
        available = _sector_data_blocks(sector)
        if len(blocks) > len(available):
            capacity = len(available) * _MIFARE_BLOCK_BYTES
            print_error(
                f"text is {len(payload)} byte(s) ({len(blocks)} block(s)) but "
                f"sector {sector} holds {capacity} byte(s) in "
                f"{len(available)} data block(s)"
            )
            raise SystemExit(1)
        targets = list(available[: len(blocks)])

    key_type: Optional[str] = None
    key: Optional[str] = None
    if keys_file:
        key_a, key_b = _load_sector_key_pair(keys_file, sector)
        if key_a:
            key_type, key = "A", key_a
        elif key_b:
            key_type, key = "B", key_b
        else:
            print_error(f"sector {sector} has no key A or key B in {keys_file}")
            raise SystemExit(1)

    commands: List[str] = []
    if targets[0] is not None:
        if key:
            commands.append(
                f"bombercat tags mifare auth --block {targets[0]} "
                f"--key-type {key_type} --key {key}"
            )
        for block, data in zip(targets, blocks):
            commands.append(
                f"bombercat tags mifare write --block {block} --data {data}"
            )

    if as_json:
        out: Dict[str, object] = {
            "text": text,
            "encoding": "utf-8",
            "bytes": len(payload),
            "padded_bytes": len(blocks) * _MIFARE_BLOCK_BYTES,
            "pad": pad.upper(),
            "sector": sector,
            "blocks": [
                {"index": i, "block": block, "data": data}
                for i, (block, data) in enumerate(zip(targets, blocks))
            ],
        }
        if commands:
            out["commands"] = commands
        print(json.dumps(out))
        return

    console.print("")
    _print_field("text", text)
    _print_field(
        "bytes",
        f"{len(payload)} → {len(blocks) * _MIFARE_BLOCK_BYTES} padded with "
        f"0x{pad.upper()} ({len(blocks)} block(s))",
    )
    if sector is not None:
        _print_field("sector", str(sector))
    if key:
        _print_field("key", f"{key} (key {key_type}, from {keys_file})")
    print_subtitle("Blocks")
    for i, (block, data) in enumerate(zip(targets, blocks)):
        _print_field(f"block {block}" if block is not None else f"[{i}]", data)
    if commands:
        print_subtitle("Write it")
        for line in commands:
            # soft_wrap: a wrapped command line can't be copy-pasted.
            console.print(f"  {line}", style="dim", soft_wrap=True)
    elif sector is None:
        print_dim("pass --sector to place these on a sector's data blocks")


# ── write-text ───────────────────────────────────────────────────────────────
#
# `mifare write-text` — `code`'s encoder wired to a real write pass: the same
# UTF-8 → block hex layout, then an auth with the keys you hold (--keys-file
# or --key) and one `mifare write` per block. What you'd get by copy-pasting
# `mifare code --keys-file`'s auth/write lines, in a single command.


def _auth_sector(
    link,
    sector: int,
    key_a: Optional[str],
    key_b: Optional[str],
    timeout: float,
    first: bool,
) -> Tuple[Optional[str], str]:
    """Authenticate SECTOR with key A, falling back to key B. `first` marks
    the very first command of the session — it waits for a card tap via
    `_run_mifare_command`; later calls go straight to `link.command` since a
    card is already selected.

    Returns ``(used_kt, reason)``: the key type ("A"/"B") that opened the
    sector and "ok", or ``(None, <firmware message>)``.
    """
    base = _sector_first_block(sector)
    attempts = [(kt, k) for kt, k in (("A", key_a), ("B", key_b)) if k]
    if not attempts:
        return None, "no key available"

    r = None
    for kt, k in attempts:
        line = f"mifare auth {base} {kt} {k.upper()}"
        if first:
            r = _run_mifare_command(link, line, timeout)
            first = False
        else:
            r = link.command(line)
        if r.ok:
            return kt, "ok"
    assert r is not None
    return None, r.message


def _resolve_text_target(
    sector: Optional[int], block: Optional[int]
) -> Tuple[int, List[int]]:
    """Turn --sector/--block into ``(sector, blocks)``: the sector to
    authenticate and the data blocks usable from the starting point on.

    With --block the sector is derived from it (4 blocks per sector) and the
    run starts at that block; with --sector it starts at the sector's first
    data block. A block that can't hold user data — a trailer, or sector 0's
    manufacturer block — is refused here rather than by the firmware."""
    if block is not None:
        sector = block // 4
        available = _sector_data_blocks(sector)
        if block not in available:
            what = (
                "sector 0's manufacturer block (UID/BCC/SAK/ATQA)"
                if block == 0
                else f"sector {sector}'s trailer (keys + access bits)"
            )
            print_error(
                f"block {block} can't hold text: it is {what} — data blocks of "
                f"sector {sector} are {', '.join(str(b) for b in available)}"
            )
            raise SystemExit(1)
        return sector, available[available.index(block) :]
    assert sector is not None
    return sector, _sector_data_blocks(sector)


def _resolve_write_keys(
    keys_file: Optional[str], key: Optional[str], key_type: str, sector: int
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the sector's write keys from --keys-file or --key, the same way
    `mifare sector` resolves its read keys. Returns ``(key_a, key_b)`` with
    the slot(s) available to try."""
    if keys_file:
        key_a, key_b = _load_sector_key_pair(keys_file, sector)
        if not key_a and not key_b:
            print_error(f"sector {sector} has no key A or key B in {keys_file}")
            raise SystemExit(1)
        return key_a, key_b
    err = _mifare_validate_hex(key, _MIFARE_KEY_HEX_LEN, "--key")
    if err:
        print_error(err)
        raise SystemExit(1)
    if key_type.upper() == "A":
        return key.upper(), None
    return None, key.upper()


@mifare.command("write-text", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("text")
@click.option(
    "--sector",
    type=click.IntRange(0, _MIFARE_CHECK_MAX_SECTORS - 1),
    default=None,
    help="Target sector — the text starts at its first data block.",
)
@click.option(
    "--block",
    type=click.IntRange(0, _MIFARE_CHECK_MAX_SECTORS * 4 - 1),
    default=None,
    help="Start at this absolute block instead of the sector's first data "
    "block; its sector is derived (4 blocks per sector).",
)
@_MIFARE_KEY_TYPE_OPTION
@click.option("--key", metavar="HEX12", help="6-byte key, 12 hex chars.")
@click.option(
    "-k",
    "--keys-file",
    "keys_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    metavar="FILE",
    help="A `sector:keyA:keyB` file (see `mifare check --output-keys`). Uses "
    "the target sector's key A, falling back to key B, instead of --key.",
)
@click.option(
    "--pad",
    default="00",
    show_default=True,
    metavar="HEX2",
    help="Filler byte for the tail of the last block, 2 hex chars.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the write result as JSON.")
@_MIFARE_TIMEOUT_OPTION
@device_options
@click.pass_context
def mifare_write_text_cmd(
    ctx,
    text,
    sector,
    block,
    key_type,
    key,
    keys_file,
    pad,
    as_json,
    timeout,
    verbose,
    port,
    device_id,
):
    """Encode TEXT and write it to a sector's data blocks in one pass.

    \b
    ⚠ AUTHORIZED USE ONLY. Write only to cards you own or have explicit
    permission to write.

    `mifare code` + `mifare write`: TEXT is UTF-8 encoded and padded out to
    whole 16-byte blocks, the sector is authenticated with the key you pass
    (--key) or the one listed for it in --keys-file, and each block is written
    in order. Pick the destination with --sector (starts at its first data
    block) or --block (starts there, its sector derived). Trailers and sector
    0's block 0 are never written — use `mifare restore` for those. A write
    that's denied stops the run and reports the blocks written so far.
    """
    if (sector is None) == (block is None):
        print_error("pass exactly one of --sector or --block")
        raise SystemExit(1)
    if keys_file and key:
        print_error("pass either --key or --keys-file, not both")
        raise SystemExit(1)
    if not keys_file and not key:
        print_error("one of --key or --keys-file is required")
        raise SystemExit(1)
    err = _mifare_validate_hex(pad, _MIFARE_PAD_HEX_LEN, "--pad")
    if err:
        print_error(err)
        raise SystemExit(1)

    payload = text.encode("utf-8")
    if not payload:
        print_error("TEXT is empty — nothing to write")
        raise SystemExit(1)

    blocks = _encode_block_hex(payload, int(pad, 16))
    sector, available = _resolve_text_target(sector, block)
    if len(blocks) > len(available):
        capacity = len(available) * _MIFARE_BLOCK_BYTES
        print_error(
            f"text is {len(payload)} byte(s) ({len(blocks)} block(s)) but "
            f"only {len(available)} data block(s) of sector {sector} are "
            f"available from block {available[0]} on ({capacity} byte(s))"
        )
        raise SystemExit(1)
    targets = available[: len(blocks)]
    key_a, key_b = _resolve_write_keys(keys_file, key, key_type, sector)

    level = _verbosity(ctx, verbose)
    written: List[Dict[str, object]] = []
    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        if not as_json:
            print_info(
                f"Writing {len(blocks)} block(s) to sector {sector} "
                f"(blocks {targets[0]}-{targets[-1]}) on {target}"
            )
        used_kt, reason = _auth_sector(link, sector, key_a, key_b, timeout, first=True)
        if used_kt is not None:
            for target_block, data in zip(targets, blocks):
                w = link.command(f"mifare write {target_block} {data}")
                if not w.ok:
                    reason = f"block {target_block} write failed: {w.message}"
                    break
                written.append({"block": target_block, "data": data})

    complete = reason == "ok"
    result: Dict[str, object] = {
        "text": text,
        "encoding": "utf-8",
        "bytes": len(payload),
        "padded_bytes": len(blocks) * _MIFARE_BLOCK_BYTES,
        "pad": pad.upper(),
        "sector": sector,
        "key_type": used_kt,
        "blocks_total": len(blocks),
        "blocks_written": len(written),
        "blocks": written,
        "reason": reason,
        "written_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    if as_json:
        print(json.dumps(result))
        raise SystemExit(0 if complete else 1)

    console.print("")
    _print_field("text", text)
    _print_field(
        "bytes",
        f"{len(payload)} → {len(blocks) * _MIFARE_BLOCK_BYTES} padded with "
        f"0x{pad.upper()} ({len(blocks)} block(s))",
    )
    _print_field("sector", str(sector))
    if used_kt:
        _print_field(
            "key",
            f"key {used_kt}" + (f", from {keys_file}" if keys_file else ""),
        )
    if written:
        print_subtitle("Blocks written")
        for entry in written:
            _print_field(f"block {entry['block']}", str(entry["data"]))
    if not complete:
        print_error(f"sector {sector}: {reason}")
        raise SystemExit(1)
    print_success(f"wrote {len(written)} block(s) to sector {sector}")
