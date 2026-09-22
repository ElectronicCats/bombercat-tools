#!/usr/bin/env python3

# Electronic Cats
# test_magspoof_track2.py — Track 2 parsing + Service Code normalization
# (modules/magspoof/track2.py). docs/IMPLEMENTATION_PLAN_AUTO_NORMALIZE_SC.md

from modules.magspoof.track2 import Track2Data, normalize_track2, parse_track2


def test_parse_valid_track2():
    t2 = ";4111111111111111=28032060000094600000?"
    parsed = parse_track2(t2)
    assert parsed is not None
    assert parsed.pan == "4111111111111111"
    assert parsed.expiration == "2803"
    assert parsed.service_code == "206"
    assert parsed.discretionary == "0000094600000"


def test_parse_invalid_track2():
    assert parse_track2("invalid") is None
    assert parse_track2(";1234=2803206?") is None  # PAN too short
    assert parse_track2("%B1234^TEST^2803?") is None  # Track 1 format


def test_normalize_ic_card_to_magstripe():
    # 206 -> 101 (IC card + PIN -> magstripe only, no PIN)
    assert (
        normalize_track2(";4111111111111111=28032060000094600000?")
        == ";4111111111111111=28031010000094600000?"
    )


def test_normalize_ic_card_fallback_kept():
    # 506 -> 501 (chip+fallback + PIN -> chip+fallback, no PIN)
    assert (
        normalize_track2(";4111111111111111=28035060000094600000?")
        == ";4111111111111111=28035010000094600000?"
    )


def test_normalize_no_change_when_already_correct():
    # 101 -> 101 (already correct)
    assert (
        normalize_track2(";4111111111111111=28031010000094600000?")
        == ";4111111111111111=28031010000094600000?"
    )


def test_normalize_national_pin_to_no_pin():
    # 226 -> 121 (national + PIN -> national, no PIN)
    assert (
        normalize_track2(";4111111111111111=28032260000094600000?")
        == ";4111111111111111=28031210000094600000?"
    )


def test_normalize_invalid_input_returns_none():
    assert normalize_track2("not a track") is None


def test_track2data_properties():
    parsed = parse_track2(";4111111111111111=28032060000094600000?")
    assert parsed.is_ic_card is True
    assert parsed.requires_pin is True
    assert parsed.normalized_service_code() == "101"


def test_to_track2_defaults_to_original_service_code():
    parsed = Track2Data(
        pan="4111111111111111", expiration="2612", service_code="201", discretionary=""
    )
    assert parsed.to_track2() == ";4111111111111111=2612201?"
    assert parsed.to_track2("101") == ";4111111111111111=2612101?"
