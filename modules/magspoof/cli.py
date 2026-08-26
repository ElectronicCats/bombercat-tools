#!/usr/bin/env python3

# Electronic Cats
# `bombercat magspoof play|set|show|button|watch|info` — magstripe emulation control
# over the magspoof REPL. Unlike tags/readers (passive observers), play/set
# actively drive the board. No aggregator/scan: `:mag` events are discrete
# and just get counted. docs/IMPLEMENTATION_PLAN_MagSpoof_CLI.md §3.2.
# Distributed as-is; no warranty is given.

import json
import re
import time
from contextlib import contextmanager
from typing import Dict, Iterator, Optional, Tuple

import click
from rich.table import Table

from ..core.bombercat import DeviceLink, Response, resolve_port
from ..utils.cli_options import device_options
from ..utils.detection_cli import (
    device_session,
    print_field as _print_field,
    verbosity as _verbosity,
)
from ..utils.output import console, make_tracer, print_error, print_info, print_success
from .parser import MagEvent, MagEventParser

# How long `magspoof info` listens for a ':mag' event before concluding none
# has been seen yet.
_INFO_PROBE_SECONDS = 2.0

# Firmware chatter the CLI's own (non -v) output hides by default in `watch`:
# boot/idle noise, not a reproduction event.
_NOISE_RE = re.compile(
    r"^(Activating MagSpoof\.\.\.|Default tracks:|Track [12]: .*|"
    r"Updated tracks:|Press the MagSpoof button)\s*$"
)


# How the firmware's ':btn' modes read in human output. "alt" is the firmware
# default: the button walks 1, 2, 1, 2… on successive presses.
_BUTTON_MODES = {"1": "track 1", "2": "track 2", "alt": "alternating 1 and 2"}


def _button_label(mode: str) -> str:
    """Human name for a ':btn' value, passing anything unexpected through
    rather than inventing a mode the firmware never claimed."""
    return _BUTTON_MODES.get(mode, mode)


@click.group("magspoof", context_settings={"help_option_names": ["-h", "--help"]})
def magspoof():
    """Magstripe emulation commands (requires the magspoof firmware)."""


@contextmanager
def _magspoof_session(
    port: Optional[str],
    device_id: Optional[int] = None,
    trace=None,
) -> Iterator[Tuple[str, DeviceLink]]:
    """Open a verified link for the `magspoof` commands, yield ``(target,
    link)``, and always close it. Thin, magspoof-flavored wrapper around
    `detection_cli.device_session` — `resolve_port`/`DeviceLink` are passed
    in explicitly so tests can still monkeypatch this module's copies."""
    with device_session(
        resolve_port, DeviceLink, "magspoof", "MagSpoof", port, device_id, trace
    ) as pair:
        yield pair


def _report_error(verb: str, r: Response) -> None:
    print_error(f"{verb} failed: {r.message}")
    if "unknown command" in r.message:
        # Same version (1.1.1.0) before and after the FW-4 hook, so `info`
        # can't tell old/new apart — this reply is the only tell.
        print_info(
            "this firmware predates magplay/magset/magget/magbtn — reflash the "
            "magspoof image (bombercat flash magspoof) to use it."
        )


# ── play ─────────────────────────────────────────────────────────────────────


@magspoof.command("play", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("track", required=False, type=click.Choice(["1", "2"]))
@device_options
@click.pass_context
def play_cmd(ctx, track, verbose, port, device_id):
    """Emulate a track now — same effect as pressing the physical button.

    With no TRACK, the firmware alternates between 1 and 2, just like the
    button. Reproduction blocks the device for ~0.6-1.5s before the reply
    arrives (worst case); that delay is normal, not a stall.

    Exit code: 0 played, 1 error (including old firmware without magplay).
    """
    level = _verbosity(ctx, verbose)
    with _magspoof_session(port, device_id, trace=make_tracer(level)) as (target, link):
        cmd = "magplay" if track is None else f"magplay {track}"
        # DEFAULT_TIMEOUT*4 (8s) already covers the ~1.5s worst case; no
        # explicit read_timeout needed unless DEFAULT_TIMEOUT is shortened.
        r = link.command(cmd)

    if not r.ok:
        _report_error("play", r)
        raise SystemExit(1)
    played = r.message.split()[-1] if r.message else "?"
    print_success(f"played track {played}")


# ── set ──────────────────────────────────────────────────────────────────────


def _validate_track_data(track: int, data: str) -> Optional[str]:
    """Mirror the firmware's `magset` validation locally so bad input never
    makes the serial round trip. Returns an error message, or None if valid."""
    if "\n" in data or "\r" in data:
        return "DATA cannot contain a newline"
    if len(data) > 126:
        return f"track too long ({len(data)} chars, max 126)"
    expected_start = "%" if track == 1 else ";"
    if len(data) < 3 or data[0] != expected_start or data[-1] != "?":
        return (
            f"bad track {track} format — expected an ISO track starting with "
            f"{expected_start!r} and ending with '?'"
        )
    return None


@magspoof.command("set", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("track", type=click.Choice(["1", "2"]))
@click.argument("data")
@device_options
@click.pass_context
def set_cmd(ctx, track, data, verbose, port, device_id):
    """Load DATA onto TRACK (1 or 2).

    Quote DATA in the shell: the protocol preserves internal spaces, but a
    literal newline breaks the serial line and is rejected locally.
    """
    error = _validate_track_data(int(track), data)
    if error:
        print_error(error)
        raise SystemExit(1)

    level = _verbosity(ctx, verbose)
    with _magspoof_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = link.command(f"magset {track} {data}")

    if not r.ok:
        _report_error("set", r)
        raise SystemExit(1)
    print_success(r.message or f"track {track} set")


# ── show ─────────────────────────────────────────────────────────────────────


@magspoof.command("show", context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help='Emit {"t1": ..., "t2": ..., "btn": ...}.',
)
@device_options
@click.pass_context
def show_cmd(ctx, as_json, verbose, port, device_id):
    """Print the tracks currently loaded on the device."""
    level = _verbosity(ctx, verbose)
    with _magspoof_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = link.command("magget")

    if not r.ok:
        _report_error("show", r)
        raise SystemExit(1)

    t1 = r.data.get("t1", "")
    t2 = r.data.get("t2", "")
    # Firmware older than the magbtn hook answers magget without ':btn'.
    btn = r.data.get("btn", "")
    if as_json:
        print(json.dumps({"t1": t1, "t2": t2, "btn": btn}))
        return
    _print_field("track 1", t1 or "[dim]—[/dim]")
    _print_field("track 2", t2 or "[dim]—[/dim]")
    _print_field("button", _button_label(btn) if btn else "[dim]—[/dim]")


# ── button ───────────────────────────────────────────────────────────────────


@magspoof.command("button", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("mode", required=False, type=click.Choice(["1", "2", "alt"]))
@device_options
@click.pass_context
def button_cmd(ctx, mode, verbose, port, device_id):
    """Show or set which track the physical button plays.

    With no MODE, reports the current setting. `1` or `2` pin the button to
    that track — useful when the reader you swipe past only decodes one of
    them — and `alt` restores the firmware default of alternating on every
    press. Like the tracks themselves the setting lives in RAM, so a reset
    or a reflash brings `alt` back.
    """
    level = _verbosity(ctx, verbose)
    with _magspoof_session(port, device_id, trace=make_tracer(level)) as (target, link):
        # Both forms answer ':btn <mode>' + '+OK', so query and set read alike.
        r = link.command("magbtn" if mode is None else f"magbtn {mode}")

    if not r.ok:
        _report_error("button", r)
        raise SystemExit(1)
    current = r.data.get("btn") or mode or "?"
    print_success(f"button plays {_button_label(current)}")


# ── watch ────────────────────────────────────────────────────────────────────


def _event_to_dict(event: MagEvent) -> Dict[str, object]:
    d: Dict[str, object] = {"ts_ms": event.ts_ms, "track": event.track}
    d.update(event.extra)
    return d


def _watch_line(event: MagEvent) -> str:
    ts = f"{event.ts_ms / 1000:.1f}s" if event.ts_ms is not None else "?"
    track = (
        event.track if event.track is not None else event.extra.get("raw_track", "?")
    )
    return f"▶ track {track} @ {ts}"


@magspoof.command("watch", context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--quiet-noise/--no-quiet-noise",
    default=True,
    show_default=True,
    help="Hide firmware boot/idle chatter (Activating MagSpoof..., etc).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit one JSON object per line.")
@device_options
@click.pass_context
def watch_cmd(ctx, quiet_noise, as_json, verbose, port, device_id):
    """Stream every reproduction — by command or physical button — until Ctrl-C."""
    level = _verbosity(ctx, verbose)
    plays = 0
    counts: Dict[int, int] = {}
    start = time.monotonic()
    with _magspoof_session(port, device_id, trace=make_tracer(level)) as (target, link):
        if not as_json:
            print_info(f"Watching {target} — Ctrl-C to stop")
        parser = MagEventParser()
        try:
            for line in link.stream():
                if not line:
                    continue
                event = parser.feed(line)
                if event is None:
                    if (
                        not quiet_noise
                        and not as_json
                        and _NOISE_RE.match(line.strip())
                    ):
                        console.print(f"[dim]{line}[/dim]")
                    continue

                plays += 1
                if event.track is not None:
                    counts[event.track] = counts.get(event.track, 0) + 1
                if as_json:
                    print(json.dumps(_event_to_dict(event)))
                else:
                    console.print(_watch_line(event))
        except KeyboardInterrupt:
            pass

    if not as_json:
        duration = time.monotonic() - start
        console.print("")
        print_info(
            f"{plays} plays, track 1 ×{counts.get(1, 0)}, "
            f"track 2 ×{counts.get(2, 0)}, {duration:.0f}s"
        )


# ── info ─────────────────────────────────────────────────────────────────────


@magspoof.command("info", context_settings={"help_option_names": ["-h", "--help"]})
@device_options
@click.pass_context
def info_cmd(ctx, verbose, port, device_id):
    """Report what this magspoof image can do.

    Shows the firmware version and state, and whether a ':mag' event has
    been seen during a short probe window.
    """
    level = _verbosity(ctx, verbose)
    with _magspoof_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = link.info()
        if not r.ok:
            print_error(f"info failed: {r.message}")
            raise SystemExit(1)

        parser = MagEventParser()
        deadline = time.monotonic() + _INFO_PROBE_SECONDS
        for line in link.stream(yield_empty=True):
            if line:
                parser.feed(line)
            if parser.structured or time.monotonic() > deadline:
                break

    table = Table(
        title=f"magspoof @ {target}", header_style="cyan bold", show_header=False
    )
    table.add_column("field", style="cyan")
    table.add_column("value")
    table.add_row("version", r.data.get("fw") or "[dim]—[/dim]")
    if parser.structured:
        events = "structured (':mag' events)"
    else:
        events = "no ':mag' events seen yet"
    table.add_row("events", events)
    table.add_row("state", r.data.get("state") or "[dim]—[/dim]")
    console.print(table)
