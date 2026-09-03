#!/usr/bin/env python3

# Electronic Cats
# aggregator.py — collapses a stream of Reader detections into a per-reader
# summary for `bombercat readers scan`: how many times each fingerprint was
# seen, and when (relative to when the scan started). Thin wrapper over the
# shared modules.utils.aggregator.Aggregator engine; twin of
# modules/tags/aggregator.py, grouped by Reader.fingerprint instead of UID.
# docs/IMPLEMENTATION_PLAN_DetectReaders_CLI.md §3.2.
# Distributed as-is; no warranty is given.

from typing import Dict, Optional

from ..utils.aggregator import Aggregator
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


def _row(reader: Reader) -> Dict[str, object]:
    return {
        "label": reader.label,
        "tech": reader.tech,
        "protocol": reader.protocol,
        "intf": reader.intf,
        "apdu": reader.apdu,
        "aid": reader.aid,
    }


class ReaderAggregator(Aggregator):
    """Feed `Reader`s in via `add()`; read the aggregate back via `to_dict()`.

    Grouped by `Reader.fingerprint` (label + aid, or label + tech:protocol
    when the label is "unknown") so repeat activations of the same
    reader/terminal collapse into one row with a count.
    """

    def __init__(self, start: Optional[float] = None):
        super().__init__(
            key_fn=lambda reader: reader.fingerprint,
            row_fn=_row,
            extra_fn=lambda reader: reader.extra,
            reserved_keys=_RESERVED_KEYS,
            start=start,
        )
