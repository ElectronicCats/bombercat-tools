#!/usr/bin/env python3

# Electronic Cats
# parser.py — turns magspoof serial output into MagEvent objects. Firmware
# ships with a frozen structured wire format from day one (no legacy text
# mode), same leading-marker convention as modules/tags/parser.py and
# modules/readers/parser.py: every reproduction, whether triggered by a
# command (`magplay`) or the physical NPIN button, is one ":mag" event.
# docs/IMPLEMENTATION_PLAN_MagSpoof_CLI.md §3.2.
# Distributed as-is; no warranty is given.

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger("rich")

# :mag <ts_ms> <track> [k=v ...]
_MAG_EVENT = re.compile(r"^:mag\s+(\d+)\s+(\S+)\s*(.*)$")


@dataclass
class MagEvent:
    """One magstripe track reproduction (command- or button-triggered)."""

    ts_ms: Optional[int] = None
    track: Optional[int] = None
    extra: Dict[str, str] = field(default_factory=dict)


class MagEventParser:
    """Feed serial lines in; get a MagEvent back once a `:mag` event is
    seen, else None. No legacy mode: magspoof was born with structured
    events, so every non-matching line (prose, noise) is simply ignored."""

    def __init__(self):
        self.structured = False

    def feed(self, line: str) -> Optional[MagEvent]:
        line = line.rstrip("\r\n")
        m = _MAG_EVENT.match(line)
        if not m:
            return None
        self.structured = True
        ts, track_raw, rest = m.groups()

        extra: Dict[str, str] = dict(
            tok.split("=", 1)
            for tok in rest.split()
            if "=" in tok and not tok.startswith("=")
        )

        track: Optional[int]
        if track_raw in ("1", "2"):
            track = int(track_raw)
        else:
            # Never invent a track number for something the firmware didn't
            # actually claim — keep the raw text instead, same defensive
            # posture as ReaderParser's non-hex apdu/aid handling.
            logger.warning("discarding unrecognised track %r", track_raw)
            extra["raw_track"] = track_raw
            track = None

        return MagEvent(ts_ms=int(ts), track=track, extra=extra)
