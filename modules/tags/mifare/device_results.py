#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

"""The results the board stored while working on its own (Fase 4, tarea 4.6).

The standalone workflow reads a card with NO PC attached: the dump goes out
over the serial port, where nobody is listening when the board is in the
field. The firmware keeps the last results in its own flash (the
`MifareResultStore` ring), so the operator collects them LATER, over USB.

This module owns that wire format and the two round-trips it needs:

  * `mifare results` — the listing: one `:mifare_resultN …` line per stored
    result (newest first) plus the `:mifare_results_stat` counters;
  * `mifare results show <n>` — one stored result, re-emitted by the
    firmware in the VERY SAME format a live dump uses (`:mifare_dump_sN …`,
    `:mifare_dump_stat …`, `:mifare_card0 …`), so the host rebuilds it with
    the code it already has.

Why the result is worth keeping at all: a stored result carries its HOLES
with their reason ("nokey", "readfail", "removed"). The canonical dump the
host rebuilds from it therefore keeps the `failed_sectors` list, and
`mifare analyze` turns that into the honest report the plan asks for in task
4.6 — the sectors that were never opened are declared, never called secure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ...core.bombercat import DeviceError
from .device_sweep import DeviceDumpResult, parse_device_dump

# The listing line: ":mifare_result0 seq=12 ts=34567 uid=AABBCCDD
# sectors=16 read=15 failed=1". The stat line ("mifare_results_stat") does
# NOT match: the index has to be digits.
_RESULT_RE = re.compile(r"^mifare_result(\d+)$")

# The firmware's wording when it does not know the command (an older build).
_UNKNOWN_MARKER = "unknown mifare subcommand"

# The firmware's wording when the ring is empty.
_NO_RESULT_MARKER = "no such stored result"


class DeviceResultsError(RuntimeError):
    """The device refused or failed to answer with its stored results."""


class DeviceResultsUnsupported(DeviceResultsError):
    """The flashed firmware has no `mifare results` command (an older build)."""


@dataclass
class DeviceResult:
    """One stored result: what the board read, and how it went.

    `ts_ms` is the board's own clock - milliseconds since it booted. The
    board has no real-time clock, so this is NOT a wall-clock time; it says
    how long after the boot the card was read, which is what the board knows.
    """

    index: int
    seq: int = 0
    ts_ms: int = 0
    uid: Optional[str] = None
    sectors: int = 0
    read: int = 0
    failed: int = 0

    @property
    def uptime_s(self) -> float:
        """Seconds the board had been up when it read the card."""
        return self.ts_ms / 1000.0

    @property
    def gaps(self) -> int:
        """The sectors that were never read - declared, never assumed safe."""
        return self.failed

    def to_dict(self) -> Dict[str, object]:
        return {
            "index": self.index,
            "seq": self.seq,
            "uptime_s": round(self.uptime_s, 3),
            "uid": self.uid,
            "sectors": self.sectors,
            "read": self.read,
            "failed": self.failed,
        }


@dataclass
class DeviceResults:
    """Every result the board has stored, newest first."""

    results: List[DeviceResult] = field(default_factory=list)
    slots: int = 0
    used: int = 0
    newest_seq: int = 0

    @property
    def empty(self) -> bool:
        return not self.results

    def to_dict(self) -> Dict[str, object]:
        return {
            "slots": self.slots,
            "used": self.used,
            "newest_seq": self.newest_seq,
            "results": [r.to_dict() for r in self.results],
        }


@dataclass
class DeviceStoredResult:
    """A stored result: its metadata plus the dump the board kept."""

    meta: Optional[DeviceResult]
    dump: DeviceDumpResult

    @property
    def uid(self) -> Optional[str]:
        return self.meta.uid if self.meta else None


def _int_field(fields: Dict[str, str], name: str) -> int:
    raw = fields.get(name, "")
    return int(raw) if raw.isdigit() else 0


def parse_device_results(data: Dict[str, str]) -> DeviceResults:
    """Parse the device's listing into a `DeviceResults`.

    `data` is the `:key value` dict the link collected: one
    `mifare_resultN` entry per stored result plus the `mifare_results_stat`
    line. A malformed token is ignored, never guessed.
    """
    out = DeviceResults()
    for key, value in (data or {}).items():
        match = _RESULT_RE.match(key)
        if not match:
            continue
        fields: Dict[str, str] = {}
        for token in (value or "").split():
            name, _, raw = token.partition("=")
            if name:
                fields[name] = raw
        uid = (fields.get("uid") or "").strip()
        out.results.append(
            DeviceResult(
                index=int(match.group(1)),
                seq=_int_field(fields, "seq"),
                ts_ms=_int_field(fields, "ts"),
                uid=uid.upper() if uid else None,
                sectors=_int_field(fields, "sectors"),
                read=_int_field(fields, "read"),
                failed=_int_field(fields, "failed"),
            )
        )
    # The firmware lists them newest first; keep that order whatever the
    # dict handed us.
    out.results.sort(key=lambda r: r.index)

    stat = (data or {}).get("mifare_results_stat", "") or ""
    for token in stat.split():
        name, _, raw = token.partition("=")
        if name == "slots" and raw.isdigit():
            out.slots = int(raw)
        elif name == "used" and raw.isdigit():
            out.used = int(raw)
        elif name == "seq" and raw.isdigit():
            out.newest_seq = int(raw)
    return out


def _classify_error(message: str) -> DeviceResultsError:
    """Map a firmware error message to the right exception.

    "unknown mifare subcommand" is how the firmware rejects a verb it does
    not know - i.e. the flashed build predates the result store.
    """
    low = (message or "").lower()
    if _UNKNOWN_MARKER in low or "unknown command" in low:
        return DeviceResultsUnsupported(
            message or "the flashed firmware has no `mifare results` command"
        )
    return DeviceResultsError(message or "the device could not answer")


def run_device_results(link, timeout: float = 30.0) -> DeviceResults:
    """Ask the board for its stored results and return them.

    Raises `DeviceResultsUnsupported` when the flashed firmware has no
    `mifare results` command, and `DeviceResultsError` for any other
    refusal.
    """
    try:
        response = link.command("mifare results", read_timeout=timeout)
    except DeviceError as e:
        raise DeviceResultsError(str(e)) from e
    if not response.ok:
        raise _classify_error((response.message or "").strip())
    return parse_device_results(response.data)


def run_device_stored_result(
    link, index: int = 0, timeout: float = 60.0
) -> DeviceStoredResult:
    """Fetch one stored result (0 = the newest) from the board.

    The firmware re-emits it in the live dump's format, so the host rebuilds
    the very same canonical dump - holes included - that it would have got
    with the card on the reader.
    """
    try:
        response = link.command(
            f"mifare results show {int(index)}", read_timeout=timeout
        )
    except DeviceError as e:
        raise DeviceResultsError(str(e)) from e
    if not response.ok:
        message = (response.message or "").strip()
        if _NO_RESULT_MARKER in message.lower():
            raise DeviceResultsError(message or "the board has no such stored result")
        raise _classify_error(message)

    results = parse_device_results(response.data)
    meta = results.results[0] if results.results else None
    dump = parse_device_dump(response.data)
    if not dump.sectors and not dump.total:
        raise DeviceResultsError(
            "the board answered without a stored dump — the result is gone"
        )
    return DeviceStoredResult(meta=meta, dump=dump)
