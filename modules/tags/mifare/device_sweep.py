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

from ...utils.output import print_info
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

_SECTOR_KEY_RE = re.compile(r"^mifare_check_s(\d+)$")
_HEX_KEY_RE = re.compile(r"^[0-9A-F]{12}$")

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
    """

    keys: Dict[int, Tuple[Optional[str], Optional[str]]] = field(default_factory=dict)
    attempts: int = 0
    naive: int = 0
    recovered: int = 0
    total: int = 0

    def recovered_count(self) -> int:
        """How many (sector, key type) slots actually carry a key — the same
        unit the device's own `recovered=r/total` counter uses."""
        return sum(
            (1 if key_a else 0) + (1 if key_b else 0)
            for key_a, key_b in self.keys.values()
        )


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
        elif name == "recovered" and "/" in raw:
            got, _, total = raw.partition("/")
            if got.isdigit():
                result.recovered = int(got)
            if total.isdigit():
                result.total = int(total)
    return result


def build_device_command(sectors: int, keys: Optional[List[str]] = None) -> str:
    """The one REPL line that triggers the on-device sweep.

    `keys`, when given, REPLACES the device's dictionary for this run (the
    same semantics as the host's `--keys`); at most
    `DEVICE_SWEEP_MAX_KEYS` of them, comma-separated, 12 hex chars each.
    """
    line = f"mifare check --sectors {int(sectors)}"
    if keys:
        line += " --keys " + ",".join(k.strip().upper() for k in keys)
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


def run_device_sweep(
    link,
    sectors: int,
    keys: Optional[List[str]] = None,
    timeout: float = DEVICE_SWEEP_TIMEOUT,
    tap_timeout: float = 15.0,
) -> DeviceSweepResult:
    """Run the sweep on the device and return its consolidated result.

    ONE round-trip: the firmware sweeps every sector x {A,B} itself and
    answers with the per-sector lines plus its counters. If no card is
    selected yet, ask the user to tap one and retry once (the same
    choreography the host-driven sweep uses).

    Raises `DeviceSweepUnsupported` when the flashed firmware has no
    `mifare check` command, and `DeviceSweepError` for any other refusal.
    """
    line = build_device_command(sectors, keys)
    response = link.command(line, read_timeout=timeout)

    if not response.ok:
        message = (response.message or "").strip()
        if "no card selected" in message.lower():
            print_info(
                f"no card selected — tap a Mifare Classic card (up to "
                f"{tap_timeout:g}s)..."
            )
            if not _wait_for_mifare_card(link, tap_timeout):
                raise DeviceSweepError("no card was tapped")
            response = link.command(line, read_timeout=timeout)
        if not response.ok:
            raise _classify_error((response.message or "").strip())

    result = parse_device_sweep(response.data)
    if not result.keys and not result.total:
        # The device said "+OK" but sent no sweep result at all: it either
        # doesn't know the command (and answered something else) or its
        # answer was lost. Either way there is nothing to trust — report it
        # as unsupported so the caller can fall back instead of reporting an
        # empty card as "every sector unknown".
        raise DeviceSweepUnsupported("the device answered without a sweep result")
    return result
