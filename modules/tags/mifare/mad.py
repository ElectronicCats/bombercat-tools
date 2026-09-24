#!/usr/bin/env python3

# Electronic Cats
# mad.py - MIFARE Application Directory (MAD) dissection. The MAD is a lookup
# table, stored in a card's own sectors, that says which application "owns"
# each sector: MAD1 lives in sector 0 (data blocks 1-2) and lists sectors
# 1-15; MAD2, on a 4K card, lives in sector 16 and lists the sectors above it.
# Each entry is a 16-bit Application ID (AID); 0x0000 means the sector is free.
# See NXP AN10787 ("MIFARE Application Directory"). Pure/offline: decodes bytes
# a dump already holds, never talks to a card.
# Distributed as-is; no warranty is given.

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

# One MAD directory block pair (block 1 + block 2 of the MAD sector) carries a
# 1-byte CRC, a 1-byte info/publisher byte and then 15 little-endian AIDs — the
# same 32-byte layout `tests/mifare_card.mad1_blocks` builds, and the layout
# MAD2's first two directory blocks reuse for the sectors just above the MAD2
# sector. The project's dump path only ever addresses 4-block sectors (see
# common._sector_first_block), so MAD2's optional third directory block — the
# one covering 4K's 16-block sectors 32-39 — is deliberately out of range here.
_MAD_AIDS_PER_BLOCKPAIR = 15
_MAD_BODY_LEN = 31  # info byte + 15 * 2 AID bytes
_AID_FREE = 0x0000


@dataclass
class MadEntry:
    sector: int
    aid: str  # 4 hex chars, big-endian display of the 16-bit AID
    allocated: bool  # False for AID 0x0000 (sector declared free)

    def to_dict(self) -> Dict[str, object]:
        return {"sector": self.sector, "aid": self.aid, "allocated": self.allocated}


@dataclass
class Mad:
    version: int  # 1 (sector 0) or 2 (sector 16)
    crc: str  # 2 hex chars, the stored CRC byte
    crc_valid: bool  # stored CRC vs. AN10787 CRC-8 over the 31 body bytes
    info: str  # 2 hex chars, the info/publisher byte
    entries: List[MadEntry]

    def to_dict(self) -> Dict[str, object]:
        return {
            "version": self.version,
            "crc": self.crc,
            "crc_valid": self.crc_valid,
            "info": self.info,
            "entries": [e.to_dict() for e in self.entries],
        }


def crc8_mad(data: bytes) -> int:
    """AN10787 MAD CRC-8: poly 0x1D, preset 0xC7, MSB-first, no final xor.

    The inverse of the encoder in `tests/mifare_card._crc8_mad`; kept here so
    shipped code validates the same way the fixtures were built.
    """
    crc = 0xC7
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def parse_mad(
    block1_hex: str, block2_hex: str, version: int = 1, first_sector: int = 1
) -> Optional[Mad]:
    """Decode a MAD directory from its two data blocks (each 16 bytes / 32 hex).

    `block1_hex` is the MAD sector's first data block — byte 0 is the CRC, byte 1
    the info/publisher byte, and bytes 2-15 the first AIDs; `block2_hex` holds
    the rest, 15 AIDs in all, mapping consecutive sectors from `first_sector`.
    `version` is 1 for the sector-0 MAD and 2 for the sector-16 MAD2 (it only
    labels the result).

    Returns None if either block isn't exactly one full block of hex — the
    caller's cue to report "no MAD" rather than a garbled one. A returned Mad
    with ``crc_valid=False`` still carries its decoded entries: the caller
    decides whether to trust them, but the honest signal is surfaced, never
    hidden.
    """
    if len(block1_hex) != 32 or len(block2_hex) != 32:
        return None
    try:
        raw = bytes.fromhex(block1_hex) + bytes.fromhex(block2_hex)
    except ValueError:
        return None

    crc_stored = raw[0]
    body = raw[1 : 1 + _MAD_BODY_LEN]
    crc_valid = crc8_mad(body) == crc_stored
    info = body[0]

    entries: List[MadEntry] = []
    for i in range(_MAD_AIDS_PER_BLOCKPAIR):
        off = 1 + i * 2
        aid = int.from_bytes(body[off : off + 2], "little")
        entries.append(
            MadEntry(
                sector=first_sector + i,
                aid=f"{aid:04X}",
                allocated=aid != _AID_FREE,
            )
        )

    return Mad(
        version=version,
        crc=f"{crc_stored:02X}",
        crc_valid=crc_valid,
        info=f"{info:02X}",
        entries=entries,
    )
