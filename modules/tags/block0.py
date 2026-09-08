#!/usr/bin/env python3

# Electronic Cats
# block0.py - MIFARE Classic block 0 dissection. Only the fixed layout used
# by 4-byte-UID cards applies: UID(4) BCC(1) SAK(1) ATQA(2) manufacturer
# data(8). 7-byte-UID cards lay block 0 out differently, so callers must not
# treat a parse failure there as an error - `parse_block0` just returns None.
# Distributed as-is; no warranty is given.

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

_SAK_NAMES = {
    0x08: "MIFARE Classic 1K",
    0x18: "MIFARE Classic 4K",
    0x09: "MIFARE Mini",
    0x28: "SmartMX Classic 1K",
    0x38: "SmartMX Classic 4K",
    # Non-NXP silicon (Infineon SLE66R35 and friends) sets the proprietary
    # b8 bit (0x80) on top of the standard Classic 1K SAK: 0x88 & 0x7F == 0x08.
    # NXP's AN10833 doesn't list it, but Proxmark3/libnfc report it as a
    # Classic 1K-compatible card; it authenticates with normal Crypto1.
    0x88: "MIFARE Classic 1K (Infineon)",
}

_ATQA_NAMES = {
    "0400": "MIFARE Classic 1K",
    "0200": "MIFARE Classic 4K",
}


@dataclass
class Block0:
    uid: str
    bcc: str
    bcc_valid: bool
    sak: str
    sak_name: str
    atqa: str
    atqa_name: str
    manufacturer_data: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "uid": self.uid,
            "bcc": self.bcc,
            "bcc_valid": self.bcc_valid,
            "sak": self.sak,
            "sak_name": self.sak_name,
            "atqa": self.atqa,
            "atqa_name": self.atqa_name,
            "manufacturer_data": self.manufacturer_data,
        }


def parse_block0(data_hex: str) -> Optional[Block0]:
    """Dissect a 16-byte (32 hex char) block 0. Returns None if `data_hex`
    isn't exactly one full block of hex - the caller's cue to skip printing
    a dissection rather than show a garbled one."""
    if len(data_hex) != 32:
        return None
    try:
        block = bytes.fromhex(data_hex)
    except ValueError:
        return None

    uid, bcc, sak, atqa, mfg = (
        block[0:4],
        block[4],
        block[5],
        block[6:8],
        block[8:16],
    )
    bcc_calc = uid[0] ^ uid[1] ^ uid[2] ^ uid[3]
    atqa_hex = atqa.hex().upper()

    return Block0(
        uid=uid.hex().upper(),
        bcc=f"{bcc:02X}",
        bcc_valid=bcc == bcc_calc,
        sak=f"{sak:02X}",
        sak_name=_SAK_NAMES.get(sak, "unknown/non-standard"),
        atqa=atqa_hex,
        atqa_name=_ATQA_NAMES.get(atqa_hex, "unknown"),
        manufacturer_data=mfg.hex().upper(),
    )
