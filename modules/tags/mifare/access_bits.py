#!/usr/bin/env python3

# Electronic Cats
# access_bits.py - MIFARE Classic access bits (C1/C2/C3) dissection. Decodes
# a sector trailer's 3 access-condition bytes into the standard per-block
# permission tables (NXP MF1S50yyX datasheet, "Access bits" / access
# conditions for data blocks and sector trailers).
# Distributed as-is; no warranty is given.

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

_NEVER = "never"
_KEY_A = "key A"
_KEY_B = "key B"
_KEY_AB = "key A or B"


@dataclass
class DataBlockAccess:
    read: str
    write: str
    increment: str
    decrement: str  # decrement/transfer/restore share one condition


@dataclass
class TrailerAccess:
    key_a_write: str
    ac_read: str
    ac_write: str
    key_b_read: str
    key_b_write: str
    # Key A itself is never readable back from the card under any access
    # condition, so there's no key_a_read field.


@dataclass
class SectorAccessBits:
    raw: str  # the 3 access-condition bytes (6 hex chars), as read
    valid: bool  # False if the bits and their stored inverses disagree
    blocks: List[DataBlockAccess]  # 3 entries, for the sector's data blocks
    trailer: TrailerAccess


# (C1, C2, C3) -> permissions, data blocks (NXP MF1S50yyX, Table 7).
_DATA_BLOCK_TABLE = {
    (0, 0, 0): DataBlockAccess(_KEY_AB, _KEY_AB, _KEY_AB, _KEY_AB),
    (0, 1, 0): DataBlockAccess(_KEY_AB, _NEVER, _NEVER, _NEVER),
    (1, 0, 0): DataBlockAccess(_KEY_AB, _KEY_B, _NEVER, _NEVER),
    (1, 1, 0): DataBlockAccess(_KEY_AB, _KEY_B, _KEY_B, _KEY_AB),
    (0, 0, 1): DataBlockAccess(_KEY_AB, _NEVER, _NEVER, _KEY_AB),
    (0, 1, 1): DataBlockAccess(_KEY_B, _KEY_B, _NEVER, _NEVER),
    (1, 0, 1): DataBlockAccess(_KEY_B, _NEVER, _NEVER, _NEVER),
    (1, 1, 1): DataBlockAccess(_NEVER, _NEVER, _NEVER, _NEVER),
}

# (C1, C2, C3) -> permissions, sector trailer (NXP MF1S50yyX, Table 8).
_TRAILER_TABLE = {
    (0, 0, 0): TrailerAccess(_KEY_A, _KEY_A, _NEVER, _KEY_A, _KEY_A),
    (0, 1, 0): TrailerAccess(_NEVER, _KEY_A, _NEVER, _KEY_A, _NEVER),
    (1, 0, 0): TrailerAccess(_KEY_B, _KEY_AB, _NEVER, _NEVER, _KEY_B),
    (1, 1, 0): TrailerAccess(_NEVER, _KEY_AB, _NEVER, _NEVER, _NEVER),
    (0, 0, 1): TrailerAccess(_KEY_A, _KEY_A, _KEY_A, _KEY_A, _KEY_A),
    (0, 1, 1): TrailerAccess(_KEY_B, _KEY_AB, _KEY_B, _NEVER, _KEY_B),
    (1, 0, 1): TrailerAccess(_NEVER, _KEY_AB, _KEY_B, _NEVER, _NEVER),
    (1, 1, 1): TrailerAccess(_NEVER, _KEY_AB, _NEVER, _NEVER, _NEVER),
}


def parse_access_bits(ac_hex: str) -> Optional[SectorAccessBits]:
    """Decode a sector trailer's 3 access-condition bytes (6 hex chars —
    bytes 6-8 of the trailer, right after the 2 key bytes' worth of user
    byte) into the read/write/increment/decrement permissions for the
    sector's 3 data blocks and its trailer.

    Returns None if `ac_hex` isn't exactly 6 hex chars - the caller's cue to
    skip printing a dissection rather than show a garbled one.
    """
    if len(ac_hex) != 6:
        return None
    try:
        b6, b7, b8 = bytes.fromhex(ac_hex)
    except ValueError:
        return None

    def bit(byte: int, n: int) -> int:
        return (byte >> n) & 1

    c1 = [bit(b7, 4 + n) for n in range(4)]
    c2 = [bit(b8, n) for n in range(4)]
    c3 = [bit(b8, 4 + n) for n in range(4)]
    c1_inv = [bit(b6, n) for n in range(4)]
    c2_inv = [bit(b6, 4 + n) for n in range(4)]
    c3_inv = [bit(b7, n) for n in range(4)]
    valid = all(
        c1[n] != c1_inv[n] and c2[n] != c2_inv[n] and c3[n] != c3_inv[n]
        for n in range(4)
    )

    blocks = [_DATA_BLOCK_TABLE[(c1[n], c2[n], c3[n])] for n in range(3)]
    trailer = _TRAILER_TABLE[(c1[3], c2[3], c3[3])]
    return SectorAccessBits(
        raw=ac_hex.upper(), valid=valid, blocks=blocks, trailer=trailer
    )
