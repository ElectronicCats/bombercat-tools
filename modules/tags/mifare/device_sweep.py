#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

"""On-device `mifare check` sweep — the device runs the dictionary sweep itself.

Plan de Implementación Firmware MIFARE, Fase 4 (tarea 4.1). The MifareClassic
firmware can sweep a card on its own: ONE `mifare check` command returns the
consolidated result — a `:mifare_check_sN <keyA|-> <keyB|->` line per sector
plus a `:mifare_check_stat attempts=… naive=… recovered=r/total` line — instead
of one `mifare auth` USB round-trip per candidate key.

This module owns that wire format (documented in the firmware's
`docs/baseline_fase0_mifare/serial_log_format.md`) and the single round-trip
that triggers it. `check.py` owns the *policy* (when the device sweep is used
instead of the host-driven one) and the rendering.

What moves and what doesn't: the number of authentications against the card is
unchanged (each attempt is still a real Crypto-1 auth over the I²C link — the
PN7150's ceiling is untouched); what disappears is the USB-serial round-trip
per candidate key and the host-side parse of every result.

The device sweep can only use the firmware's own candidate universe: its
compact embedded dictionary (32 keys) plus the persistent key-store
(`mifare keys`, Fase 3), or an explicit key list passed with `--keys` (the
firmware accepts at most `MIFARE_CHECK_MAX_KEYS` = 64). It knows nothing about
the host's named families (`--dict`) or UID-derived schemes (`--uid-derived`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ...core.bombercat import DeviceError, link_lost
from ...utils.output import print_info, print_warning
from .session import _wait_for_mifare_card

# The firmware's own caps (MifareClassic.ino: MIFARE_CHECK_MAX_KEYS and
# MIFARE_CHECK_MAX_SECTORS). Keep in sync — the host must never build a
# command the device would reject.
DEVICE_SWEEP_MAX_KEYS = 64
DEVICE_SWEEP_MAX_SECTORS = 32

# A device sweep is ONE round-trip, but it may run for minutes on a card whose
# sectors nothing opens (the device tries every candidate on every sector), so
# the read deadline is far longer than the per-command default.
DEVICE_SWEEP_TIMEOUT = 300.0

# How many times the sweep may re-open the port and try again when the board
# disappears mid-command. A reboot re-enumerates the CDC and kills the open
# file descriptor; the sweep itself is idempotent, so one retry recovers the
# common case (the board rebooting under the host) without looping forever.
DEVICE_SWEEP_RECONNECTS = 1

_SECTOR_KEY_RE = re.compile(r"^mifare_check_s(\d+)$")
_HEX_KEY_RE = re.compile(r"^[0-9A-F]{12}$")

# The on-device dump (Fase 4, tarea 4.4): one line per sector with the 4
# blocks as 128 hex chars (or "-" for a hole), the key type that opened it,
# and - for a sector that could not be read - the reason.
_DUMP_SECTOR_RE = re.compile(r"^mifare_dump_s(\d+)$")
_HEX_SECTOR_RE = re.compile(r"^[0-9A-F]{128}$")

# A key that wasn't recovered is reported as "-" (a hole), never as zeros or
# blanks — the honest-gap rule of the wire format.
_HOLE = "-"


class DeviceSweepError(RuntimeError):
    """The device refused or failed the on-device sweep."""


class DeviceSweepUnsupported(DeviceSweepError):
    """The flashed firmware has no `mifare check` command (an older build)."""


@dataclass
class DeviceSweepResult:
    """What the device reported for the sweep it just ran.

    `keys` maps a sector number to its `(key A, key B)` pair; a key that was
    not recovered is `None` (a declared hole). `attempts`/`naive` are the
    device's own counters — `attempts` is the number of real authentications
    it issued, `naive` what a no-reuse sweep of the same universe would cost.

    `trailer_reads` is how many trailer reads the device issued to recover a
    Key B the dictionary did not find (the on-device twin of the host's
    trailer-read pass). `None` means the flashed firmware does not report it
    — an older build — and the host must then run its own pass.
    """

    keys: Dict[int, Tuple[Optional[str], Optional[str]]] = field(default_factory=dict)
    attempts: int = 0
    naive: int = 0
    recovered: int = 0
    total: int = 0
    trailer_reads: Optional[int] = None
    # Where the BOARD spent the wall-clock, in milliseconds (Fase 4, Sesión
    # 11): `auth_ms` inside the Crypto-1 authentication, `reselect_ms` inside
    # the re-select (a full reader re-arm + re-detection) and `trailer_ms` in
    # the trailer-read pass. Only a firmware that reports them fills these in
    # (None otherwise) — they are what tells the device sweep's cost apart
    # from the host-driven one, which issues the same number of
    # authentications.
    auth_ms: Optional[int] = None
    reselect_ms: Optional[int] = None
    trailer_ms: Optional[int] = None
    # True when the device answered from its own cached sweep (the
    # on-detection auto-probe already swept this card with the same
    # dictionary, so the device did NOT sweep it again — Fase 4, Sesión 11).
    # None when the flashed firmware does not report it.
    cached: Optional[bool] = None
    # True when the device's sweep was INTERRUPTED before it covered every
    # sector (the card left the field, or the re-selects gave up). The
    # per-sector keys are then a PARTIAL result, and the caller has to say so
    # instead of presenting them as the whole card.
    aborted: Optional[bool] = None

    def recovered_count(self) -> int:
        """How many (sector, key type) slots actually carry a key — the same
        unit the device's own `recovered=r/total` counter uses."""
        return sum(
            (1 if key_a else 0) + (1 if key_b else 0)
            for key_a, key_b in self.keys.values()
        )

    def did_trailer_read(self) -> bool:
        """True when the device ran the trailer-read pass itself, so the host
        must NOT repeat it over USB."""
        if self.aborted:
            # The device's sweep was interrupted, so the trailer pass never
            # ran: the host still has to do it for the sectors that did open
            # (and `trailer=0` on an interrupted sweep means "not reached",
            # not "nothing to recover").
            return False
        return self.trailer_reads is not None


def _parse_key(token: str) -> Optional[str]:
    """One key token → the 12-hex key, or None for a hole ("-") or anything
    that isn't a well-formed key (defensive: a malformed token is a hole, not
    a guess)."""
    token = token.strip().upper()
    if not token or token == _HOLE or not _HEX_KEY_RE.match(token):
        return None
    return token


def parse_device_sweep(data: Dict[str, str]) -> DeviceSweepResult:
    """Parse the device's consolidated reply into a `DeviceSweepResult`.

    `data` is the `:key value` dict the link collected: one
    `mifare_check_sN` entry per sector plus the `mifare_check_stat` line.
    """
    result = DeviceSweepResult()
    for key, value in data.items():
        match = _SECTOR_KEY_RE.match(key)
        if not match:
            continue
        parts = (value or "").split()
        key_a = _parse_key(parts[0]) if len(parts) > 0 else None
        key_b = _parse_key(parts[1]) if len(parts) > 1 else None
        result.keys[int(match.group(1))] = (key_a, key_b)

    stat = data.get("mifare_check_stat", "") or ""
    for token in stat.split():
        name, _, raw = token.partition("=")
        if name == "attempts" and raw.isdigit():
            result.attempts = int(raw)
        elif name == "naive" and raw.isdigit():
            result.naive = int(raw)
        elif name == "trailer" and raw.isdigit():
            # Present only in firmware that runs the trailer-read pass itself
            # (Fase 4, tarea 4.4). Its presence tells the host the device
            # already recovered every Key B it could read back.
            result.trailer_reads = int(raw)
        elif name in ("auth_ms", "reselect_ms", "trailer_ms") and raw.isdigit():
            setattr(result, name, int(raw))
        elif name == "cached" and raw.isdigit():
            # The device reused its own sweep instead of sweeping again.
            result.cached = raw != "0"
        elif name == "aborted" and raw.isdigit():
            # The device's sweep was interrupted before it covered every
            # sector: the keys it reports are a partial result.
            result.aborted = raw != "0"
        elif name == "recovered" and "/" in raw:
            got, _, total = raw.partition("/")
            if got.isdigit():
                result.recovered = int(got)
            if total.isdigit():
                result.total = int(total)
    return result


def build_device_command(
    sectors: int, keys: Optional[List[str]] = None, fresh: bool = False
) -> str:
    """The one REPL line that triggers the on-device sweep.

    `keys`, when given, REPLACES the device's dictionary for this run (the
    same semantics as the host's `--keys`); at most
    `DEVICE_SWEEP_MAX_KEYS` of them, comma-separated, 12 hex chars each.

    `fresh` sweeps the card again even when the device still holds the
    results of an earlier sweep — the way to get a new answer when that
    earlier sweep was interrupted (the device answers from its cache, which
    would repeat the interrupted result).
    """
    line = f"mifare check --sectors {int(sectors)}"
    if keys:
        line += " --keys " + ",".join(k.strip().upper() for k in keys)
    if fresh:
        line += " --fresh"
    return line


def _classify_error(message: str) -> DeviceSweepError:
    """Map a firmware error message to the right exception.

    "unknown mifare subcommand" is how the firmware rejects a verb it doesn't
    know — i.e. the flashed build predates the on-device sweep. Anything else
    is a real failure of the sweep (card removed mid-sweep, bad usage…).
    """
    low = (message or "").lower()
    if "unknown mifare subcommand" in low or "unknown command" in low:
        return DeviceSweepUnsupported(
            message or "the flashed firmware has no `mifare check` command"
        )
    return DeviceSweepError(message or "the device sweep failed")


def _board_state(link) -> str:
    """The firmware's own state, or "" when it doesn't report one.

    The MifareClassic firmware reports `scanning` (no card), `tag-detected`
    (a session is open), `sweeping` (running a dictionary sweep) and
    `awaiting-removal` (waiting for the card to leave the field). The last one
    matters for the host's advice: while the board waits for a removal, no
    amount of tapping helps — the card has to go away for a moment.
    """
    try:
        response = link.info()
    except DeviceError:
        return ""
    if not response.ok:
        return ""
    return (response.data.get("state") or "").strip()


def _reconnect(link) -> bool:
    """Re-open the port after the board vanished. True when it came back."""
    reopen = getattr(link, "reopen", None)
    if reopen is None:
        return False
    try:
        return bool(reopen())
    except (DeviceError, OSError):
        return False


def _request_sweep(link, line: str, timeout: float, tap_timeout: float, quiet: bool):
    """Send the sweep command, waiting for a card tap if the board has none.

    Returns the device's response. Raises `DeviceSweepError` when no card was
    tapped, and `DeviceError` (from the core link) when the board disappears.
    """
    response = link.command(line, read_timeout=timeout)
    if response.ok or "no card selected" not in (response.message or "").lower():
        return response

    if not quiet:
        if _board_state(link) == "awaiting-removal":
            print_info(
                "the board is waiting for the card to leave the field — lift it "
                "for a second, then tap it again"
            )
        else:
            print_info(
                f"no card selected — tap a Mifare Classic card (up to "
                f"{tap_timeout:g}s)..."
            )
    # The firmware opens the session only after its own auto-probe sweep, so
    # the wait has to outlast that sweep — `_wait_for_mifare_card` extends
    # itself once it sees the sweep's per-sector lines.
    if not _wait_for_mifare_card(link, tap_timeout, sweep_timeout=timeout):
        raise DeviceSweepError("no card was tapped")
    return link.command(line, read_timeout=timeout)


def run_device_sweep(
    link,
    sectors: int,
    keys: Optional[List[str]] = None,
    timeout: float = DEVICE_SWEEP_TIMEOUT,
    tap_timeout: float = 15.0,
    reconnects: int = DEVICE_SWEEP_RECONNECTS,
    quiet: bool = False,
    fresh: bool = False,
) -> DeviceSweepResult:
    """Run the sweep on the device and return its consolidated result.

    ONE round-trip: the firmware sweeps every sector x {A,B} itself and
    answers with the per-sector lines plus its counters. If no card is
    selected yet, ask the user to tap one and retry once (the same
    choreography the host-driven sweep uses).

    A board that disappears mid-command (it rebooted — a reboot re-enumerates
    the CDC and kills the open port) is recovered: the port is re-opened and
    the sweep is retried, because the sweep is idempotent and the firmware
    re-detects the card by itself after a reboot.

    `quiet` keeps the progress messages off stdout — the `--json` output must
    stay machine-readable.

    Raises `DeviceSweepUnsupported` when the flashed firmware has no
    `mifare check` command, and `DeviceSweepError` for any other refusal.
    """
    line = build_device_command(sectors, keys, fresh)
    attempts_left = reconnects + 1

    while True:
        try:
            response = _request_sweep(link, line, timeout, tap_timeout, quiet)
        except DeviceError as e:
            if attempts_left <= 0 or not link_lost(str(e)):
                raise DeviceSweepError(str(e)) from e
            attempts_left -= 1
            if not quiet:
                print_warning(
                    "the board disconnected during the sweep (it rebooted?) — "
                    "reconnecting and retrying..."
                )
            if not _reconnect(link):
                raise DeviceSweepError(
                    "the board disconnected during the sweep and did not come "
                    "back — re-plug it and run the command again"
                ) from e
            continue
        if not response.ok:
            raise _classify_error((response.message or "").strip())
        break

    result = parse_device_sweep(response.data)
    if not result.keys and not result.total:
        # The device said "+OK" but sent no sweep result at all: it either
        # doesn't know the command (and answered something else) or its
        # answer was lost. Either way there is nothing to trust — report it
        # as unsupported so the caller can fall back instead of reporting an
        # empty card as "every sector unknown".
        raise DeviceSweepUnsupported("the device answered without a sweep result")
    return result


# ── the on-device dump (Fase 4, tarea 4.4) ────────────────────────────────────
#
# `mifare dump` reads the sectors the sweep cracked, with the keys the sweep
# found — ONE round-trip for the whole card instead of one per sector. The
# firmware substitutes the recovered keys into the trailer (a card never reads
# Key A back, and often not Key B either), exactly as the host's own dump
# does, so both dumps carry the same bytes.

# The firmware's wording when it has no sweep results to dump with.
_NO_SWEEP_MARKER = "no sweep results"


class DeviceDumpError(RuntimeError):
    """The device refused or failed the on-device dump."""


class DeviceDumpUnsupported(DeviceDumpError):
    """The flashed firmware has no `mifare dump` command (an older build)."""


class DeviceDumpNoSweep(DeviceDumpError):
    """The device has no sweep results yet — it never swept this card.

    Not a failure: the host can sweep first and dump afterwards, or fall back
    to its own per-sector dump.
    """


@dataclass
class DeviceDumpSector:
    """One dumped sector: `data_hex` is the 4 blocks (128 hex chars) or None
    for a hole; `key_type` is "A"/"B" (the key that opened it) or None;
    `reason` says why a sector could not be read ("nokey", "readfail",
    "removed") or is None when it was read."""

    data_hex: Optional[str] = None
    key_type: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class DeviceDumpResult:
    """What the device reported for the dump it just ran."""

    sectors: Dict[int, DeviceDumpSector] = field(default_factory=dict)
    read: int = 0
    failed: int = 0
    total: int = 0
    # The raw block 0 the card returned (32 hex chars), plus the fields the
    # firmware dissected out of it. The host re-parses the raw bytes with its
    # own block0.py — the same dissection, so both agree.
    card0: Optional[str] = None
    card0_fields: Dict[str, str] = field(default_factory=dict)

    def blocks_of(self, sector: int) -> Optional[List[str]]:
        """The sector's 4 blocks (32 hex chars each), or None for a hole."""
        entry = self.sectors.get(sector)
        if entry is None or not entry.data_hex:
            return None
        return [entry.data_hex[i : i + 32] for i in range(0, len(entry.data_hex), 32)]


def parse_device_dump(data: Dict[str, str]) -> DeviceDumpResult:
    """Parse the device's dump reply into a `DeviceDumpResult`.

    `data` is the `:key value` dict the link collected: one `mifare_dump_sN`
    entry per sector, the `mifare_dump_stat` counters and the `mifare_card0`
    line.
    """
    result = DeviceDumpResult()
    for key, value in data.items():
        match = _DUMP_SECTOR_RE.match(key)
        if match:
            parts = (value or "").split()
            entry = DeviceDumpSector()
            if parts and _HEX_SECTOR_RE.match(parts[0].strip().upper()):
                entry.data_hex = parts[0].strip().upper()
            if len(parts) > 1 and parts[1].upper() in ("A", "B"):
                entry.key_type = parts[1].upper()
            if len(parts) > 2 and parts[2]:
                entry.reason = parts[2]
            result.sectors[int(match.group(1))] = entry
            continue
        if key == "mifare_card0":
            parts = (value or "").split()
            if parts and len(parts[0]) == 32:
                result.card0 = parts[0].upper()
            for token in parts[1:]:
                name, _, raw = token.partition("=")
                if name:
                    result.card0_fields[name] = raw
            continue
        if key == "mifare_dump_stat":
            for token in (value or "").split():
                name, _, raw = token.partition("=")
                if name == "read" and raw.isdigit():
                    result.read = int(raw)
                elif name == "failed" and raw.isdigit():
                    result.failed = int(raw)
                elif name == "total" and raw.isdigit():
                    result.total = int(raw)
    return result


def build_device_dump_command(sectors: int) -> str:
    """The one REPL line that triggers the on-device dump."""
    return f"mifare dump --sectors {int(sectors)}"


def _classify_dump_error(message: str) -> DeviceDumpError:
    """Map a firmware error message to the right exception.

    "unknown mifare subcommand" is how the firmware rejects a verb it doesn't
    know — i.e. the flashed build predates the on-device dump.
    """
    low = (message or "").lower()
    if "unknown mifare subcommand" in low or "unknown command" in low:
        return DeviceDumpUnsupported(
            message or "the flashed firmware has no `mifare dump` command"
        )
    return DeviceDumpError(message or "the device dump failed")


def run_device_dump(
    link,
    sectors: int,
    timeout: float = DEVICE_SWEEP_TIMEOUT,
    tap_timeout: float = 15.0,
    reconnects: int = DEVICE_SWEEP_RECONNECTS,
    quiet: bool = False,
) -> DeviceDumpResult:
    """Run the dump on the device and return its consolidated result.

    ONE round-trip: the firmware reads every sector its sweep cracked and
    answers with the per-sector lines, its counters and the block 0 it read.

    Raises `DeviceDumpUnsupported` when the flashed firmware has no
    `mifare dump` command, `DeviceDumpNoSweep` when it has no sweep results
    to dump with, and `DeviceDumpError` for any other refusal.
    """
    line = build_device_dump_command(sectors)
    attempts_left = reconnects + 1

    while True:
        try:
            response = _request_sweep(link, line, timeout, tap_timeout, quiet)
        except DeviceError as e:
            if attempts_left <= 0 or not link_lost(str(e)):
                raise DeviceDumpError(str(e)) from e
            attempts_left -= 1
            if not quiet:
                print_warning(
                    "the board disconnected during the dump (it rebooted?) — "
                    "reconnecting and retrying..."
                )
            if not _reconnect(link):
                raise DeviceDumpError(
                    "the board disconnected during the dump and did not come "
                    "back — re-plug it and run the command again"
                ) from e
            continue
        if not response.ok:
            message = (response.message or "").strip()
            if _NO_SWEEP_MARKER in message.lower():
                raise DeviceDumpNoSweep(message)
            raise _classify_dump_error(message)
        break

    result = parse_device_dump(response.data)
    if not result.sectors and not result.total:
        raise DeviceDumpUnsupported("the device answered without a dump result")
    return result
