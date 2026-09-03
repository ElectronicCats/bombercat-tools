#!/usr/bin/env python3

# Electronic Cats
# aggregator.py — collapses a stream of Tag detections into a per-tag summary
# for `bombercat tags scan`: how many times each one was seen, and when
# (relative to when the scan started). Thin wrapper over the shared
# modules.utils.aggregator.Aggregator engine; twin of
# modules/readers/aggregator.py, grouped by UID instead of fingerprint.
# docs/CLI_IMPROVEMENTS_DetectTags.md §3.3.
# Distributed as-is; no warranty is given.

from typing import Dict, Optional

from ..utils.aggregator import Aggregator
from .parser import Tag

# Columns this module computes itself. A device-supplied `extra` key with the
# same name (e.g. a firmware emitting `count=99`) must not silently overwrite
# them (M13) — such keys are renamed with an `x_` prefix instead.
_RESERVED_KEYS = {"uid", "tech", "protocol", "count", "first_s", "last_s", "ts_ms"}


def _key(tag: Tag) -> str:
    return tag.uid or f"{tag.tech}:{tag.protocol}:{tag.ts_ms}"


def _row(tag: Tag) -> Dict[str, object]:
    return {"uid": tag.uid, "tech": tag.tech, "protocol": tag.protocol}


class TagAggregator(Aggregator):
    """Feed `Tag`s in via `add()`; read the aggregate back via `to_dict()`.

    Grouped by UID — or, when the firmware prints none (NFC-B/F in legacy
    mode), by `tech:protocol:ts_ms` so each such detection stays its own row
    rather than colliding under one "unavailable" bucket.
    """

    def __init__(self, start: Optional[float] = None):
        super().__init__(
            key_fn=_key,
            row_fn=_row,
            extra_fn=lambda tag: tag.extra,
            reserved_keys=_RESERVED_KEYS,
            start=start,
        )
