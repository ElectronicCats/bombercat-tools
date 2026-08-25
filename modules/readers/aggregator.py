#!/usr/bin/env python3

# Electronic Cats
# aggregator.py — collapses a stream of Reader detections into a per-reader
# summary for `bombercat readers scan`: how many times each fingerprint was
# seen, and when (relative to when the scan started). Twin of
# modules/tags/aggregator.py, grouped by Reader.fingerprint instead of UID.
# docs/IMPLEMENTATION_PLAN_DetectReaders_CLI.md §3.2.
# Distributed as-is; no warranty is given.

import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .parser import Reader

# Columns this module computes itself. A device-supplied `extra` key with the
# same name must not silently overwrite them (M13) — such keys are renamed
# with an `x_` prefix instead.
_RESERVED_KEYS = {
    "label",
    "tech",
    "protocol",
    "intf",
    "apdu",
    "aid",
    "count",
    "first_s",
    "last_s",
    "ts_ms",
}


def _merge_extra(row: Dict[str, object], extra: Dict[str, str]) -> None:
    for key, value in extra.items():
        row["x_" + key if key in _RESERVED_KEYS else key] = value


@dataclass
class _Entry:
    reader: Reader
    count: int
    first_s: float
    last_s: float


class ReaderAggregator:
    """Feed `Reader`s in via `add()`; read the aggregate back via `to_dict()`.

    Grouped by `Reader.fingerprint` (label + aid, or label + tech:protocol
    when the label is "unknown") so repeat activations of the same
    reader/terminal collapse into one row with a count.
    """

    def __init__(self, start: Optional[float] = None):
        self._start = start if start is not None else time.monotonic()
        self._entries: Dict[str, _Entry] = {}
        self._order: List[str] = []

    def add(self, reader: Reader) -> None:
        key = reader.fingerprint
        elapsed = time.monotonic() - self._start
        entry = self._entries.get(key)
        if entry is None:
            self._entries[key] = _Entry(
                reader=reader, count=1, first_s=elapsed, last_s=elapsed
            )
            self._order.append(key)
        else:
            entry.count += 1
            entry.last_s = elapsed

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def total_detections(self) -> int:
        return sum(e.count for e in self._entries.values())

    def to_dict(self) -> List[Dict[str, object]]:
        rows: List[Dict[str, object]] = []
        for key in self._order:
            e = self._entries[key]
            row: Dict[str, object] = {
                "label": e.reader.label,
                "tech": e.reader.tech,
                "protocol": e.reader.protocol,
                "intf": e.reader.intf,
                "apdu": e.reader.apdu,
                "aid": e.reader.aid,
                "count": e.count,
                "first_s": round(e.first_s, 1),
                "last_s": round(e.last_s, 1),
            }
            _merge_extra(row, e.reader.extra)
            rows.append(row)
        return rows
