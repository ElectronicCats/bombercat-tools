#!/usr/bin/env python3

# Electronic Cats
# `bombercat emvy …` — EMVy Controller swiss-army commands (EMVyBomberCat
# firmware): EMV read, APDU passthrough, tag/magstripe/NDEF/EMV-card emulation.
# Like magspoof, these commands actively drive the board — but unlike every
# other group, gating here is by *firmware identity*, not by an auto-flashable
# capability: EMVyBomberCat is built from source (arduino-cli), not a prebuilt
# release, so there is no image `bombercat flash` could install. We therefore
# verify `info.fw_name == "emvybombercat"` and, on a mismatch, refute with a
# build-and-flash hint instead of reaching for `ensure_firmware` (D2/§5, P-1:
# gate by id).
#
# Discovery (ping/info/identify) speaks the canonical +OK/-ERR REPL, so `emvy
# info` uses DeviceLink directly. The operative verbs (Fases 3-4: read/apdu/tag/
# mag/emu) speak EMVyBomberCat's historical dialect and will drive an EmvyLink
# (link.py) opened after this same identity gate — see
# docs/IMPLEMENTATION_PLAN_EMVyBomberCat_CLI.md §6 Fase 2.
# Distributed as-is; no warranty is given.

import json
import sys
from contextlib import contextmanager
from typing import Iterable, Iterator, Optional, Tuple

import click
from rich.table import Table

from ..core.bombercat import DeviceLink, Response, resolve_port
from ..utils.cli_options import device_options
from ..utils.detection_cli import (
    device_session,
    print_field as _print_field,
    verbosity as _verbosity,
)
from ..utils.output import (
    console,
    make_tracer,
    print_error,
    print_info,
    print_success,
    print_warning,
)
from .link import EmvyLink
from .parser import (
    EmvyError,
    parse_apdu_resp,
    parse_cardscan,
    parse_emu_event,
    parse_nfcinfo,
    parse_scan_json,
    parse_tag,
    raise_for_err,
)

# The `fw_name` an EMVyBomberCat board reports over `info` (must match its
# registry `id`, see core/firmwares.py). This is the whole gate: a board that
# answers the handshake but names itself something else is the wrong firmware.
EMVY_FW_NAME = "emvybombercat"

# The operative surface EMVyBomberCat serves, shown by `emvy info`. These are
# firmware capabilities (present whatever the CLI exposes yet), each landing as
# an `emvy` subcommand across Fases 3-4. Kept static rather than probed: the
# firmware offers no capability query, and hard-coding the honest list beats
# pretending to discover it (FW-0a would let `info` advertise `:ops …`, but the
# firmware does not do that today).
_OPERATIONS: Tuple[Tuple[str, str], ...] = (
    ("read", "EMV card read (SCAN → PAN/expiry/track2/AID)"),
    ("apdu", "APDU passthrough to a live card (WAIT/APDU/RELEASE)"),
    ("tag", "tag UID read (TAG)"),
    ("mag", "magstripe swipe emulation (MAG)"),
    ("emu", "NDEF tag / EMV-card emulation (EMU/EMUEMV)"),
    ("cardscan", "read an EMV card into device RAM for reemulation (CARDSCAN)"),
    ("nfcinfo", "PN7150 I2C diagnostic (NFCINFO)"),
    ("reboot", "reset the MCU; USB re-enumerates (REBOOT)"),
)


@click.group("emvy")
def emvy() -> None:
    """EMVy Controller swiss-army commands (requires the EMVyBomberCat firmware)."""


@contextmanager
def _emvy_session(
    port: Optional[str], device_id: Optional[int] = None, trace=None
) -> Iterator[Tuple[str, DeviceLink, Response]]:
    """Open a link verified to be running EMVyBomberCat, yield
    ``(target, link, info)``, and always close it.

    Reuses `device_session` (requires=None: resolve the port, open a DeviceLink,
    and confirm the handshake) and then adds the identity gate this group needs:
    `info` must report `fw_name == emvybombercat`. On a mismatch it refutes and
    exits non-zero, pointing at building/flashing the firmware from source — it
    never auto-flashes, because EMVyBomberCat has no prebuilt image (D2/§5).

    `resolve_port`/`DeviceLink` are referenced as this module's globals so tests
    can monkeypatch them (the `use_link` fixture), mirroring `_magspoof_session`.
    """
    with device_session(
        resolve_port,
        DeviceLink,
        "emvy",
        "EMVyBomberCat",
        port,
        device_id,
        trace,
    ) as (target, link):
        info = link.info()
        name = info.data.get("fw_name", "") if info.ok else ""
        if name != EMVY_FW_NAME:
            print_error(
                f"{target} is not running EMVyBomberCat "
                f"(it reports fw_name={name or '—'})."
            )
            print_info(
                "the `emvy` commands need the EMVyBomberCat firmware — build and "
                "flash it from bombercat-firmware/EMVyBomberCat with arduino-cli. "
                "It is not a prebuilt release, so `bombercat flash` cannot install "
                "it."
            )
            raise SystemExit(1)
        yield target, link, info


@contextmanager
def _emvy_operative_session(
    port: Optional[str], device_id: Optional[int] = None, trace=None
) -> Iterator[Tuple[str, EmvyLink]]:
    """Like `_emvy_session`, but hands back an `EmvyLink` instead of the
    `DeviceLink` used for the identity gate, and always closes it.

    Discovery (ping/info) speaks the canonical framing, so the gate opens a
    `DeviceLink` first; the operative verbs (read/apdu/tag/mag/emu) speak
    EMVyBomberCat's historical dialect, so once the gate passes we close that
    `DeviceLink` and reopen the same port as an `EmvyLink`. `EmvyLink` is a
    module global too, so tests can monkeypatch it like resolve_port/DeviceLink.
    """
    with _emvy_session(port, device_id, trace) as (target, link, _info):
        link.close()
        elink = EmvyLink(target, trace=trace).open()
        try:
            yield target, elink
        finally:
            elink.close()


@emvy.command("info")
@device_options
@click.pass_context
def info_cmd(ctx, verbose, port, device_id):
    """Report the EMVyBomberCat firmware and the operations it exposes.

    Confirms the board is running EMVyBomberCat (refusing otherwise), then
    prints its version, current state (idle/scanning/emulating/hw-error) and the
    operative surface the `emvy` subcommands drive.

    Exit code: 0 identified, 1 wrong firmware or link error.
    """
    level = _verbosity(ctx, verbose)
    with _emvy_session(port, device_id, trace=make_tracer(level)) as (
        target,
        _link,
        info,
    ):
        fw = info.data.get("fw", "")
        state = info.data.get("state", "")

    table = Table(
        title=f"EMVyBomberCat @ {target}", header_style="cyan bold", show_header=False
    )
    table.add_column("field", style="cyan")
    table.add_column("value")
    table.add_row("version", fw or "[dim]—[/dim]")
    table.add_row("state", state or "[dim]—[/dim]")
    console.print(table)

    console.print("")
    console.print("  [cyan bold]operations[/cyan bold]")
    for name, description in _OPERATIONS:
        console.print(f"  [green]{name:<6}[/green] [dim]{description}[/dim]")


# ── read ─────────────────────────────────────────────────────────────────────
#
# `SCAN <cents>` runs the firmware's full EMV flow: it streams `# …` progress
# logs, then the card data as JSON between bare `JSON_START`/`JSON_END`
# lines, or leaves a `# ERROR: …` log (e.g. `# ERROR: Timeout`) with no
# JSON_END at all on failure (§2.3). Field names are not yet confirmed by a
# captured fixture (Fase 0's field capture is still pending hardware — see
# the Progress Log), so the table below shows the common EMV fields under a
# few aliases and falls back to printing whatever else the firmware included.

_DEFAULT_SCAN_CENTS = 500

# (display label, candidate JSON keys in preference order)
_READ_FIELDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("PAN", ("pan",)),
    ("expiry", ("expiry", "exp", "expiration")),
    ("label", ("label", "application_label", "app_label")),
    ("AID", ("aid",)),
    ("track2", ("track2", "t2")),
    ("ARQC", ("arqc",)),
    ("ATC", ("atc",)),
    ("AIP", ("aip",)),
)


def _first(data: dict, keys: Tuple[str, ...]) -> Optional[str]:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _abort_on_scan_error(line: str) -> None:
    """`on_line` callback for the SCAN stream: the firmware never sends
    JSON_END on failure, only a `# ERROR: …` log — so waiting for the
    sentinel would just time out. Raise as soon as that log appears instead
    of waiting out the full --timeout."""
    stripped = line.strip()
    if stripped.startswith("# ERROR"):
        raise EmvyError(stripped.lstrip("# ").strip() or "scan failed")


@emvy.command("read")
@click.option(
    "--amount",
    "cents",
    default=_DEFAULT_SCAN_CENTS,
    show_default=True,
    type=int,
    help="Transaction amount in cents to present to the card during SCAN.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the parsed SCAN result as one JSON object.",
)
@click.option(
    "--raw",
    "as_raw",
    is_flag=True,
    help="Print every raw line as SCAN streams, instead of a parsed table (handy to capture a fixture).",
)
@click.option(
    "-t",
    "--timeout",
    default=30.0,
    show_default=True,
    help="Seconds to wait for the SCAN to complete.",
)
@device_options
@click.pass_context
def read_cmd(ctx, cents, as_json, as_raw, timeout, verbose, port, device_id):
    """Read an EMV card via a SCAN (PAN/expiry/AID/track2/...).

    Present the card once this starts. Exit code: 0 read, 1 no card / scan
    error / link error.
    """
    level = _verbosity(ctx, verbose)
    with _emvy_operative_session(port, device_id, trace=make_tracer(level)) as (
        target,
        elink,
    ):
        if not as_json and not as_raw:
            print_info(f"Waiting for a card on {target} (up to {timeout:g}s)...")
        on_line = console.print if as_raw else _abort_on_scan_error
        try:
            elink.send(f"SCAN {cents}")
            lines = elink.stream_until("JSON_END", on_line=on_line, timeout=timeout)
        except EmvyError as e:
            print_error(f"read failed: {e}")
            raise SystemExit(1)

    if as_raw:
        return

    try:
        data = parse_scan_json(lines)
    except EmvyError as e:
        print_error(f"read failed: {e}")
        raise SystemExit(1)

    if as_json:
        print(json.dumps(data))
        return

    console.print(f"[cyan bold]EMV card[/cyan bold] [dim]@ {target}[/dim]")
    console.print("")
    shown = set()
    for label, keys in _READ_FIELDS:
        value = _first(data, keys)
        if value is None:
            continue
        _print_field(label, value)
        shown.update(keys)
    extra = {k: v for k, v in data.items() if k not in shown}
    if extra:
        console.print("")
        for key, value in extra.items():
            _print_field(key, str(value))


# ── apdu ─────────────────────────────────────────────────────────────────────
#
# Passthrough is a stateful (edge-triggered) sequence, not a single verb: WAIT
# arms discovery and engages the card, one or more APDU: exchanges tunnel to
# it, and RELEASE lets it go (D4). This command owns that whole sequence so no
# subcommand ever exposes a bare APDU: — RELEASE always fires, even on
# Ctrl-C or a mid-run error, so a passthrough session can't leave the card
# engaged for the next command.

_DEFAULT_WAIT_MS = 30000
# Margin over the firmware's own WAIT budget so the client doesn't give up
# before a WAIT that's legitimately still waiting for a tap.
_WAIT_MARGIN_S = 5.0


def _validate_apdu_hex(value: str) -> Optional[str]:
    v = value.strip()
    if not v:
        return "APDU cannot be empty"
    try:
        bytes.fromhex(v)
    except ValueError:
        return f"not valid hex: {value!r}"
    return None


def _apdu_lines(apdu: Optional[str]) -> Iterable[str]:
    """One-shot ARGUMENT, or one hex APDU per stdin line (--stdin)."""
    if apdu is not None:
        yield apdu
        return
    for raw in sys.stdin:
        line = raw.strip()
        if line:
            yield line


@emvy.command("apdu")
@click.argument("apdu", required=False)
@click.option(
    "--stdin",
    "read_stdin",
    is_flag=True,
    help="Read one hex APDU per line from stdin, all inside one WAIT/RELEASE session.",
)
@click.option(
    "--json", "as_json", is_flag=True, help='Emit {"resp": "<hex>"} per exchange.'
)
@click.option(
    "-w",
    "--wait",
    "wait_ms",
    default=_DEFAULT_WAIT_MS,
    show_default=True,
    metavar="MS",
    help="Milliseconds WAIT arms discovery for.",
)
@device_options
@click.pass_context
def apdu_cmd(ctx, apdu, read_stdin, as_json, wait_ms, verbose, port, device_id):
    """Tunnel one or more APDUs to a live card (WAIT -> APDU: -> RELEASE).

    One-shot: `emvy apdu <hex>` sends a single command APDU and prints its
    response. `--stdin` instead reads one hex APDU per line and sends each
    inside the same WAIT/RELEASE session, useful for scripting an EMV
    exchange. Exactly one of APDU / --stdin is required. Present the card
    once this starts; RELEASE always fires on exit, including Ctrl-C.

    Exit code: 0 every APDU answered, 1 no card / a bad APDU / link error.
    """
    if bool(apdu) == bool(read_stdin):
        print_error("pass exactly one of an APDU argument or --stdin")
        raise SystemExit(1)
    if apdu is not None:
        err = _validate_apdu_hex(apdu)
        if err:
            print_error(err)
            raise SystemExit(1)

    level = _verbosity(ctx, verbose)
    with _emvy_operative_session(port, device_id, trace=make_tracer(level)) as (
        target,
        elink,
    ):
        if not as_json:
            print_info(
                f"Waiting for a card on {target} (up to {wait_ms / 1000.0:g}s)..."
            )
        try:
            lines = elink.exchange(
                f"WAIT {wait_ms}", timeout=wait_ms / 1000.0 + _WAIT_MARGIN_S
            )
            raise_for_err(lines)
        except EmvyError as e:
            print_error(f"apdu failed: {e}")
            raise SystemExit(1)

        exit_code = 0
        try:
            for hex_apdu in _apdu_lines(apdu):
                err = _validate_apdu_hex(hex_apdu)
                if err:
                    print_error(err)
                    exit_code = 1
                    continue
                try:
                    resp = parse_apdu_resp(elink.exchange(f"APDU:{hex_apdu.strip()}"))
                except EmvyError as e:
                    print_error(f"apdu {hex_apdu} failed: {e}")
                    exit_code = 1
                    continue
                if as_json:
                    print(json.dumps({"resp": resp.hex().upper()}))
                else:
                    console.print(resp.hex().upper())
        except KeyboardInterrupt:
            print_warning("aborted")
            exit_code = 1
        finally:
            try:
                elink.exchange("RELEASE")
            except EmvyError:
                pass

    if exit_code:
        raise SystemExit(exit_code)


# ── tag ──────────────────────────────────────────────────────────────────────

# Margin over the firmware's own tag-wait budget (emvyTagsRead: 8s) so the
# client doesn't give up before the firmware's own timeout does.
_TAG_TIMEOUT_S = 12.0


@emvy.command("tag")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help='Emit {"proto": ..., "tech": ..., "uid": ...}.',
)
@device_options
@click.pass_context
def tag_cmd(ctx, as_json, verbose, port, device_id):
    """Read any nearby tag's protocol/tech/UID (TAG).

    Present the tag once this starts (waits up to 8s). Exit code: 0 read, 1
    no tag / link error.
    """
    level = _verbosity(ctx, verbose)
    with _emvy_operative_session(port, device_id, trace=make_tracer(level)) as (
        target,
        elink,
    ):
        if not as_json:
            print_info(f"Waiting for a tag on {target} (up to 8s)...")
        try:
            lines = elink.exchange(
                "TAG", terminators=("TAG:", "ERR:"), timeout=_TAG_TIMEOUT_S
            )
            data = parse_tag(lines[-1])
        except EmvyError as e:
            print_error(f"tag failed: {e}")
            raise SystemExit(1)

    if as_json:
        print(json.dumps(data))
        return

    console.print(f"[cyan bold]tag[/cyan bold] [dim]@ {target}[/dim]")
    console.print("")
    _print_field("protocol", data.get("proto", ""))
    if data.get("tech"):
        _print_field("tech", data["tech"])
    _print_field("UID", data.get("uid") or "[dim]—[/dim]")


# ── mag ──────────────────────────────────────────────────────────────────────
#
# `emvyMagPlay` (modes_mag.ino) tolerates tracks with or without their ISO
# sentinels (%..?/;..?) and either track being empty (single-track swipe), so
# validation here stays local to what the firmware actually enforces (127-char
# buffer, and the `|` field separator can't appear inside a track) rather than
# reusing magspoof's stricter `_validate_track_data` (which demands the ISO
# sentinels magspoof's own card store requires).


def _validate_mag_track(track: int, data: str) -> Optional[str]:
    if "\n" in data or "\r" in data:
        return f"track {track} cannot contain a newline"
    if "|" in data:
        return f"track {track} cannot contain '|' (the MAG: field separator)"
    if len(data) > 127:
        return f"track {track} too long ({len(data)} chars, max 127)"
    return None


@emvy.command("mag")
@click.argument("track1")
@click.argument("track2")
@device_options
@click.pass_context
def mag_cmd(ctx, track1, track2, verbose, port, device_id):
    """Emulate a magstripe swipe of TRACK1/TRACK2 (MAG).

    Either track may be "" for a single-track swipe. Tracks are sent as-is —
    the firmware accepts them with or without their ISO sentinels (%...?/
    ;...?). Exit code: 0 played, 1 invalid track / link error.
    """
    if not track1 and not track2:
        print_error("pass at least one non-empty track (TRACK1 and/or TRACK2)")
        raise SystemExit(1)
    for n, data in ((1, track1), (2, track2)):
        if not data:
            continue
        err = _validate_mag_track(n, data)
        if err:
            print_error(err)
            raise SystemExit(1)

    level = _verbosity(ctx, verbose)
    with _emvy_operative_session(port, device_id, trace=make_tracer(level)) as (
        _target,
        elink,
    ):
        try:
            raise_for_err(elink.exchange(f"MAG:{track1}|{track2}"))
        except EmvyError as e:
            print_error(f"mag failed: {e}")
            raise SystemExit(1)

    print_success("swipe emulated")


# ── cardscan ─────────────────────────────────────────────────────────────────

# Margin over the firmware's own budget (pollCard: 20s, plus the EMV flow that
# follows a detected card).
_CARDSCAN_TIMEOUT_S = 25.0


@emvy.command("cardscan")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help='Emit {"aid": ..., "pan": ..., "exp": ..., "t2": ...}.',
)
@device_options
@click.pass_context
def cardscan_cmd(ctx, as_json, verbose, port, device_id):
    """Read an EMV card into the device's RAM for reemulation (CARDSCAN).

    Feeds `emvy emu card --from-ram`. Present the card once this starts (up
    to ~20s). Exit code: 0 scanned, 1 no card / scan failure / link error.
    """
    level = _verbosity(ctx, verbose)
    with _emvy_operative_session(port, device_id, trace=make_tracer(level)) as (
        target,
        elink,
    ):
        if not as_json:
            print_info(f"Waiting for a card on {target} (up to ~20s)...")
        try:
            lines = elink.exchange(
                "CARDSCAN",
                terminators=("EMU:SCANNED", "ERR:"),
                timeout=_CARDSCAN_TIMEOUT_S,
            )
            data = parse_cardscan(lines[-1])
        except EmvyError as e:
            print_error(f"cardscan failed: {e}")
            raise SystemExit(1)

    if as_json:
        print(json.dumps(data))
        return

    console.print(
        f"[cyan bold]scanned card[/cyan bold] [dim]@ {target} "
        f"(stored in device RAM)[/dim]"
    )
    console.print("")
    for label, key in (
        ("AID", "aid"),
        ("PAN", "pan"),
        ("expiry", "exp"),
        ("track2", "t2"),
    ):
        _print_field(label, data.get(key) or "[dim]—[/dim]")
    print_info("use `emvy emu card --from-ram` to reemulate it")


# ── emu (streaming: ndef / card) ─────────────────────────────────────────────
#
# EMU:<hex>/EMUEMV[...] are asynchronous (D5): the firmware starts emulating
# and returns immediately, then loop()'s emuPump() streams progress lines
# (EMU:RX/EMU:TX/EMU:MSG-SENT) until EMU:DONE, STOP, REBOOT, or the firmware's
# own EMU_MAX_MS (180s) auto-stop. `_emu_stream` sends the trigger line and
# renders that stream live, sending STOP on Ctrl-C so the board doesn't keep
# emulating after the CLI exits (mirrors `stream_until`'s own contract).


@emvy.group("emu")
def emu():
    """NDEF tag / EMV-card emulation (EMU/EMUEMV)."""


def _validate_optional_hex(value: str) -> Optional[str]:
    """Hex validation for a field the firmware treats as "" = use default
    (emu ndef's message, emu card's --aid/--pan/--exp/--track2) — unlike
    `_validate_apdu_hex`, empty is valid here."""
    v = value.strip()
    if not v:
        return None
    try:
        bytes.fromhex(v)
    except ValueError:
        return f"not valid hex: {value!r}"
    return None


def _render_emu_line(line: str, as_raw: bool) -> None:
    if as_raw:
        console.print(line)
        return
    event = parse_emu_event(line)
    if event is None:
        console.print(f"[dim]{line}[/dim]")
        return
    kind = event["kind"]
    if "fields" in event:
        extra = " " + " ".join(f"{k}={v}" for k, v in event["fields"].items())
    elif "hex" in event:
        extra = f" {event['hex']}"
    elif event.get("raw"):
        extra = f" {event['raw']}"
    else:
        extra = ""
    console.print(f"[cyan]EMU:{kind}[/cyan]{extra}")


def _emu_stream(elink, trigger: str, *, timeout: Optional[float], as_raw: bool) -> None:
    """Send `trigger` (an `EMU:.../EMUEMV...` line) and render every `EMU:*`
    event live until `EMU:DONE`, `--timeout`, or Ctrl-C (which sends `STOP`
    first, per `EmvyLink.stream_until`, then returns cleanly instead of
    propagating — Ctrl-C ending a long-running emulation is the expected,
    not exceptional, way to stop it)."""
    elink.send(trigger)
    try:
        elink.stream_until(
            "EMU:DONE",
            on_line=lambda line: _render_emu_line(line, as_raw),
            stop_cmd="STOP",
            timeout=timeout,
        )
    except KeyboardInterrupt:
        print_warning("stopped")


@emu.command("ndef")
@click.argument("hex_ndef", metavar="HEX")
@click.option(
    "--raw", "as_raw", is_flag=True, help="Print every raw EMU:* line, unparsed."
)
@click.option(
    "-t",
    "--timeout",
    "timeout",
    type=float,
    default=None,
    help="Seconds to bound the whole emulation stream (default: run until "
    "EMU:DONE, Ctrl-C, or the firmware's own 180s auto-stop).",
)
@device_options
@click.pass_context
def emu_ndef_cmd(ctx, hex_ndef, as_raw, timeout, verbose, port, device_id):
    """Emulate an NFC Forum Type 4 tag serving HEX as its NDEF message (EMU:).

    HEX may be "" for an empty tag. Streams every reader APDU live
    (SELECT-APP/SELECT-CC/SELECT-NDEF/READ/WRITE) until EMU:DONE, --timeout,
    or Ctrl-C. Present the tag to a reader once this starts.
    """
    err = _validate_optional_hex(hex_ndef)
    if err:
        print_error(err)
        raise SystemExit(1)

    level = _verbosity(ctx, verbose)
    with _emvy_operative_session(port, device_id, trace=make_tracer(level)) as (
        target,
        elink,
    ):
        print_info(f"emulating an NDEF tag on {target} — Ctrl-C to stop")
        try:
            _emu_stream(elink, f"EMU:{hex_ndef}", timeout=timeout, as_raw=as_raw)
        except EmvyError as e:
            print_error(f"emu ndef failed: {e}")
            raise SystemExit(1)


@emu.command("card")
@click.option(
    "--from-ram",
    "from_ram",
    is_flag=True,
    help="Reemulate the card most recently stored in device RAM by `emvy cardscan`.",
)
@click.option("--aid", default="", help="AID to announce in the PPSE (hex).")
@click.option("--pan", default="", help="PAN to serve in the record (hex BCD).")
@click.option("--exp", default="", help="Expiry to serve in the record (hex, YYMMDD).")
@click.option("--track2", default="", help="Track 2 to serve in the record (hex).")
@click.option(
    "--raw", "as_raw", is_flag=True, help="Print every raw EMU:* line, unparsed."
)
@click.option(
    "-t",
    "--timeout",
    "timeout",
    type=float,
    default=None,
    help="Seconds to bound the whole emulation stream (default: run until "
    "EMU:DONE, Ctrl-C, or the firmware's own 180s auto-stop).",
)
@device_options
@click.pass_context
def emu_card_cmd(
    ctx, from_ram, aid, pan, exp, track2, as_raw, timeout, verbose, port, device_id
):
    """Emulate an EMV card for a payment terminal (EMUEMV).

    With no options, serves a canned test Visa. --from-ram reemulates the
    card `emvy cardscan` last read; --aid/--pan/--exp/--track2 (all hex,
    empty = firmware default) inject a specific captured card instead — the
    two are mutually exclusive. Streams every terminal APDU live (decoded:
    SELECT-PPSE/SELECT-AID/GPO/READ-RECORD/GENERATE-AC) until EMU:DONE,
    --timeout, or Ctrl-C. This is not a working card — it never returns a
    valid cryptogram — the point is to see what a terminal asks for.
    """
    custom_fields = any((aid, pan, exp, track2))
    if from_ram and custom_fields:
        print_error("--from-ram and --aid/--pan/--exp/--track2 are mutually exclusive")
        raise SystemExit(1)
    for label, value in (
        ("--aid", aid),
        ("--pan", pan),
        ("--exp", exp),
        ("--track2", track2),
    ):
        err = _validate_optional_hex(value)
        if err:
            print_error(f"{label}: {err}")
            raise SystemExit(1)

    if from_ram:
        trigger = "EMUEMV:RAM"
    elif custom_fields:
        trigger = f"EMUEMV:{aid}|{pan}|{exp}|{track2}"
    else:
        trigger = "EMUEMV"

    level = _verbosity(ctx, verbose)
    with _emvy_operative_session(port, device_id, trace=make_tracer(level)) as (
        target,
        elink,
    ):
        print_info(f"emulating an EMV card on {target} — Ctrl-C to stop")
        try:
            _emu_stream(elink, trigger, timeout=timeout, as_raw=as_raw)
        except EmvyError as e:
            print_error(f"emu card failed: {e}")
            raise SystemExit(1)


# ── nfcinfo ──────────────────────────────────────────────────────────────────


@emvy.command("nfcinfo")
@device_options
@click.pass_context
def nfcinfo_cmd(ctx, verbose, port, device_id):
    """Diagnose the PN7150 over I2C and re-arm discovery (NFCINFO).

    A live chip answers with its firmware version — if that's what you get,
    the I2C link is fine and a "no card" symptom is an RF problem (card,
    antenna, placement), not a wiring one.
    """
    level = _verbosity(ctx, verbose)
    with _emvy_operative_session(port, device_id, trace=make_tracer(level)) as (
        target,
        elink,
    ):
        try:
            lines = elink.exchange("NFCINFO", terminators=("NFCINFO:",))
            data = parse_nfcinfo(lines[-1])
        except EmvyError as e:
            print_error(f"nfcinfo failed: {e}")
            raise SystemExit(1)

    if "fwver" in data:
        print_success(f"PN7150 alive @ {target} — firmware version {data['fwver']}")
    else:
        console.print(data.get("raw", ""))


# ── reboot ───────────────────────────────────────────────────────────────────


@emvy.command("reboot")
@device_options
@click.pass_context
def reboot_cmd(ctx, verbose, port, device_id):
    """Reset the board's MCU (REBOOT).

    The USB CDC re-enumerates on reset (D6): this link cannot be reused and
    a subsequent `emvy` command may need a different --port. No reply is
    expected — the board resets before it could send one.
    """
    level = _verbosity(ctx, verbose)
    with _emvy_operative_session(port, device_id, trace=make_tracer(level)) as (
        target,
        elink,
    ):
        elink.send("REBOOT")

    print_warning(
        f"{target} is rebooting — it will re-enumerate over USB; reconnect "
        "before the next emvy command."
    )
