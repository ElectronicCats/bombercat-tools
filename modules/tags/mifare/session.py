#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

# The one place `tags mifare` reaches the hardware: opening a verified link to
# the MifareClassic firmware, waiting for a card tap, and running REPL lines.
#
# Every command goes through here, so tests point the whole group at a fake by
# patching this module's `resolve_port`/`DeviceLink`.

import re
import time
from typing import Optional, Tuple

import click

from ...core.bombercat import DeviceLink, resolve_port
from ...core.firmwares import CAP_MIFARE
from ...utils.detection_cli import device_session
from ...utils.output import print_info
from .common import _sector_first_block


# Always printed right before the firmware opens its interactive session
# (probeMifareBlock() runs, then openMifareSession() — see MifareClassic.ino's
# handleTagDetected()), so seeing it is a reliable "the session is open now".
_MIFARE_PROBE_RE = re.compile(r"^:mifare\s")
_MIFARE_TAP_TIMEOUT = 15.0

# The firmware's on-detection auto-probe streams one line per sector it
# resolves (:mifare_check_sN) BEFORE it emits the session-open signal — the
# signal comes last so the host drains the sweep output on a clean stream.
# Those lines are also the proof that the card WAS seen and the sweep is
# running, which is what tells the host to stop counting the tap timeout and
# wait for the sweep instead.
_MIFARE_SWEEP_LINE_RE = re.compile(r"^:mifare_check_s\d+\b")


def _mifare_session(port: Optional[str], device_id: Optional[int] = None, trace=None):
    """Open a verified link for the `tags mifare` commands, naming the
    MifareClassic firmware in its error message. `resolve_port`/`DeviceLink`
    are looked up by name at call time so tests can monkeypatch this
    module's copies."""
    return device_session(
        resolve_port,
        DeviceLink,
        "tags mifare",
        "MifareClassic",
        port,
        device_id,
        trace,
        requires=CAP_MIFARE,
    )


def _wait_for_mifare_card(link, timeout: float, sweep_timeout: Optional[float] = None):
    """Block until the firmware's auto-probe ':mifare' event appears — proof
    it has opened the interactive session — or `timeout` seconds pass.

    The firmware opens that session only AFTER its on-detection auto-probe has
    swept every sector, and that sweep lasts as long as the card needs: ~11 s
    measured on a 1K card with default keys, and minutes on a card whose
    sectors nothing opens. `timeout` therefore only bounds "did the user tap
    at all"; once the sweep's own per-sector lines start arriving (the proof
    that the card was seen), the wait extends to `sweep_timeout` so a slow
    sweep is not mistaken for a missing card.
    """
    deadline = time.monotonic() + timeout
    sweep_deadline = time.monotonic() + (sweep_timeout or timeout)
    for line in link.stream(yield_empty=True):
        if line and _MIFARE_PROBE_RE.match(line):
            return True
        if line and _MIFARE_SWEEP_LINE_RE.match(line):
            deadline = sweep_deadline
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
