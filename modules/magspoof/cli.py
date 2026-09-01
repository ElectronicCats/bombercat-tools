#!/usr/bin/env python3

# Electronic Cats
# `bombercat magspoof play|show|watch|info|card` — magstripe emulation
# control over the magspoof REPL. Unlike tags/readers (passive observers), play
# and card edits actively drive the board. Tracks now live in a persistent
# multi-card store (the `card` subgroup); play/show act on the active
# card. No aggregator/scan: `:mag` events are discrete
# and just get counted. docs/IMPLEMENTATION_PLAN_MagSpoof_CLI.md §3.2;
# play drives the swipe the active card actually needs — both tracks for a
# financial card, the lone one for a membership card — see
# docs/PLAN_UNIFIED_TRACK_PLAYBACK.md.
# Distributed as-is; no warranty is given.

import json
import re
import time
from contextlib import contextmanager
from typing import Dict, Iterator, Optional, Tuple

import click
from click.shell_completion import CompletionItem
from rich.table import Table

from ..core.bombercat import DeviceLink, Response, resolve_port
from ..core.usb_connection import find_device
from ..utils.cli_options import device_options
from ..utils.detection_cli import (
    device_session,
    print_field as _print_field,
    verbosity as _verbosity,
)
from ..utils.output import console, make_tracer, print_error, print_info, print_success
from .parser import MagEvent, MagEventParser
from .track2 import normalize_track2, parse_track2
from .track_parser import TrackStandard, analyze_card, card_analysis_to_dict

# How long `magspoof info` listens for a ':mag' event before concluding none
# has been seen yet.
_INFO_PROBE_SECONDS = 2.0

# Ceiling on the `magcard list` a <TAB> fires to complete a card name. Short so
# a stalled/wrong board makes the shell hesitate briefly, not hang.
_COMPLETION_READ_TIMEOUT = 1.5

# Firmware chatter the CLI's own (non -v) output hides by default in `watch`:
# boot/idle noise, not a reproduction event.
_NOISE_RE = re.compile(
    r"^(Activating MagSpoof\.\.\.|Default tracks:|Track [12]: .*|"
    r"Updated tracks:|Press the MagSpoof button)\s*$"
)


# How the firmware's ':btn' modes read in `show`/`card info`. Report-only: no
# command writes the mode, because the firmware's default ("alt") already makes
# the physical button swipe whatever the active card carries — modes "alt" and
# "1" both run playActiveCard(). "2" is the one mode that diverges, pinning the
# button to track 2, and only raw serial (`magbtn 2`) can get a board there.
_BUTTON_MODES = {
    "1": "active card",
    "2": "track 2 only",
    "alt": "alternating 1 and 2",
}


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
@device_options
@click.pass_context
def play_cmd(ctx, verbose, port, device_id):
    """Emulate a swipe of the active card.

    Same effect as pressing the physical button. A two-track card plays track 1
    forward then track 2 in reverse (what a reader sees on a real swipe); a
    single-track membership card plays just the track it carries. Reproduction
    blocks the device for ~0.6-1.5s before the reply arrives (worst case); that
    delay is normal, not a stall.

    Exit code: 0 played, 1 error (including old firmware without magplay).
    """
    level = _verbosity(ctx, verbose)
    with _magspoof_session(port, device_id, trace=make_tracer(level)) as (target, link):
        # Bare `magplay` lets the firmware pick the track(s) the active card
        # actually holds: a full 1-then-2 swipe for a two-track card, or the
        # lone track of a single-track card. DEFAULT_TIMEOUT*4 (8s) already
        # covers the ~1.5s worst case; no explicit read_timeout needed.
        r = link.command("magplay")

    if not r.ok:
        _report_error("play", r)
        raise SystemExit(1)
    # Firmware answers "+OK played <track>"; render it as a sentence.
    msg = r.message or ""
    if msg.startswith("played "):
        print_success(f"played track {msg.split()[-1]}")
    else:
        print_success(msg or "played card")


# ── validation helpers ───────────────────────────────────────────────────────


def _validate_card_name(name: str) -> Optional[str]:
    """Mirror the firmware's `validName` (CardDatabase): non-empty, at most 31
    chars, no spaces or control characters so the space-separated `magcard`
    parser can round-trip it. Returns an error message, or None if valid."""
    if not name:
        return "card name cannot be empty"
    if len(name) > 31:
        return f"card name too long ({len(name)} chars, max 31)"
    for ch in name:
        if ch <= " " or ch == "\x7f":
            return "card name cannot contain spaces or control characters"
    return None


def _validate_track_data(track: int, data: str) -> Optional[str]:
    """Mirror the firmware's track validation (validateTrack, shared by
    `magcard set`) locally so bad input never makes the serial round trip.
    Returns an error message, or None if valid."""
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


# ── show ─────────────────────────────────────────────────────────────────────


_SC_STATUS_STYLE = {
    "OK_FALLBACK": "green",
    "REQUIRES_CHIP": "red",
    "REQUIRES_PIN": "yellow",
    "REQUIRES_CHIP_AND_PIN": "red bold",
}

# Raw TrackStandard enum values read like machine keys ("iso7813_financial");
# `show` prints these human names instead. Anything unmapped falls back to the
# enum's own value so a new standard never renders as a blank.
_STANDARD_LABELS = {
    TrackStandard.ISO_7813_FINANCIAL: "ISO 7813 financial (payment card)",
    TrackStandard.PBOC_UNIONPAY: "PBOC / UnionPay (financial)",
    TrackStandard.AAMVA_DL: "AAMVA driver's license / ID",
    TrackStandard.LOYALTY_GENERIC: "loyalty / transit / generic",
    TrackStandard.UNKNOWN: "unknown",
}

# One-glance security verdict for a financial card's Service Code: (style,
# icon+label). Mirrors _SC_STATUS_STYLE's colours but spells out what the swipe
# will face, so the reader doesn't have to decode chip/PIN rows themselves.
_SC_STATUS_BADGE = {
    "OK_FALLBACK": ("green", "✓ magstripe fallback allowed"),
    "REQUIRES_CHIP": ("red", "⚠ chip required — swipe may be refused"),
    "REQUIRES_PIN": ("yellow", "⚠ PIN required"),
    "REQUIRES_CHIP_AND_PIN": (
        "red bold",
        "⚠ chip + PIN required — swipe may be refused",
    ),
    "UNKNOWN": ("dim", "? non-standard service code"),
}


def _standard_label(std: TrackStandard) -> str:
    """Human name for a detected TrackStandard, passing an unmapped one through
    as its raw value rather than rendering nothing."""
    return _STANDARD_LABELS.get(std, std.value)


def _format_expiry(yymm: str) -> str:
    """A 4-digit ISO YYMM expiry as friendly MM/YY; anything else unchanged."""
    if len(yymm) == 4 and yymm.isdigit():
        return f"{yymm[2:]}/{yymm[:2]}"
    return yymm


def _group_pan(pan: str) -> str:
    """Space a PAN into 4-digit groups the way it reads on the card face."""
    return " ".join(pan[i : i + 4] for i in range(0, len(pan), 4))


@magspoof.command("show", context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help='Emit {"t1": ..., "t2": ..., "btn": ..., "analysis": {...}}.',
)
@click.option(
    "--verbose-analysis/--no-verbose-analysis",
    default=True,
    show_default=True,
    help=(
        "Show detected standard (ISO 7813 financial, PBOC/UnionPay, AAMVA "
        "driver's license, loyalty/transit) + Service Code analysis "
        "(chip/PIN/fallback) for financial cards."
    ),
)
@device_options
@click.pass_context
def show_cmd(ctx, as_json, verbose_analysis, verbose, port, device_id):
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

    analysis = analyze_card(t1, t2) if verbose_analysis else None

    if as_json:
        payload: Dict[str, object] = {"t1": t1, "t2": t2, "btn": btn}
        if analysis is not None:
            payload["analysis"] = card_analysis_to_dict(analysis)
        print(json.dumps(payload))
        return

    # ── device state ─────────────────────────────────────────────
    console.print(f"[cyan bold]magspoof card[/cyan bold] [dim]@ {target}[/dim]")
    console.print("")
    _print_field("track 1", t1 or "[dim]—[/dim]")
    _print_field("track 2", t2 or "[dim]—[/dim]")
    _print_field("button", _button_label(btn) if btn else "[dim]—[/dim]")

    if analysis is None:
        return

    # ── decoded analysis ─────────────────────────────────────────
    console.print("")
    console.print(f"  [dim]── analysis {'─' * 30}[/dim]")
    _print_field("standard", _standard_label(analysis.primary_standard))

    # Decode the raw track into card-face fields: the cardholder name only
    # rides Track 1, PAN/expiry sit on both — prefer whichever track parsed.
    parsed1 = analysis.track1.parsed if analysis.track1 else None
    parsed2 = analysis.track2.parsed if analysis.track2 else None
    name = getattr(parsed1, "name", "")
    pan = getattr(parsed2, "pan", "") or getattr(parsed1, "pan", "")
    expiry = getattr(parsed2, "expiration", "") or getattr(parsed1, "expiration", "")
    if name:
        _print_field("cardholder", name)
    if pan:
        _print_field("PAN", _group_pan(pan))
    if expiry:
        _print_field("expires", _format_expiry(expiry))

    sca = analysis.track2.service_code_analysis if analysis.track2 else None
    if sca is None and analysis.track1:
        sca = analysis.track1.service_code_analysis
    if sca:
        badge_style, badge_text = _SC_STATUS_BADGE.get(
            sca.status, ("white", sca.status)
        )
        style = _SC_STATUS_STYLE.get(sca.status, "white")
        sc_display = (
            f"[{style}]{sca.original}[/{style}] → {sca.normalized}"
            if sca.original != sca.normalized
            else f"[{style}]{sca.original}[/{style}] (already normalized)"
        )
        _print_field("service code", sc_display)
        _print_field("security", f"[{badge_style}]{badge_text}[/{badge_style}]")
        _print_field(
            "chip required",
            "[yellow]yes[/yellow]" if sca.requires_chip else "[green]no[/green]",
        )
        _print_field(
            "PIN required",
            "[yellow]yes[/yellow]" if sca.requires_pin else "[green]no[/green]",
        )

    if analysis.recommendations:
        console.print("")
    for rec in analysis.recommendations:
        print_info(f"use: {rec}")


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


# ── nfc (PN7150) ─────────────────────────────────────────────────────────────
#
# NFC commands for the PN7150 side of magspoof (IMPLEMENTATION_PLAN_NFC_
# VISA_MAGSPOOF.md). `nfcselres` (Phase 2), `nfcvisa` (Phase 3), `nfcread`
# (Phase 4) and `nfcinfo` (Phase 6) are all wired up in firmware.

# Ceiling matching the firmware's own VISA_MSD_TIMEOUT_MS (15s, see
# emulateVisaMSD() in magspoof.ino) plus margin, so the client doesn't time
# out first while `nfcvisa` blocks the REPL waiting for a terminal tap.
_NFC_VISA_READ_TIMEOUT = 17.0

# Ceiling covering the firmware's own NFC_READ_WAIT_TAG_MS (8s tag-wait, see
# handleCommand()'s "nfcread" branch in magspoof.ino) plus the
# PPSE/VISA-AID/GPO/READ-RECORD transceive sequence that follows it, so the
# client doesn't time out first while `nfcread` blocks the REPL.
_NFC_READ_READ_TIMEOUT = 12.0


@magspoof.group("nfc", context_settings={"help_option_names": ["-h", "--help"]})
def nfc():
    """NFC (PN7150) commands (requires the magspoof firmware)."""


@nfc.command("selres", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("mode", type=click.Choice(["chip", "nochip"]))
@device_options
@click.pass_context
def nfc_selres_cmd(ctx, mode, verbose, port, device_id):
    """Set the PN7150's SEL_RES chip bit.

    'chip' (0x33) advertises ISO-DEP/EMV support; 'nochip' (0x13) forces MSD
    (magstripe) fallback. This is a manual override — it applies immediately
    but is reset back to the current mode's default (chip for reader, nochip
    for emulation) the next time resetNfc() runs on the device.
    """
    level = _verbosity(ctx, verbose)
    with _magspoof_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = link.command(f"nfcselres {mode}")

    if not r.ok:
        _report_error("nfc selres", r)
        raise SystemExit(1)
    print_success(f"SEL_RES set to {mode}")


@nfc.command("visa", context_settings={"help_option_names": ["-h", "--help"]})
@device_options
@click.pass_context
def nfc_visa_cmd(ctx, verbose, port, device_id):
    """Start a VISA MSD contactless emulation session.

    Switches the PN7150 into emulation mode (SEL_RES nochip, so the terminal
    falls back to magstripe instead of attempting EMV crypto) and emulates
    the active card's Track 2 — or a built-in fallback token if the active
    card has none — through the PPSE/VISA-AID/GPO/READ-RECORD exchange.
    Blocks up to 15s waiting for a terminal tap; tap the BomberCat to a
    contactless reader once this starts.
    """
    level = _verbosity(ctx, verbose)
    with _magspoof_session(port, device_id, trace=make_tracer(level)) as (target, link):
        print_info("waiting for a contactless tap (up to 15s)...")
        r = link.command("nfcvisa", read_timeout=_NFC_VISA_READ_TIMEOUT)

    if not r.ok:
        _report_error("nfc visa", r)
        raise SystemExit(1)
    print_success("VISA MSD emulation complete")


@nfc.command("read", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("name")
@device_options
@click.pass_context
def nfc_read_cmd(ctx, name, verbose, port, device_id):
    """Read a physical EMV/Visa card's Track 2 over NFC and store it on NAME.

    Switches the PN7150 into reader mode, waits up to 8s for a card to enter
    the field, then runs the PPSE/VISA-AID/GPO/READ-RECORD sequence once.
    NAME picks new-vs-existing: an existing card gets its Track 2 updated
    (Track 1, if any, is left untouched); a name not yet in the store is
    created fresh and the scanned Track 2 written onto it. Present the card
    once this starts.
    """
    name_err = _validate_card_name(name)
    if name_err:
        print_error(name_err)
        raise SystemExit(1)

    level = _verbosity(ctx, verbose)
    with _magspoof_session(port, device_id, trace=make_tracer(level)) as (target, link):
        print_info("waiting for a card (up to 8s)...")
        r = link.command(f"nfcread {name}", read_timeout=_NFC_READ_READ_TIMEOUT)

    if not r.ok:
        _report_error("nfc read", r)
        raise SystemExit(1)
    stored_name = r.data.get("name", name)
    is_new = "new card" in (r.message or "")
    verb = "created" if is_new else "updated"
    print_success(f"{verb} {stored_name} — stored track 2: {r.data.get('t2', '')}")


@nfc.command("info", context_settings={"help_option_names": ["-h", "--help"]})
@device_options
@click.pass_context
def nfc_info_cmd(ctx, verbose, port, device_id):
    """Report the PN7150's firmware version, current RF role, SEL_RES state
    and whatever tag was last detected.

    A pure status read — unlike `nfc visa`/`nfc read` it never switches mode
    or waits for a tag, so "tag" reflects the last detection (from `nfc
    read`, or the reader-mode bring-up at boot), not a fresh probe.
    """
    level = _verbosity(ctx, verbose)
    with _magspoof_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = link.command("nfcinfo")

    if not r.ok:
        _report_error("nfc info", r)
        raise SystemExit(1)

    table = Table(
        title=f"magspoof NFC @ {target}", header_style="cyan bold", show_header=False
    )
    table.add_column("field", style="cyan")
    table.add_column("value")
    table.add_row("PN7150 firmware", r.data.get("fw") or "[dim]—[/dim]")
    table.add_row("mode", r.data.get("mode") or "[dim]—[/dim]")
    table.add_row("SEL_RES", r.data.get("selres") or "[dim]—[/dim]")
    tag_seen = r.data.get("tag") == "yes"
    table.add_row("last tag seen", "yes" if tag_seen else "no")
    if tag_seen:
        table.add_row("UID", r.data.get("uid") or "[dim]—[/dim]")
    console.print(table)


# ── card (persistent multi-card store) ────────────────────────────────────────
#
# The `magcard` verb (firmware Phase 3, IMPLEMENTATION_PLAN_MagSpoof_Flash.md)
# manages a flash-resident database of named cards. Each list row arrives as one
# ':cardN <name>\t<track1>\t<track2>' line — indexed keys so DeviceLink.command()
# keeps every card (a plain ':card' repeat would collapse in its by-key dict),
# tab-delimited because neither the name nor either track charset contains a tab.
# One track fits a `magcard set` line but two full tracks would blow the REPL's
# input buffer, so `card add` composes an atomic add out of `add` + two `set`s.


def _iter_cards(data: Dict[str, str]) -> Iterator[Tuple[str, str, str]]:
    """Yield (name, track1, track2) for each ':cardN' line, in index order."""
    i = 0
    while f"card{i}" in data:
        parts = data[f"card{i}"].split("\t")
        parts += ["", "", ""]
        yield parts[0], parts[1], parts[2]
        i += 1


def _completion_target(port: Optional[str], device_id: Optional[int]) -> Optional[str]:
    """The port a <TAB> should query, or None. Unlike `resolve_port`, this never
    handshakes: `--port` wins, otherwise the board is numbered by USB id and we
    take the requested `-d` (or #1 by default — the same default `-d` itself
    uses). Only the one port we return is ever opened, so completing a card name
    can't reset a neighbouring board."""
    if port:
        return port
    dev = find_device(device_id)
    return dev.port if dev else None


def _stored_card_names(port: Optional[str], device_id: Optional[int]) -> list:
    """Names in the board's flash store, for shell completion — best-effort.
    The board itself is the only source of these names, so this opens the
    resolved port and asks `magcard list`. Old firmware without the store just
    answers `-ERR`, which reads here as an empty list."""
    target = _completion_target(port, device_id)
    if not target:
        return []
    link = DeviceLink(target)
    try:
        link.open()
        r = link.command("magcard list", read_timeout=_COMPLETION_READ_TIMEOUT)
    finally:
        link.close()
    if not r.ok:
        return []
    return [name for name, _t1, _t2 in _iter_cards(r.data) if name]


def complete_card_name(ctx, param, incomplete):
    """`shell_complete` for a NAME that must be an existing stored card
    (`card del/set/select/get`). Reads the already-typed `--port`/`-d` off the
    command's context so `card select -p /dev/ttyACM0 <TAB>` targets that board.
    Any failure — no board, wrong firmware, timeout — yields no suggestions
    rather than breaking the user's <TAB>."""
    params = ctx.params if ctx is not None else {}
    try:
        names = _stored_card_names(params.get("port"), params.get("device_id"))
    except Exception:
        return []
    wanted = incomplete.lower()
    return [CompletionItem(name) for name in names if name.lower().startswith(wanted)]


@magspoof.group("card", context_settings={"help_option_names": ["-h", "--help"]})
def card():
    """Manage the persistent multi-card store (requires flash-storage firmware).

    Cards live in flash and survive a reset or reflash. The active card is what
    `magspoof play`/`show` and the physical button use; `card select` switches
    it. Older firmware without the store answers `-ERR unknown command` — reflash
    with `bombercat flash magspoof`.
    """


@card.command("list", context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--json", "as_json", is_flag=True, help="Emit one JSON object per card.")
@device_options
@click.pass_context
def card_list_cmd(ctx, as_json, verbose, port, device_id):
    """List every stored card, marking the active one."""
    level = _verbosity(ctx, verbose)
    with _magspoof_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = link.command("magcard list")

    if not r.ok:
        _report_error("card list", r)
        raise SystemExit(1)

    active = r.data.get("active", "")
    cards = list(_iter_cards(r.data))
    if as_json:
        for name, t1, t2 in cards:
            print(
                json.dumps({"name": name, "t1": t1, "t2": t2, "active": name == active})
            )
        return

    if not cards:
        print_info("no cards stored")
        return

    table = Table(title=f"magspoof cards @ {target}", header_style="cyan bold")
    table.add_column("", width=1)  # active marker
    table.add_column("name", style="cyan")
    table.add_column("track 1")
    table.add_column("track 2")
    for name, t1, t2 in cards:
        marker = "[green]●[/green]" if name == active else ""
        table.add_row(marker, name, t1 or "[dim]—[/dim]", t2 or "[dim]—[/dim]")
    console.print(table)


@card.command("add", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("name")
@click.option("--t1", "track1", help="Track 1 data (starts with '%', ends with '?').")
@click.option("--t2", "track2", help="Track 2 data (starts with ';', ends with '?').")
@click.option(
    "--normalize-sc/--no-normalize-sc",
    "normalize_sc",
    default=False,
    help=(
        "Auto-normalize --t2's Service Code for magstripe fallback (no "
        "chip/PIN) before writing it. FOR AUTHORIZED TESTING ONLY."
    ),
)
@device_options
@click.pass_context
def card_add_cmd(ctx, name, track1, track2, normalize_sc, verbose, port, device_id):
    """Add a new card NAME with one or both tracks.

    Pass --t1 and/or --t2; at least one is required. Two-track financial cards
    give both; single-track membership/loyalty cards give just the one they
    carry. Each track is validated locally (ISO sentinels, length, charset)
    before anything reaches the device; the card is created and its track(s)
    written in one go, and a failed track write rolls the empty card back.
    --normalize-sc rewrites --t2's Service Code first (see `card
    normalize-sc`), so the card is stored already-normalized.
    """
    name_err = _validate_card_name(name)
    if name_err:
        print_error(name_err)
        raise SystemExit(1)
    if track1 is None and track2 is None:
        print_error("a card needs at least one track — pass --t1 and/or --t2")
        raise SystemExit(1)

    writes = []
    sc_normalized = False
    for tk, data in ((1, track1), (2, track2)):
        if data is None:
            continue
        if tk == 2 and normalize_sc:
            norm = normalize_track2(data)
            if norm is None:
                print_error(
                    "--normalize-sc: track 2 is not valid ISO 7813 — cannot normalize"
                )
                raise SystemExit(1)
            sc_normalized = norm != data
            data = norm
        data_err = _validate_track_data(tk, data)
        if data_err:
            print_error(data_err)
            raise SystemExit(1)
        writes.append((tk, data))

    level = _verbosity(ctx, verbose)
    with _magspoof_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = link.command(f"magcard add {name}")
        if not r.ok:
            _report_error("card add", r)
            raise SystemExit(1)
        # A full track won't share a REPL line with another, so fill them one per
        # command. Roll the empty card back on failure so a partial add leaves
        # no trace.
        for tk, data in writes:
            rt = link.command(f"magcard set {name} {tk} {data}")
            if not rt.ok:
                link.command(f"magcard del {name}")
                _report_error("card add", rt)
                raise SystemExit(1)

    kind = "1-track" if len(writes) == 1 else "2-track"
    msg = f"added {kind} card {name}"
    if normalize_sc and track2 is not None:
        msg += (
            " (service code normalized)"
            if sc_normalized
            else " (service code already normalized)"
        )
    print_success(msg)


@card.command("del", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("name", shell_complete=complete_card_name)
@device_options
@click.pass_context
def card_del_cmd(ctx, name, verbose, port, device_id):
    """Delete the card called NAME."""
    level = _verbosity(ctx, verbose)
    with _magspoof_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = link.command(f"magcard del {name}")

    if not r.ok:
        _report_error("card del", r)
        raise SystemExit(1)
    print_success(f"deleted card {name}")


@card.command("set", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("name", shell_complete=complete_card_name)
@click.option("--t1", "track1", help="New track 1 (starts with '%', ends with '?').")
@click.option("--t2", "track2", help="New track 2 (starts with ';', ends with '?').")
@click.option(
    "--nfc",
    "nfc_mode",
    type=click.Choice(["chip", "nochip"]),
    help=(
        "SEL_RES preference for this card (IMPLEMENTATION_PLAN_NFC_VISA_"
        "MAGSPOOF.md Phase 5.3): 'chip' advertises ISO-DEP/EMV support during "
        "'nfc visa'/'nfc read', 'nochip' forces MSD fallback. Consulted "
        "whenever this card is active; a card with no preference set falls "
        "back to the mode's own default (chip for reader, nochip for "
        "emulation)."
    ),
)
@click.option(
    "--normalize-sc/--no-normalize-sc",
    "normalize_sc",
    default=False,
    help=(
        "Auto-normalize --t2's Service Code for magstripe fallback (no "
        "chip/PIN) before writing it. FOR AUTHORIZED TESTING ONLY. To "
        "normalize a card's *already-stored* track 2 instead, use `card "
        "normalize-sc --apply`."
    ),
)
@device_options
@click.pass_context
def card_set_cmd(
    ctx, name, track1, track2, nfc_mode, normalize_sc, verbose, port, device_id
):
    """Update one or both tracks, and/or the SEL_RES preference, of the
    existing card NAME.

    Pass --t1, --t2 and/or --nfc; at least one is required. Track data is
    validated locally before it makes the serial round trip. --normalize-sc
    rewrites --t2's Service Code first (see `card normalize-sc`).
    """
    if track1 is None and track2 is None and nfc_mode is None:
        print_error("nothing to set — pass --t1, --t2 and/or --nfc")
        raise SystemExit(1)

    updates = []
    sc_normalized = False
    for tk, data in ((1, track1), (2, track2)):
        if data is None:
            continue
        if tk == 2 and normalize_sc:
            norm = normalize_track2(data)
            if norm is None:
                print_error(
                    "--normalize-sc: track 2 is not valid ISO 7813 — cannot normalize"
                )
                raise SystemExit(1)
            sc_normalized = norm != data
            data = norm
        err = _validate_track_data(tk, data)
        if err:
            print_error(err)
            raise SystemExit(1)
        updates.append((tk, data))

    level = _verbosity(ctx, verbose)
    with _magspoof_session(port, device_id, trace=make_tracer(level)) as (target, link):
        for tk, data in updates:
            r = link.command(f"magcard set {name} {tk} {data}")
            if not r.ok:
                _report_error("card set", r)
                raise SystemExit(1)
        if nfc_mode is not None:
            r = link.command(f"magcard set {name} nfc {nfc_mode}")
            if not r.ok:
                _report_error("card set", r)
                raise SystemExit(1)

    changed = [f"track {tk}" for tk, _ in updates]
    if nfc_mode is not None:
        changed.append(f"nfc {nfc_mode}")
    msg = f"updated {' and '.join(changed)} on {name}"
    if normalize_sc and track2 is not None:
        msg += (
            " (service code normalized)"
            if sc_normalized
            else " (service code already normalized)"
        )
    print_success(msg)


@card.command("select", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("name", shell_complete=complete_card_name)
@device_options
@click.pass_context
def card_select_cmd(ctx, name, verbose, port, device_id):
    """Make NAME the active card (persisted across resets)."""
    level = _verbosity(ctx, verbose)
    with _magspoof_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = link.command(f"magcard select {name}")

    if not r.ok:
        _report_error("card select", r)
        raise SystemExit(1)
    print_success(f"active card is now {r.data.get('active', name)}")


@card.command("get", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("name", required=False, shell_complete=complete_card_name)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help='Emit {"name": ..., "t1": ..., "t2": ..., "nfc": ..., "active": ...}.',
)
@device_options
@click.pass_context
def card_get_cmd(ctx, name, as_json, verbose, port, device_id):
    """Show a card's tracks (the active card when NAME is omitted)."""
    level = _verbosity(ctx, verbose)
    cmd = f"magcard get {name}" if name else "magcard get"
    with _magspoof_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = link.command(cmd)

    if not r.ok:
        _report_error("card get", r)
        raise SystemExit(1)

    card_name = r.data.get("name", "")
    t1 = r.data.get("t1", "")
    t2 = r.data.get("t2", "")
    # Firmware older than the Phase 5 CardEntry extension answers `get`
    # without ':nfc'.
    nfc_mode = r.data.get("nfc", "")
    is_active = r.data.get("active", "") == "1"
    if as_json:
        print(
            json.dumps(
                {
                    "name": card_name,
                    "t1": t1,
                    "t2": t2,
                    "nfc": nfc_mode,
                    "active": is_active,
                }
            )
        )
        return

    _print_field("name", card_name or "[dim]—[/dim]")
    _print_field("active", "yes" if is_active else "no")
    _print_field("track 1", t1 or "[dim]—[/dim]")
    _print_field("track 2", t2 or "[dim]—[/dim]")
    _print_field("nfc selres", nfc_mode or "[dim]—[/dim]")


@card.command("info", context_settings={"help_option_names": ["-h", "--help"]})
@device_options
@click.pass_context
def card_info_cmd(ctx, verbose, port, device_id):
    """Show store stats: card count, capacity, active card and button mode."""
    level = _verbosity(ctx, verbose)
    with _magspoof_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = link.command("magcard info")

    if not r.ok:
        _report_error("card info", r)
        raise SystemExit(1)

    count = r.data.get("count", "?")
    capacity = r.data.get("capacity", "?")
    btn = r.data.get("btn", "")
    table = Table(
        title=f"magspoof store @ {target}", header_style="cyan bold", show_header=False
    )
    table.add_column("field", style="cyan")
    table.add_column("value")
    table.add_row("cards", f"{count} / {capacity}")
    table.add_row("active", r.data.get("active") or "[dim]—[/dim]")
    table.add_row("button", _button_label(btn) if btn else "[dim]—[/dim]")
    console.print(table)


@card.command("normalize-sc", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("name", required=False, shell_complete=complete_card_name)
@click.option(
    "--apply", is_flag=True, help="Write the normalized Track 2 back to the card."
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help=(
        'Emit {"name", "service_code", "service_code_normalized", "track2", '
        '"track2_normalized", "is_ic_card", "requires_pin"}.'
    ),
)
@device_options
@click.pass_context
def card_normalize_sc_cmd(ctx, name, apply, as_json, verbose, port, device_id):
    """Normalize a stored card's Track 2 Service Code for magstripe fallback.

    Rewrites the Service Code so a terminal can swipe the card without
    demanding a chip or a PIN (the active card when NAME is omitted). Without
    --apply this only previews the change; pass --apply to write it back via
    `card set`. FOR AUTHORIZED TESTING ONLY — this deliberately weakens the
    card's stated security requirements.
    """
    level = _verbosity(ctx, verbose)
    cmd = f"magcard get {name}" if name else "magcard get"
    with _magspoof_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = link.command(cmd)
        if not r.ok:
            _report_error("card normalize-sc", r)
            raise SystemExit(1)

        card_name = r.data.get("name", "")
        t2 = r.data.get("t2", "")
        if not t2:
            print_error(f"card {card_name or name} has no track 2")
            raise SystemExit(1)

        parsed = parse_track2(t2)
        if parsed is None:
            print_error("stored track 2 is not valid ISO 7813 — cannot normalize")
            raise SystemExit(1)

        sc = parsed.service_code
        sc_norm = parsed.normalized_service_code()
        t2_norm = parsed.to_track2(sc_norm)

        if as_json:
            print(
                json.dumps(
                    {
                        "name": card_name,
                        "service_code": sc,
                        "service_code_normalized": sc_norm,
                        "track2": t2,
                        "track2_normalized": t2_norm,
                        "is_ic_card": parsed.is_ic_card,
                        "requires_pin": parsed.requires_pin,
                    }
                )
            )
            return

        sc_display = (
            f"{sc} → {sc_norm}" if sc != sc_norm else f"{sc} (already normalized)"
        )
        _print_field("name", card_name or "[dim]—[/dim]")
        _print_field("service code", sc_display)
        _print_field("chip required", "yes" if parsed.is_ic_card else "no")
        _print_field("PIN required", "yes" if parsed.requires_pin else "no")

        if not apply:
            if sc != sc_norm:
                print_info("use --apply to write the normalized track 2 to the card")
            return

        if sc == sc_norm:
            print_info(f"{card_name or name} already normalized — nothing to write")
            return

        err = _validate_track_data(2, t2_norm)
        if err:
            print_error(f"normalized track 2 failed local validation: {err}")
            raise SystemExit(1)

        rt = link.command(f"magcard set {card_name or name} 2 {t2_norm}")
        if not rt.ok:
            _report_error("card normalize-sc", rt)
            raise SystemExit(1)

    print_success(f"service code normalized on {card_name or name}: {sc} → {sc_norm}")
