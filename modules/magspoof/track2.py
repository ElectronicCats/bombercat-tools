#!/usr/bin/env python3

# Electronic Cats
# track2.py — ISO 7813 Track 2 parsing + Service Code normalization for
# magstripe fallback. Local-only (never touches the serial link) so
# `magspoof card normalize-sc` can preview the rewrite before anything
# round-trips to the device via `magcard set NAME 2 ...`.
# docs/IMPLEMENTATION_PLAN_AUTO_NORMALIZE_SC.md
# Distributed as-is; no warranty is given.

import re
from dataclasses import dataclass
from typing import Optional

# ;{PAN}={YYMM}{service code}{discretionary data}?
_TRACK2_RE = re.compile(
    r"^;(?P<pan>[0-9]{13,19})=(?P<exp>[0-9]{4})(?P<sc>[0-9]{3})(?P<disc>[0-9]*)\?$"
)


@dataclass
class Track2Data:
    """Parsed ISO 7813 Track 2 components."""

    pan: str
    expiration: str  # YYMM
    service_code: str  # 3 digits
    discretionary: str

    @property
    def is_ic_card(self) -> bool:
        """True if the Service Code's 1st digit (2 or 6) demands a chip."""
        return self.service_code[0] in ("2", "6")

    @property
    def requires_pin(self) -> bool:
        """True if the Service Code's 3rd digit (6) demands a PIN."""
        return self.service_code[2] == "6"

    def normalized_service_code(self) -> str:
        """Service Code rewritten for magstripe fallback + no PIN: 1st digit
        2/6 -> 1 (chip-required -> magstripe-only; 5 is left as-is, it
        already allows fallback), 3rd digit 6 -> 1 (PIN-required -> none)."""
        first, second, third = self.service_code
        if first in ("2", "6"):
            first = "1"
        if third == "6":
            third = "1"
        return f"{first}{second}{third}"

    def to_track2(self, service_code: Optional[str] = None) -> str:
        """Reconstruct the Track 2 string, optionally with a substitute
        Service Code (defaults to the one this card was parsed with)."""
        sc = service_code if service_code is not None else self.service_code
        return f";{self.pan}={self.expiration}{sc}{self.discretionary}?"


def parse_track2(track2: str) -> Optional[Track2Data]:
    """Parse an ISO 7813 Track 2 string. Returns None if it doesn't match
    the `;PAN=YYMMSCdisc?` shape (e.g. a Track 1 string, or malformed data)."""
    m = _TRACK2_RE.match(track2.strip())
    if not m:
        return None
    return Track2Data(
        pan=m["pan"], expiration=m["exp"], service_code=m["sc"], discretionary=m["disc"]
    )


def normalize_track2(track2: str) -> Optional[str]:
    """Parse TRACK2 and rewrite its Service Code for magstripe fallback (no
    chip, no PIN required). Returns None if TRACK2 isn't valid Track 2."""
    parsed = parse_track2(track2)
    if parsed is None:
        return None
    return parsed.to_track2(parsed.normalized_service_code())
