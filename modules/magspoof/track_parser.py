#!/usr/bin/env python3

# Electronic Cats
# track_parser.py — card-standard detection + enriched Service Code analysis
# for `magspoof show`. `_DETECTORS` is an ordered registry: a future standard
# is added by registering one more detector function here, not by rewriting
# `detect_track_standard` or the CLI. docs/IMPLEMENTATION_PLAN_SHOW_ENHANCED.md
#
# Only ISO 7813 (financial) and PBOC/UnionPay have a full field parse +
# Service Code analysis, because they share one well-documented wire format.
# AAMVA (driver's licenses) and the LOYALTY_GENERIC catch-all are detection
# only (no field decode): AAMVA's per-jurisdiction field layout isn't
# uniform across US states, and "loyalty" vs. "transit" cards have no
# registered IIN scheme to tell them apart from a raw magstripe string, so
# both are intentionally bucketed as LOYALTY_GENERIC rather than guessed.
# Distributed as-is; no warranty is given.

import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional

from .track2 import (
    Track2Data,
    normalize_service_code,
    parse_track2,
    service_code_requires_chip,
    service_code_requires_pin,
)


class TrackStandard(str, Enum):
    ISO_7813_FINANCIAL = "iso7813_financial"
    PBOC_UNIONPAY = "pboc_unionpay"
    AAMVA_DL = "aamva_dl"
    LOYALTY_GENERIC = "loyalty_generic"
    UNKNOWN = "unknown"


# ISO/IEC 7812 IIN range registered to China UnionPay. Payment PANs never
# start with 636 (that block is registered to AAMVA, see below), so this and
# the AAMVA check can't collide.
_UNIONPAY_PAN_PREFIX = "62"

# ISO/IEC 7812 IIN block registered to AAMVA for driver's license / ID cards
# (issuer 636026 and neighboring jurisdiction-specific IINs). Track 2 for
# these encodes as ";636...=...?", structurally identical to a financial
# track but never a real payment PAN.
_AAMVA_IIN_PREFIX = "636"


# %B{PAN}^{NAME}^{YYMM}{service code}{discretionary}?  (IATA / ISO 7813 Track 1)
_TRACK1_FINANCIAL_RE = re.compile(
    r"^%B(?P<pan>[0-9]{13,19})\^(?P<name>[^^]{2,26})\^"
    r"(?P<exp>[0-9]{4})(?P<sc>[0-9]{3})(?P<disc>[0-9]*)\?$"
)


@dataclass
class Track1Data:
    """Parsed IATA (ISO 7813 Track 1) financial components."""

    pan: str
    name: str
    expiration: str  # YYMM
    service_code: str  # 3 digits
    discretionary: str


def parse_track1_financial(track1: str) -> Optional[Track1Data]:
    """Parse an IATA-format financial Track 1. Returns None if TRACK1 doesn't
    match the `%B PAN ^ NAME ^ YYMMSCdisc ?` shape."""
    m = _TRACK1_FINANCIAL_RE.match(track1.strip())
    if not m:
        return None
    return Track1Data(
        pan=m["pan"],
        name=m["name"].strip(),
        expiration=m["exp"],
        service_code=m["sc"],
        discretionary=m["disc"],
    )


# ── standard detection ──────────────────────────────────────────────────────

# (track_str, track_num) -> the standard it matches, or None. Ordered list,
# first match wins.
_Detector = Callable[[str, int], Optional[TrackStandard]]
_DETECTORS: List[_Detector] = []


def register_detector(fn: _Detector) -> _Detector:
    _DETECTORS.append(fn)
    return fn


@register_detector
def _detect_aamva(track: str, track_num: int) -> Optional[TrackStandard]:
    """AAMVA driver's license / ID card. Detection only — field layout
    (name, DOB, address...) varies per US jurisdiction, so we don't attempt
    to decode it, only to recognize it so it isn't mistaken for a payment
    card or left as UNKNOWN."""
    if track_num == 1:
        # Financial Track 1 always starts with '%B'; AAMVA never does. The
        # '^' field separator (jurisdiction ^ name ^ ...) rules out a bare
        # numeric loyalty/transit track.
        if track.startswith("%") and not track.startswith("%B") and "^" in track:
            return TrackStandard.AAMVA_DL
        return None
    if (
        track.startswith(";" + _AAMVA_IIN_PREFIX)
        and "=" in track
        and track.endswith("?")
    ):
        return TrackStandard.AAMVA_DL
    return None


@register_detector
def _detect_iso7813_financial(track: str, track_num: int) -> Optional[TrackStandard]:
    if track_num == 1:
        parsed = parse_track1_financial(track)
    else:
        parsed = parse_track2(track)
    if parsed is None:
        return None
    if parsed.pan.startswith(_UNIONPAY_PAN_PREFIX):
        return TrackStandard.PBOC_UNIONPAY
    return TrackStandard.ISO_7813_FINANCIAL


@register_detector
def _detect_loyalty_generic(track: str, track_num: int) -> Optional[TrackStandard]:
    """Catch-all for a well-formed but non-financial, non-AAMVA track (store
    loyalty cards, gift cards, transit passes, ...). These have no
    registered IIN scheme, so we can't reliably tell "loyalty" from
    "transit" apart from a raw magstripe string — both land here rather
    than being guessed."""
    if track_num == 1 and track.startswith("%") and track.endswith("?"):
        return TrackStandard.LOYALTY_GENERIC
    if track_num == 2 and track.startswith(";") and track.endswith("?"):
        return TrackStandard.LOYALTY_GENERIC
    return None


def detect_track_standard(track: str, track_num: int) -> TrackStandard:
    """Detect which card standard TRACK_NUM (1 or 2) belongs to."""
    track = track.strip()
    if not track:
        return TrackStandard.UNKNOWN
    for detector in _DETECTORS:
        result = detector(track, track_num)
        if result is not None:
            return result
    return TrackStandard.UNKNOWN


# ── Service Code analysis ───────────────────────────────────────────────────


@dataclass
class ServiceCodeAnalysis:
    """Decoded meaning of a 3-digit ISO 7813 Service Code."""

    original: str
    normalized: str
    status: str  # OK_FALLBACK | REQUIRES_CHIP | REQUIRES_PIN | REQUIRES_CHIP_AND_PIN | UNKNOWN
    message: str
    requires_chip: bool
    requires_pin: bool
    allows_fallback: bool


def analyze_service_code(sc: str) -> Optional[ServiceCodeAnalysis]:
    """Analyze a 3-digit Service Code. Returns None if SC isn't 3 digits."""
    if len(sc) != 3 or not sc.isdigit():
        return None

    first = sc[0]
    requires_chip = service_code_requires_chip(sc)
    requires_pin = service_code_requires_pin(sc)
    allows_fallback = first in ("1", "5")  # 1=magstripe only, 5=chip+fallback

    if requires_chip and requires_pin:
        status = "REQUIRES_CHIP_AND_PIN"
        message = f"requires a chip (digit 1={first}) and a PIN (digit 3=6)"
    elif requires_chip:
        status = "REQUIRES_CHIP"
        message = f"requires a chip (digit 1={first})"
    elif requires_pin:
        status = "REQUIRES_PIN"
        message = "requires a PIN (digit 3=6)"
    elif allows_fallback:
        status = "OK_FALLBACK"
        message = f"allows magstripe fallback (digit 1={first})"
    else:
        status = "UNKNOWN"
        message = f"non-standard service code: {sc}"

    return ServiceCodeAnalysis(
        original=sc,
        normalized=normalize_service_code(sc),
        status=status,
        message=message,
        requires_chip=requires_chip,
        requires_pin=requires_pin,
        allows_fallback=allows_fallback,
    )


# ── card-level analysis ──────────────────────────────────────────────────────


@dataclass
class TrackAnalysis:
    track_num: int
    standard: TrackStandard
    parsed: Optional[object]  # Track1Data / Track2Data, or None if unparsed
    service_code_analysis: Optional[ServiceCodeAnalysis]


@dataclass
class CardAnalysis:
    track1: Optional[TrackAnalysis]
    track2: Optional[TrackAnalysis]
    primary_standard: TrackStandard
    is_financial: bool
    service_code_status: Optional[str]
    recommendations: List[str]


# PBOC/UnionPay magstripe cards follow the exact same ISO 7813 wire format
# as other financial cards, so they get the same field parse + Service Code
# analysis — only the reported `standard` differs.
_FINANCIAL_STANDARDS = frozenset(
    (TrackStandard.ISO_7813_FINANCIAL, TrackStandard.PBOC_UNIONPAY)
)


def _analyze_track(track: str, track_num: int) -> TrackAnalysis:
    standard = detect_track_standard(track, track_num)
    parsed: Optional[object] = None
    if standard in _FINANCIAL_STANDARDS:
        parsed = (
            parse_track1_financial(track) if track_num == 1 else parse_track2(track)
        )
    sc_analysis = analyze_service_code(parsed.service_code) if parsed else None
    return TrackAnalysis(track_num, standard, parsed, sc_analysis)


def analyze_card(t1: str, t2: str) -> CardAnalysis:
    """Analyze the active card's tracks: detect the standard and, for
    ISO 7813 / PBOC financial cards, decode the Service Code
    (chip/PIN/fallback)."""
    t1_analysis = _analyze_track(t1, 1) if t1 else None
    t2_analysis = _analyze_track(t2, 2) if t2 else None

    primary_standard = TrackStandard.UNKNOWN
    if t2_analysis and t2_analysis.standard != TrackStandard.UNKNOWN:
        primary_standard = t2_analysis.standard
    elif t1_analysis and t1_analysis.standard != TrackStandard.UNKNOWN:
        primary_standard = t1_analysis.standard

    is_financial = primary_standard in _FINANCIAL_STANDARDS

    # Track 2 is authoritative (it's the only track `card normalize-sc`
    # rewrites), fall back to Track 1's if Track 2 has none.
    sc_status = None
    if t2_analysis and t2_analysis.service_code_analysis:
        sc_status = t2_analysis.service_code_analysis.status
    elif t1_analysis and t1_analysis.service_code_analysis:
        sc_status = t1_analysis.service_code_analysis.status

    recommendations: List[str] = []
    if t2_analysis and t2_analysis.service_code_analysis:
        sca = t2_analysis.service_code_analysis
        if sca.requires_chip or sca.requires_pin:
            recommendations.append("bombercat magspoof card normalize-sc --apply")

    return CardAnalysis(
        track1=t1_analysis,
        track2=t2_analysis,
        primary_standard=primary_standard,
        is_financial=is_financial,
        service_code_status=sc_status,
        recommendations=recommendations,
    )


# ── JSON serialization ───────────────────────────────────────────────────────


def _service_code_analysis_to_dict(sca: ServiceCodeAnalysis) -> dict:
    return {
        "original": sca.original,
        "normalized": sca.normalized,
        "status": sca.status,
        "message": sca.message,
        "requires_chip": sca.requires_chip,
        "requires_pin": sca.requires_pin,
        "allows_fallback": sca.allows_fallback,
    }


def _track_analysis_to_dict(ta: TrackAnalysis) -> dict:
    parsed: Optional[dict] = None
    if isinstance(ta.parsed, Track1Data):
        parsed = {
            "pan": ta.parsed.pan,
            "name": ta.parsed.name,
            "expiration": ta.parsed.expiration,
            "service_code": ta.parsed.service_code,
            "discretionary": ta.parsed.discretionary,
        }
    elif isinstance(ta.parsed, Track2Data):
        parsed = {
            "pan": ta.parsed.pan,
            "expiration": ta.parsed.expiration,
            "service_code": ta.parsed.service_code,
            "discretionary": ta.parsed.discretionary,
        }
    return {
        "standard": ta.standard.value,
        "parsed": parsed,
        "service_code_analysis": (
            _service_code_analysis_to_dict(ta.service_code_analysis)
            if ta.service_code_analysis
            else None
        ),
    }


def card_analysis_to_dict(analysis: CardAnalysis) -> dict:
    return {
        "primary_standard": analysis.primary_standard.value,
        "is_financial": analysis.is_financial,
        "service_code_status": analysis.service_code_status,
        "track1": (
            _track_analysis_to_dict(analysis.track1) if analysis.track1 else None
        ),
        "track2": (
            _track_analysis_to_dict(analysis.track2) if analysis.track2 else None
        ),
        "recommendations": analysis.recommendations,
    }
