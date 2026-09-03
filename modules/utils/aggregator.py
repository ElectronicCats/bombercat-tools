#!/usr/bin/env python3

# Electronic Cats
# aggregator.py — generic accumulate/dedupe/to_dict engine shared by
# ReaderAggregator (modules/readers/aggregator.py) and TagAggregator
# (modules/tags/aggregator.py): collapses a stream of detections into one row
# per unique key, tracking how many times it was seen and when (relative to
# when the scan started). Domain specifics (the key, the row's own columns,
# reserved-key handling) are supplied by the caller via callbacks.
# Distributed as-is; no warranty is given.

import time
from dataclasses import dataclass
from typing import Callable, Dict, Generic, List, Optional, Set, TypeVar

T = TypeVar("T")


def merge_extra(
    row: Dict[str, object], extra: Dict[str, str], reserved: Set[str]
) -> None:
    """Merge a device-supplied `extra` dict into `row`, renaming any key that
    collides with a column the aggregator computes itself (M13) with an
    `x_` prefix instead of silently overwriting it."""
    for key, value in extra.items():
        row["x_" + key if key in reserved else key] = value


@dataclass
class _Entry(Generic[T]):
    item: T
    count: int
    first_s: float
    last_s: float


class Aggregator(Generic[T]):
    """Feed items in via `add()`; read the aggregate back via `to_dict()`.

    Grouped by `key_fn(item)`. `row_fn(item)` builds the item's own columns;
    `count`/`first_s`/`last_s` are appended on top, then `extra_fn(item)`
    (if given) is merged in via `merge_extra`.
    """

    def __init__(
        self,
        key_fn: Callable[[T], str],
        row_fn: Callable[[T], Dict[str, object]],
        extra_fn: Optional[Callable[[T], Dict[str, str]]] = None,
        reserved_keys: Optional[Set[str]] = None,
        start: Optional[float] = None,
    ):
        self._key_fn = key_fn
        self._row_fn = row_fn
        self._extra_fn = extra_fn
        self._reserved_keys = reserved_keys or set()
        self._start = start if start is not None else time.monotonic()
        self._entries: Dict[str, _Entry] = {}
        self._order: List[str] = []

    def add(self, item: T) -> None:
        key = self._key_fn(item)
        elapsed = time.monotonic() - self._start
        entry = self._entries.get(key)
        if entry is None:
            self._entries[key] = _Entry(
                item=item, count=1, first_s=elapsed, last_s=elapsed
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
            row = self._row_fn(e.item)
            row["count"] = e.count
            row["first_s"] = round(e.first_s, 1)
            row["last_s"] = round(e.last_s, 1)
            if self._extra_fn is not None:
                merge_extra(row, self._extra_fn(e.item), self._reserved_keys)
            rows.append(row)
        return rows
