#!/usr/bin/env python3

# Electronic Cats
# parser.py — turns DetectReaders serial output into Reader objects. Firmware
# ships with a frozen structured wire format from day one (no legacy text
# mode to fall back to, unlike TagParser): every detection is one ":reader"
# event. Twin of modules/tags/parser.py, same leading-marker convention.
# docs/IMPLEMENTATION_PLAN_DetectReaders_CLI.md §2, §3.2.
# Distributed as-is; no warranty is given.

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger("rich")

# :reader <ts_ms> <tech> <protocol> [intf=<name>] [apdu=<hex|->] [aid=<hex>]
#         [label=<slug>] [n=<count>]
_READER_EVENT = re.compile(r"^:reader\s+(\d+)\s+(\S+)\s+(\S+)\s*(.*)$")

# apdu/aid are only accepted as such if they're actually hex digits (after
# uppercasing) — same defensive validation as TagParser's UID.
_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")


@dataclass
class Reader:
    """One reader/terminal detection."""

    ts_ms: Optional[int] = None
    tech: Optional[str] = None
    protocol: Optional[str] = None
    intf: Optional[str] = None
    apdu: Optional[str] = None
    aid: Optional[str] = None
    label: Optional[str] = None
    n: Optional[int] = None
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def pretty_apdu(self) -> str:
        if not self.apdu:
            return "-"
        return " ".join(self.apdu[i : i + 2] for i in range(0, len(self.apdu), 2))

    @property
    def fingerprint(self) -> str:
        """Grouping key for `readers scan`: label (+ aid when known), with a
        tech:protocol suffix for "unknown" so distinct unclassified readers
        don't collapse into one bucket."""
        label = self.label or "unknown"
        parts = [label]
        if self.aid:
            parts.append(self.aid)
        if label == "unknown":
            parts.append(f"{self.tech}:{self.protocol}")
        return ":".join(parts)


def _validated_hex(raw: Optional[str], field_name: str, extra: Dict[str, str]) -> Optional[str]:
    """Only accept `raw` as hex if it's actually hex digits. Garbage is kept
    as text in `extra["raw_<field_name>"]` instead of silently becoming a
    "valid-looking" value that pollutes reports/exports."""
    if raw is None:
        return None
    if _HEX_RE.match(raw):
        return raw.upper()
    logger.warning("discarding non-hex %s %r", field_name, raw)
    extra[f"raw_{field_name}"] = raw
    return None


class ReaderParser:
    """Feed serial lines in; get a Reader back once a `:reader` event is
    seen, else None. No legacy mode: DetectReaders was born with structured
    events, so every non-matching line (prose, noise) is simply ignored."""

    def __init__(self):
        self.structured = False

    def feed(self, line: str) -> Optional[Reader]:
        line = line.rstrip("\r\n")
        m = _READER_EVENT.match(line)
        if not m:
            return None
        self.structured = True
        ts, tech, protocol, rest = m.groups()

        kv = dict(
            tok.split("=", 1)
            for tok in rest.split()
            if "=" in tok and not tok.startswith("=")
        )
        intf = kv.pop("intf", None)
        apdu_raw = kv.pop("apdu", None)
        aid_raw = kv.pop("aid", None)
        label = kv.pop("label", None)
        n_raw = kv.pop("n", None)
        extra: Dict[str, str] = dict(kv)  # unclaimed k=v tokens, forward-compatible

        apdu = None
        if apdu_raw is not None and apdu_raw != "-":
            apdu = _validated_hex(apdu_raw.upper(), "apdu", extra)
        aid = (
            _validated_hex(aid_raw.upper(), "aid", extra) if aid_raw is not None else None
        )
        n = int(n_raw) if n_raw is not None and n_raw.isdigit() else None

        return Reader(
            ts_ms=int(ts),
            tech=tech,
            protocol=protocol,
            intf=intf,
            apdu=apdu,
            aid=aid,
            label=label,
            n=n,
            extra=extra,
        )
