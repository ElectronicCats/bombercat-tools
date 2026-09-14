#!/usr/bin/env python3

# Electronic Cats
# parser.py — turns DetectTags serial output into Tag objects. Two backends
# behind one interface: structured ":tag" events (firmware with FW-1) and the
# human-readable text of displayCardInfo() (already-published .uf2 images).
# docs/CLI_IMPROVEMENTS_DetectTags.md §3.5, §6.
# Distributed as-is; no warranty is given.

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, Optional

from .chips import fingerprint

logger = logging.getLogger("rich")

# :tag <ts_ms> <tech> <protocol> <uid_hex|-> [k=v ...]
_TAG_EVENT = re.compile(r"^:tag\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s*(.*)$")

# A valid UID is hex digits only (after `_hex_compact` strips any "0x" tokens).
_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")

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
# Prose the sketch prints for every NFC-A detection just before emitTagEvent().
# The ':tag' event itself never carries them, so they are buffered here and
# folded into the next structured event as atqa/sak/model (see chips.py).
_SENS_RES = re.compile(r"^\s*SENS\s*RES\s*=\s*(.+)$")
_SEL_RES = re.compile(r"^\s*SEL\s*RES\s*=\s*(.+)$")

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


def _validated_uid(raw: str, extra: Dict[str, str]) -> Optional[str]:
    """Only accept `raw` as a UID if it's actually hex digits. Garbage (glitch
    bytes, a malformed line) is kept as text in `extra["raw_uid"]` instead of
    silently becoming a "valid-looking" UID that pollutes reports/exports."""
    if raw is None:
        return None
    if _HEX_RE.match(raw):
        return raw
    logger.warning("discarding non-hex UID %r", raw)
    extra["raw_uid"] = raw
    return None


class TagParser:
    """Feed serial lines in; get a Tag back once a detection completes, else
    None. Switches to structured mode permanently on the first ':tag' seen."""

    def __init__(self):
        self.structured = False
        self._pending = Tag()
        self._pending_id_seen = False
        self._sens_res: Optional[str] = None
        self._sel_res: Optional[str] = None

    @property
    def _deferred(self) -> bool:
        """True while a complete legacy NFC-A detection is being held back.

        NFC-A prints `SEL RES` (the SAK) one line *after* the UID, so closing
        on the UID line - as every other technology does - would emit the tag
        before its own SAK exists. Only NFC-A waits, and only by one line:
        both the published .uf2 and the current sketch print SEL RES
        unconditionally right after NFC ID, with `Remove the Card` behind it
        as a backstop.
        """
        return (
            self._pending_id_seen
            and self._pending.tech == "NFC-A"
            and self._sel_res is None
        )

    def _close(self) -> Tag:
        """Finish the pending legacy detection: fold in the chip fingerprint
        (NFC-A only - ATQA/SAK are an ISO14443-3A notion, and NFC-B's longer
        SENSB_RES shares the same prose label) and reset for the next one."""
        done = self._pending
        if done.tech == "NFC-A":
            for key, value in fingerprint(self._sens_res, self._sel_res).items():
                done.extra.setdefault(key, value)
        self._pending = Tag()
        self._pending_id_seen = False
        # The ATQA/SAK buffer is deliberately NOT cleared here: on a board that
        # emits both, the ':tag' event for this same detection arrives after
        # the prose and must still see them. It is cleared when the next
        # detection starts, and by the ':tag' path itself.
        return done

    def feed(self, line: str) -> Optional[Tag]:
        # Buffered before either mode gets a look: these lines are printed
        # ahead of the ':tag' event they describe, including the very first
        # one (which arrives while this parser is still in legacy mode).
        m = _SENS_RES.match(line)
        if m:
            self._sens_res = m.group(1)
            return None
        m = _SEL_RES.match(line)
        if m:
            # Read `_deferred` before storing: it is defined as "SAK not in
            # yet", so storing first would make it false by construction.
            deferred = self._deferred
            self._sel_res = m.group(1)
            # In legacy mode this is the line an NFC-A detection was held back
            # for — the SAK arrives one line after the UID.
            if deferred:
                return self._close()
            return None

        m = _TAG_EVENT.match(line)
        if m:
            self.structured = True
            ts, tech, proto, uid, rest = m.groups()
            extra = dict(
                kv.split("=", 1)
                for kv in rest.split()
                if "=" in kv and not kv.startswith("=")
            )
            # setdefault, not update: if a future firmware ever emits its
            # own atqa=/sak=/model= on the event, the device's value wins over
            # anything inferred from the prose.
            if tech == "NFC-A":
                for key, value in fingerprint(self._sens_res, self._sel_res).items():
                    extra.setdefault(key, value)
            self._sens_res = self._sel_res = None
            uid = None if uid == "-" else uid.upper()
            return Tag(
                uid=_validated_uid(uid, extra),
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
            done = self._close() if self._deferred else None
            self._sens_res = self._sel_res = None
            self._pending = Tag(
                protocol=_PROTOCOL_BY_NUM.get(int(m.group(1)), f"proto {m.group(1)}")
            )
            self._pending_id_seen = False
            return done
        m = _LEGACY_PROTO_NAMED.search(line)
        if m:
            done = self._close() if self._deferred else None
            self._sens_res = self._sel_res = None
            self._pending = Tag(protocol=m.group(1).upper())
            self._pending_id_seen = False
            return done
        m = _LEGACY_TECH.match(line)
        if m:
            self._pending.tech = m.group(1)
            if self._pending_id_seen and not self._deferred:
                return self._close()
            return None
        m = _LEGACY_ID.match(line)
        if m:
            # Buffer the UID even if Technology hasn't arrived yet - lines
            # can appear in either order. Finalize only once both are known.
            self._pending.uid = _validated_uid(
                _hex_compact(m.group(1)), self._pending.extra
            )
            self._pending_id_seen = True
            if self._pending.tech and not self._deferred:
                return self._close()
            return None
        if _LEGACY_CLOSE.match(line):
            # Printed after every prose block. Closes two cases: a deferred
            # NFC-A whose SEL RES somehow never arrived (safety net, so a
            # detection is never lost), and NFC-B/F on firmware that predates
            # PUPI/IDm extraction - no ID line will ever come, so close with
            # uid=None rather than hang waiting for one.
            if self._deferred or self._pending.tech in ("NFC-B", "NFC-F"):
                return self._close()
        return None
