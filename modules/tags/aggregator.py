#!/usr/bin/env python3

# Electronic Cats
# aggregator.py — collapses a stream of Tag detections into a per-tag summary
# for `bombercat tags scan`: how many times each one was seen, and when
# (relative to when the scan started).
# docs/CLI_IMPROVEMENTS_DetectTags.md §3.3.
# Distributed as-is; no warranty is given.

import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .parser import Tag

# Columns this module computes itself. A device-supplied `extra` key with the
# same name (e.g. a firmware emitting `count=99`) must not silently overwrite
# them (M13) — such keys are renamed with an `x_` prefix instead.
_RESERVED_KEYS = {"uid", "tech", "protocol", "count", "first_s", "last_s", "ts_ms"}


def _merge_extra(row: Dict[str, object], extra: Dict[str, str]) -> None:
    for key, value in extra.items():
        row["x_" + key if key in _RESERVED_KEYS else key] = value


@dataclass
class _Entry:
    tag: Tag
    count: int
    first_s: float
    last_s: float


class TagAggregator:
    """Feed `Tag`s in via `add()`; read the aggregate back via `to_dict()`.

    Grouped by UID — or, when the firmware prints none (NFC-B/F in legacy
    mode), by `tech:protocol:ts_ms` so each such detection stays its own row
    rather than colliding under one "unavailable" bucket.
    """

    def __init__(self, start: Optional[float] = None):
        self._start = start if start is not None else time.monotonic()
        self._entries: Dict[str, _Entry] = {}
        self._order: List[str] = []

    def add(self, tag: Tag) -> None:
        key = tag.uid or f"{tag.tech}:{tag.protocol}:{tag.ts_ms}"
        elapsed = time.monotonic() - self._start
        entry = self._entries.get(key)
        if entry is None:
            self._entries[key] = _Entry(
                tag=tag, count=1, first_s=elapsed, last_s=elapsed
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
                "uid": e.tag.uid,
                "tech": e.tag.tech,
                "protocol": e.tag.protocol,
                "count": e.count,
                "first_s": round(e.first_s, 1),
                "last_s": round(e.last_s, 1),
            }
            _merge_extra(row, e.tag.extra)
            rows.append(row)
        return rows
