#!/usr/bin/env python3

# Electronic Cats
# parser.py — turns DetectTags serial output into Tag objects. Two backends
# behind one interface: structured ":tag" events (firmware with FW-1) and the
# human-readable text of displayCardInfo() (already-published .uf2 images).
# docs/CLI_IMPROVEMENTS_DetectTags.md §3.5, §6.
# Distributed as-is; no warranty is given.

import re
from dataclasses import dataclass, field
from typing import Dict, Optional

# :tag <ts_ms> <tech> <protocol> <uid_hex|-> [k=v ...]
_TAG_EVENT = re.compile(r"^:tag\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s*(.*)$")

# Legacy text: "\tNFC ID = 0x04 0x1a 0x2b"  /  "\tTechnology: NFC-A"
# PUPI (NFC-B) and IDm (NFC-F) are the UID-equivalent fields firmware with the
# PUPI/IDm extraction (see DetectTags.ino) prints for those two technologies.
_LEGACY_TECH = re.compile(r"^\s*Technology:\s*(\S+)")
_LEGACY_ID = re.compile(r"^\s*(?:NFC ID|ID|PUPI|IDm)\s*=\s*(.+)$")
_LEGACY_PROTO_NUM = re.compile(r"Remote activated tag type:\s*(\d+)")
_LEGACY_PROTO_NAMED = re.compile(r"Remote (\w+) card activated")
# Printed unconditionally after every detection's prose block (loop(), not
# displayCardInfo()) - the reliable "this detection is over" signal for
# firmware builds that never print a PUPI/IDm line at all (pre-extraction).
_LEGACY_CLOSE = re.compile(r"^\s*Remove the Card")

# Protocol.h values from Electronic_Cats_PN7150 (MIFARE is 0x80, not
# contiguous with the rest; ISO15693 never arrives via this path because the
# firmware prints it through the named-protocol message instead).
_PROTOCOL_BY_NUM = {
    0x1: "T1T",
    0x2: "T2T",
    0x3: "T3T",
    0x4: "ISODEP",
    0x5: "NFCDEP",
    0x6: "ISO15693",
    0x80: "MIFARE",
}


@dataclass
class Tag:
    """One detection. `uid` is None when the firmware prints no UID at all
    (NFC-B / NFC-F on firmware that predates PUPI/IDm extraction) — never
    invent a value."""

    uid: Optional[str] = None
    tech: Optional[str] = None
    protocol: Optional[str] = None
    ts_ms: Optional[int] = None
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def pretty_uid(self) -> str:
        if not self.uid:
            return f"unavailable ({self.tech or 'unknown tech'}: firmware prints no ID)"
        return ":".join(self.uid[i : i + 2] for i in range(0, len(self.uid), 2))


def _hex_compact(text: str) -> Optional[str]:
    """'0x04 0x1a 0x2b' -> '041A2B';  'null' -> None."""
    if not text or text.strip() == "null":
        return None
    out = "".join(t[2:] if t.lower().startswith("0x") else t for t in text.split())
    return out.upper() or None


class TagParser:
    """Feed serial lines in; get a Tag back once a detection completes, else
    None. Switches to structured mode permanently on the first ':tag' seen."""

    def __init__(self):
        self.structured = False
        self._pending = Tag()

    def feed(self, line: str) -> Optional[Tag]:
        m = _TAG_EVENT.match(line)
        if m:
            self.structured = True
            ts, tech, proto, uid, rest = m.groups()
            extra = dict(kv.split("=", 1) for kv in rest.split() if "=" in kv)
            return Tag(
                uid=None if uid == "-" else uid.upper(),
                tech=tech,
                protocol=proto,
                ts_ms=int(ts),
                extra=extra,
            )
        if self.structured:
            return None  # human text no longer carries information

        # --- legacy mode: accumulate until we have tech + (maybe) a UID ---
        m = _LEGACY_PROTO_NUM.search(line)
        if m:
            self._pending = Tag(
                protocol=_PROTOCOL_BY_NUM.get(int(m.group(1)), f"proto {m.group(1)}")
            )
            return None
        m = _LEGACY_PROTO_NAMED.search(line)
        if m:
            self._pending = Tag(protocol=m.group(1).upper())
            return None
        m = _LEGACY_TECH.match(line)
        if m:
            self._pending.tech = m.group(1)
            return None
        m = _LEGACY_ID.match(line)
        if m and self._pending.tech:
            self._pending.uid = _hex_compact(m.group(1))
            done, self._pending = self._pending, Tag()
            return done
        if _LEGACY_CLOSE.match(line) and self._pending.tech in ("NFC-B", "NFC-F"):
            # No PUPI/IDm line ever came (firmware predates the extraction) -
            # close with uid=None rather than hang waiting for a line that
            # will never arrive.
            done, self._pending = self._pending, Tag()
            return done
        return None
