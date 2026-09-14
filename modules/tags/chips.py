#!/usr/bin/env python3

# Electronic Cats
# chips.py — (ATQA, SAK) -> NFC chip model. The PN7150 already prints both
# values as prose ("SENS RES = 0x44 0x00" / "SEL RES = 0x00") before every
# detection; this turns that pair into a chip identifier the way `hf 14a info`
# (proxmark3) and libnfc/nfc-tools do, following NXP AN10833 "MIFARE type
# identification procedure".
#
# Scope and honesty about it: ATQA+SAK identify a *family*, not always a part
# number. Several chips are byte-identical at this layer and are only told
# apart by a command DetectTags never issues:
#   - Ultralight / Ultralight C / UL EV1 / NTAG203/21x all answer 0044/00;
#     GET_VERSION (0x60) or the UL-C 3DES AUTH is what separates them.
#   - DESFire D40 / EV1 / EV2 / EV3 and NTAG 424 DNA all answer 0344/20;
#     GetVersion (0x60) is what separates them.
#   - "Magic" / Gen1a / CUID / FUID clones and Fudan FM11RF08 deliberately
#     answer exactly like the genuine MIFARE Classic they impersonate; they
#     are found with the 0x40/0x43 backdoor probe, not with ATQA/SAK.
# Those entries therefore name the family. Anything unknown resolves to None
# rather than to a guess.
# Distributed as-is; no warranty is given.

import re
from typing import Dict, List, Optional, Tuple

_HEX_BYTE = re.compile(r"^[0-9A-Fa-f]{1,2}$")

# Key: (ATQA written MSB-first, the way proxmark prints it — "00 44" -> "0044",
#       SAK as one byte). Value: the chip model.
_CHIPS: Dict[Tuple[str, str], str] = {
    # ── MIFARE Classic ───────────────────────────────────────────────────────
    ("0004", "08"): "MIFARE Classic 1K",
    ("0044", "08"): "MIFARE Classic 1K (7-byte UID)",
    ("0004", "09"): "MIFARE Mini 0.3K",
    ("0004", "19"): "MIFARE Classic 2K",
    ("0002", "18"): "MIFARE Classic 4K",
    ("0042", "18"): "MIFARE Classic 4K (7-byte UID)",
    ("0004", "88"): "Infineon MIFARE Classic 1K",
    ("0004", "98"): "Gemplus MPCOS",
    ("0004", "B8"): "Gemplus MPCOS",
    # ── MIFARE Plus (SL1 is indistinguishable from Classic on purpose) ───────
    ("0004", "10"): "MIFARE Plus 2K (SL2)",
    ("0044", "10"): "MIFARE Plus 2K (SL2, 7-byte UID)",
    ("0002", "11"): "MIFARE Plus 4K (SL2)",
    ("0042", "11"): "MIFARE Plus 4K (SL2, 7-byte UID)",
    ("0004", "20"): "MIFARE Plus 2K (SL3)",
    ("0044", "20"): "MIFARE Plus 2K (SL3, 7-byte UID)",
    ("0002", "20"): "MIFARE Plus 4K (SL3)",
    ("0042", "20"): "MIFARE Plus 4K (SL3, 7-byte UID)",
    # ── MIFARE Ultralight / NTAG (see the family note above) ─────────────────
    ("0044", "00"): "MIFARE Ultralight / NTAG",
    # ── ISO14443-4 secure chips (see the family note above) ──────────────────
    ("0344", "20"): "MIFARE DESFire / NTAG 424 DNA",
    # ── SmartMX / JCOP: a secure element emulating a MIFARE Classic ──────────
    ("0004", "28"): "SmartMX with MIFARE Classic 1K emulation",
    ("0002", "38"): "Nokia 6212/6131 (MIFARE Classic 4K emulation)",
    ("0304", "28"): "JCOP 31/41 (MIFARE Classic 1K emulation)",
    ("0048", "20"): "JCOP 31 (ISO14443-4)",
}


def _hex_bytes(raw: Optional[str]) -> List[str]:
    """'0x44 0x00' -> ['44', '00'];  anything that isn't a clean list of hex
    bytes -> [] (the caller then reports no fingerprint rather than a wrong
    one)."""
    out = []
    for token in (raw or "").replace(",", " ").split():
        token = token[2:] if token[:2].lower() == "0x" else token
        if not _HEX_BYTE.match(token):
            return []
        out.append(token.upper().zfill(2))
    return out


def normalize_atqa(sens_res: Optional[str]) -> Optional[str]:
    """SENS_RES prose -> ATQA as it is conventionally written, or None.

    The PN7150 hands over the two SENS_RES bytes in the order they arrived,
    and ISO14443-3 sends ATQA low byte first — so the wire's "0x44 0x00" is
    ATQA 0x0044. Exactly two bytes are required, which also rules out the
    longer SENSB_RES (NFC-B) that shares this prose label.
    """
    parts = _hex_bytes(sens_res)
    if len(parts) != 2:
        return None
    return parts[1] + parts[0]


def normalize_sak(sel_res: Optional[str]) -> Optional[str]:
    """SEL_RES prose -> the single SAK byte, or None."""
    parts = _hex_bytes(sel_res)
    return parts[0] if len(parts) == 1 else None


def lookup(atqa: Optional[str], sak: Optional[str]) -> Optional[str]:
    """(ATQA, SAK) -> chip model, or None when the pair isn't in the table."""
    if not atqa or not sak:
        return None
    return _CHIPS.get((atqa.upper(), sak.upper()))


def fingerprint(sens_res: Optional[str], sel_res: Optional[str]) -> Dict[str, str]:
    """The `atqa`/`sak`/`model` keys to merge into `Tag.extra`.

    Each key is present only when it could actually be derived, so a tag whose
    ATQA/SAK never arrived — or whose pair isn't in the table — simply carries
    fewer keys instead of empty ones.
    """
    out: Dict[str, str] = {}
    atqa = normalize_atqa(sens_res)
    sak = normalize_sak(sel_res)
    if atqa:
        out["atqa"] = atqa
    if sak:
        out["sak"] = sak
    model = lookup(atqa, sak)
    if model:
        out["model"] = model
    return out
