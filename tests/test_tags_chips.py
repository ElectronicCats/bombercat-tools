#!/usr/bin/env python3

# Electronic Cats
# test_tags_chips.py — the (ATQA, SAK) -> chip model table
# (modules/tags/chips.py) and the prose normalization feeding it.

import pytest

from modules.tags.chips import fingerprint, lookup, normalize_atqa, normalize_sak


# ── normalization ────────────────────────────────────────────────────────────


def test_atqa_is_byte_swapped_from_the_wire_order():
    """ISO14443-3 sends ATQA low byte first, so the PN7150's '0x44 0x00' is
    ATQA 0x0044 — the way proxmark writes it."""
    assert normalize_atqa("0x44 0x00") == "0044"
    assert normalize_atqa("0x04 0x00") == "0004"
    assert normalize_atqa("0x44 0x03") == "0344"


def test_atqa_accepts_unprefixed_and_unpadded_bytes():
    assert normalize_atqa("44 0") == "0044"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "0x50 0x11 0x22 0x33 0x44",  # NFC-B SENSB_RES shares the prose label
        "0x44",
        "0x44 0xZZ",
        "4400",  # not the format the sketch prints
    ],
)
def test_atqa_rejects_anything_that_is_not_exactly_two_hex_bytes(raw):
    assert normalize_atqa(raw) is None


def test_sak_is_a_single_byte():
    assert normalize_sak("0x08") == "08"
    assert normalize_sak("0xb8") == "B8"
    assert normalize_sak("0x00 0x08") is None
    assert normalize_sak(None) is None


# ── table lookup ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "atqa,sak,model",
    [
        ("0004", "08", "MIFARE Classic 1K"),
        ("0002", "18", "MIFARE Classic 4K"),
        ("0004", "09", "MIFARE Mini 0.3K"),
        ("0042", "18", "MIFARE Classic 4K (7-byte UID)"),
        ("0044", "00", "MIFARE Ultralight / NTAG"),
        ("0344", "20", "MIFARE DESFire / NTAG 424 DNA"),
        ("0004", "10", "MIFARE Plus 2K (SL2)"),
        ("0002", "11", "MIFARE Plus 4K (SL2)"),
    ],
)
def test_known_pairs_resolve(atqa, sak, model):
    assert lookup(atqa, sak) == model


def test_lookup_is_case_insensitive():
    assert lookup("0344", "20") == lookup("0344", "20")
    assert lookup("0004", "b8") == "Gemplus MPCOS"


def test_unknown_or_missing_pairs_resolve_to_none():
    assert lookup("BEEF", "99") is None
    assert lookup(None, "08") is None
    assert lookup("0004", None) is None


def test_table_is_keyed_uniquely():
    from modules.tags.chips import _CHIPS

    assert len(_CHIPS) == len(set(_CHIPS))


# ── fingerprint() ────────────────────────────────────────────────────────────


def test_fingerprint_returns_atqa_sak_and_model():
    assert fingerprint("0x04 0x00", "0x08") == {
        "atqa": "0004",
        "sak": "08",
        "model": "MIFARE Classic 1K",
    }


def test_fingerprint_keeps_atqa_sak_when_the_model_is_unknown():
    """An unmatched pair still reports what the card actually answered — only
    `model` is left out."""
    assert fingerprint("0xBE 0xEF", "0x99") == {"atqa": "EFBE", "sak": "99"}


@pytest.mark.parametrize(
    "sens,sel", [(None, None), ("0x04 0x00", None), (None, "0x08")]
)
def test_fingerprint_omits_what_it_cannot_derive(sens, sel):
    assert "model" not in fingerprint(sens, sel)
