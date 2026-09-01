#!/usr/bin/env python3

# Electronic Cats
# test_magspoof_track_parser.py — standard detection + Service Code analysis
# (modules/magspoof/track_parser.py). docs/IMPLEMENTATION_PLAN_SHOW_ENHANCED.md

from modules.magspoof.track_parser import (
    TrackStandard,
    analyze_card,
    analyze_service_code,
    card_analysis_to_dict,
    detect_track_standard,
    parse_track1_financial,
)

TRACK1 = "%B4111111111111111^TEST/CARD^261200000000000?"
TRACK2_CHIP_PIN = ";4111111111111111=28032060000094600000?"  # sc=206
TRACK2_FALLBACK = ";4111111111111111=28031010000094600000?"  # sc=101

TRACK2_UNIONPAY = ";6225881234567890=28032010000000000?"
TRACK1_AAMVA = "%CA12345678^DOE^LASTNAME^FIRSTNAME^M^?"
TRACK2_AAMVA = ";6360260123456789=2512201?"
TRACK1_LOYALTY = "%1234567890123456?"
TRACK2_LOYALTY = ";9991234567890123?"


def test_detect_iso7813_financial_track1():
    std = detect_track_standard(TRACK1, 1)
    assert std == TrackStandard.ISO_7813_FINANCIAL


def test_detect_iso7813_financial_track2():
    std = detect_track_standard(TRACK2_CHIP_PIN, 2)
    assert std == TrackStandard.ISO_7813_FINANCIAL


def test_detect_unknown_for_garbage():
    assert detect_track_standard("garbage", 2) == TrackStandard.UNKNOWN
    assert detect_track_standard("", 2) == TrackStandard.UNKNOWN


def test_detect_pboc_unionpay_track2():
    std = detect_track_standard(TRACK2_UNIONPAY, 2)
    assert std == TrackStandard.PBOC_UNIONPAY


def test_pboc_unionpay_gets_service_code_analysis():
    analysis = analyze_card("", TRACK2_UNIONPAY)
    assert analysis.primary_standard == TrackStandard.PBOC_UNIONPAY
    assert analysis.is_financial is True
    assert analysis.track2.service_code_analysis is not None
    assert analysis.track2.service_code_analysis.original == "201"


def test_detect_aamva_track1():
    std = detect_track_standard(TRACK1_AAMVA, 1)
    assert std == TrackStandard.AAMVA_DL


def test_detect_aamva_track2_by_iin():
    std = detect_track_standard(TRACK2_AAMVA, 2)
    assert std == TrackStandard.AAMVA_DL


def test_aamva_track2_is_not_treated_as_financial():
    # TRACK2_AAMVA structurally matches the ISO 7813 Track 2 shape too
    # (digits '=' digits '?'), so the AAMVA/636-IIN check must win before
    # the financial detector ever sees it — otherwise a DL gets analyzed
    # (and could get "recommended" a Service Code normalization) as if it
    # were a payment card.
    analysis = analyze_card(TRACK1_AAMVA, TRACK2_AAMVA)
    assert analysis.primary_standard == TrackStandard.AAMVA_DL
    assert analysis.is_financial is False
    assert analysis.track2.parsed is None
    assert analysis.track2.service_code_analysis is None
    assert analysis.recommendations == []


def test_detect_loyalty_generic_track1():
    assert detect_track_standard(TRACK1_LOYALTY, 1) == TrackStandard.LOYALTY_GENERIC


def test_detect_loyalty_generic_track2():
    assert detect_track_standard(TRACK2_LOYALTY, 2) == TrackStandard.LOYALTY_GENERIC


def test_loyalty_generic_card_has_no_service_code_analysis():
    analysis = analyze_card(TRACK1_LOYALTY, TRACK2_LOYALTY)
    assert analysis.primary_standard == TrackStandard.LOYALTY_GENERIC
    assert analysis.is_financial is False
    assert analysis.service_code_status is None
    assert analysis.recommendations == []


def test_parse_track1_financial():
    parsed = parse_track1_financial(TRACK1)
    assert parsed is not None
    assert parsed.pan == "4111111111111111"
    assert parsed.name == "TEST/CARD"
    assert parsed.expiration == "2612"
    assert parsed.service_code == "000"


def test_parse_track1_financial_rejects_track2():
    assert parse_track1_financial(TRACK2_CHIP_PIN) is None


def test_analyze_service_code_requires_chip_and_pin():
    result = analyze_service_code("206")
    assert result.status == "REQUIRES_CHIP_AND_PIN"
    assert result.requires_chip is True
    assert result.requires_pin is True
    assert result.normalized == "101"


def test_analyze_service_code_ok_fallback():
    result = analyze_service_code("101")
    assert result.status == "OK_FALLBACK"
    assert result.normalized == "101"


def test_analyze_service_code_requires_pin_only():
    result = analyze_service_code("106")
    assert result.status == "REQUIRES_PIN"
    assert result.normalized == "101"


def test_analyze_service_code_invalid_input():
    assert analyze_service_code("12") is None
    assert analyze_service_code("abc") is None


def test_analyze_card_financial_with_bad_service_code():
    analysis = analyze_card(TRACK1, TRACK2_CHIP_PIN)
    assert analysis.is_financial is True
    assert analysis.primary_standard == TrackStandard.ISO_7813_FINANCIAL
    assert analysis.service_code_status == "REQUIRES_CHIP_AND_PIN"
    assert len(analysis.recommendations) == 1
    assert "normalize-sc" in analysis.recommendations[0]


def test_analyze_card_financial_already_normalized_has_no_recommendation():
    analysis = analyze_card(TRACK1, TRACK2_FALLBACK)
    assert analysis.service_code_status == "OK_FALLBACK"
    assert analysis.recommendations == []


def test_analyze_card_with_no_tracks():
    analysis = analyze_card("", "")
    assert analysis.track1 is None
    assert analysis.track2 is None
    assert analysis.primary_standard == TrackStandard.UNKNOWN
    assert analysis.is_financial is False
    assert analysis.service_code_status is None
    assert analysis.recommendations == []


def test_card_analysis_to_dict_shape():
    analysis = analyze_card(TRACK1, TRACK2_CHIP_PIN)
    d = card_analysis_to_dict(analysis)
    assert d["primary_standard"] == "iso7813_financial"
    assert d["is_financial"] is True
    assert d["service_code_status"] == "REQUIRES_CHIP_AND_PIN"
    assert d["track1"]["parsed"]["pan"] == "4111111111111111"
    assert d["track2"]["parsed"]["pan"] == "4111111111111111"
    assert d["track2"]["service_code_analysis"]["status"] == "REQUIRES_CHIP_AND_PIN"
    assert "normalize-sc" in d["recommendations"][0]
