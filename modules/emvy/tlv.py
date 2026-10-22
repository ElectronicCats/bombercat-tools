#!/usr/bin/env python3

# Electronic Cats
# tlv.py — minimal BER-TLV helpers for the small amount of EMV parsing the
# host needs: walking the constructed templates a card returns (6F/A5/BF0C/
# 61/77/70) to find one tag, and expanding a DOL (Data Object List — the
# tag+length list a card announces in 9F38) into the (tag, length) pairs a
# GPO command body must fill. This is deliberately not a general TLV/DOL
# engine (no long-form tag numbers beyond two bytes, no multi-byte length
# edge cases beyond the long-form length encoding cards actually use): the
# host only probes, and the full EMV decoding lives in the firmware.
# Distributed as-is; no warranty is given.

from typing import List, Optional, Tuple

# Constructed EMV templates whose children tlv_find() recurses into.
_CONSTRUCTED = (0x6F, 0xA5, 0xBF0C, 0x61, 0x77, 0x70)


def _read_tag(data: bytes, i: int) -> Tuple[Optional[int], int]:
    """One tag at ``i`` (1- or 2-byte EMV tag number); None when truncated."""
    if i >= len(data):
        return None, i
    tag = data[i]
    i += 1
    if tag & 0x1F == 0x1F:
        if i >= len(data):
            return None, i
        tag = (tag << 8) | data[i]
        i += 1
    return tag, i


def _read_length(data: bytes, i: int) -> Tuple[Optional[int], int]:
    """One length at ``i`` (short form or 0x8N long form); None when truncated."""
    if i >= len(data):
        return None, i
    ln = data[i]
    i += 1
    if ln & 0x80:
        n = ln & 0x7F
        if i + n > len(data):
            return None, i
        ln = int.from_bytes(data[i : i + n], "big")
        i += n
    return ln, i


def tlv_find(data: bytes, want: int, depth: int = 0) -> Optional[bytes]:
    """Return the value of the first ``want`` tag inside ``data``, recursing
    into the constructed EMV templates (6F/A5/BF0C/61/77/70). Returns None
    when the tag is absent or ``data`` is too truncated to walk. ``want`` is
    a 1- or 2-byte tag number (e.g. ``0x4F`` for an AID, ``0x9F38`` for a
    PDOL, ``0x94`` for an AFL)."""
    if depth > 6:
        return None
    i = 0
    while i + 2 <= len(data):
        tag, i = _read_tag(data, i)
        if tag is None:
            return None
        ln, i = _read_length(data, i)
        if ln is None or i + ln > len(data):
            return None
        val = data[i : i + ln]
        if tag == want:
            return val
        if tag in _CONSTRUCTED:
            hit = tlv_find(val, want, depth + 1)
            if hit is not None:
                return hit
        i += ln
    return None


def parse_dol(dol: bytes) -> List[Tuple[int, int]]:
    """Expand a DOL (e.g. the PDOL announced in 9F38) into its
    ``(tag, length)`` pairs — tag numbers with no value, just the lengths a
    command body must fill. Trailing garbage is ignored."""
    items: List[Tuple[int, int]] = []
    i = 0
    while i < len(dol):
        tag, i = _read_tag(dol, i)
        if tag is None or i >= len(dol):
            break
        items.append((tag, dol[i]))
        i += 1
    return items


__all__ = ["tlv_find", "parse_dol"]
