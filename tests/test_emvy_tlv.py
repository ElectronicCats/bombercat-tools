#!/usr/bin/env python3

# Electronic Cats
# test_emvy_tlv.py — the minimal BER-TLV/DOL helpers probe-card builds its GPO
# with (modules/emvy/tlv.py): walking constructed templates to a tag, and
# expanding a PDOL into (tag, length) pairs. Pure functions, no serial.

import pytest

from modules.emvy.tlv import parse_dol, tlv_find


def test_tlv_find_returns_the_value_of_a_top_level_tag():
    data = bytes.fromhex("82021C00")
    assert tlv_find(data, 0x82) == bytes.fromhex("1C00")


def test_tlv_find_recurses_into_constructed_templates():
    # 6F { 84 .., A5 { 9F38 { PDOL } } } — the shape of a SELECT-AID FCI.
    fci = bytes.fromhex(
        "6F1E8407A0000000031010A5139F38109F02069F1A0295055F2A029C019F3501"
    )
    assert tlv_find(fci, 0x4F) is None  # not in this template
    assert tlv_find(fci, 0x9F38) == bytes.fromhex("9F02069F1A0295055F2A029C019F3501")


def test_tlv_find_recurses_through_bf0c_into_a_61_entry():
    ppse = bytes.fromhex(
        "6F20840E325041592E5359532E4444463031A50EBF0C0B61094F07A0000000031010"
    )
    assert tlv_find(ppse, 0x4F) == bytes.fromhex("A0000000031010")


def test_tlv_find_returns_none_when_absent():
    assert tlv_find(bytes.fromhex("82021C00"), 0x94) is None


@pytest.mark.parametrize("data", ["", "82", "8205AA", "9F", "9F38", "1F", "9F38FF00"])
def test_tlv_find_survives_truncated_blobs(data):
    assert tlv_find(bytes.fromhex(data), 0x9F38) is None


def test_parse_dol_expands_tag_length_pairs():
    dol = bytes.fromhex("9F02069F1A0295055F2A029C019F3501")
    assert parse_dol(dol) == [
        (0x9F02, 6),
        (0x9F1A, 2),
        (0x95, 5),
        (0x5F2A, 2),
        (0x9C, 1),
        (0x9F35, 1),
    ]


def test_parse_dol_handles_one_byte_tags():
    assert parse_dol(bytes.fromhex("95059A039C01")) == [(0x95, 5), (0x9A, 3), (0x9C, 1)]


def test_parse_dol_ignores_a_trailing_partial_tag():
    assert parse_dol(bytes.fromhex("9F")) == []
