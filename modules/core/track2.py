#!/usr/bin/env python3

# Electronic Cats
# track2.py — ISO 7813 Track 2 parsing + Service Code normalization for
# magstripe fallback. Firmware-agnostic core: pure and local-only (never
# touches a serial link), so any module can decode a track without pulling in
# device I/O — e.g. `magspoof card normalize-sc` previews the rewrite before it
# round-trips via `magcard set NAME 2 ...`, and `emvy read` decodes a scanned
# track2. Lives in core/ (not magspoof/) precisely so both groups can share it.
# docs/IMPLEMENTATION_PLAN_AUTO_NORMALIZE_SC.md
# Distributed as-is; no warranty is given.

import re
from dataclasses import dataclass
from typing import Optional

# ;{PAN}={YYMM}{service code}{discretionary data}?
_TRACK2_RE = re.compile(
    r"^;(?P<pan>[0-9]{13,19})=(?P<exp>[0-9]{4})(?P<sc>[0-9]{3})(?P<disc>[0-9]*)\?$"
)


def service_code_requires_chip(sc: str) -> bool:
    """True if a 3-digit Service Code's 1st digit (2 or 6) demands a chip.
    Shared with track_parser.py so both Track 1 and Track 2 analysis apply
    the same ISO 7813 rule."""
    return sc[0] in ("2", "6")


def service_code_requires_pin(sc: str) -> bool:
    """True if a 3-digit Service Code's 3rd digit (6) demands a PIN."""
    return sc[2] == "6"


def normalize_service_code(
    sc: str, *, remove_chip: bool = True, remove_pin: bool = True
) -> str:
    """Rewrite a 3-digit Service Code for magstripe fallback + no PIN: 1st
    digit 2/6 -> 1 (chip-required -> magstripe-only; 5 is left as-is, it
    already allows fallback), 3rd digit 6 -> 1 (PIN-required -> none).

    Args:
        sc: The 3-digit service code
        remove_chip: If True, change first digit 2/6 -> 1 (default True)
        remove_pin: If True, change third digit 6 -> 1 (default True)
    """
    first, second, third = sc
    if remove_chip and first in ("2", "6"):
        first = "1"
    if remove_pin and third == "6":
        third = "1"
    return f"{first}{second}{third}"


def harden_service_code(
    sc: str, *, require_chip: bool = True, require_pin: bool = True
) -> str:
    """Rewrite a 3-digit Service Code to demand a chip and/or a PIN — the
    inverse of `normalize_service_code`. 1st digit 1 -> 2 / 5 -> 6
    (international/national interchange preserved, chip now required; 2 and 6
    are already chip-required and left as-is), 3rd digit -> 6 (PIN required).

    Args:
        sc: The 3-digit service code
        require_chip: If True, change first digit 1 -> 2 / 5 -> 6 (default True)
        require_pin: If True, change third digit to 6 (default True)
    """
    first, second, third = sc
    if require_chip:
        if first == "1":
            first = "2"
        elif first == "5":
            first = "6"
    if require_pin and third != "6":
        third = "6"
    return f"{first}{second}{third}"


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
        return service_code_requires_chip(self.service_code)

    @property
    def requires_pin(self) -> bool:
        """True if the Service Code's 3rd digit (6) demands a PIN."""
        return service_code_requires_pin(self.service_code)

    def normalized_service_code(
        self, *, remove_chip: bool = True, remove_pin: bool = True
    ) -> str:
        """Service Code rewritten for magstripe fallback + no PIN (see
        `normalize_service_code`)."""
        return normalize_service_code(
            self.service_code, remove_chip=remove_chip, remove_pin=remove_pin
        )

    def hardened_service_code(
        self, *, require_chip: bool = True, require_pin: bool = True
    ) -> str:
        """Service Code rewritten to demand a chip and/or a PIN (see
        `harden_service_code`)."""
        return harden_service_code(
            self.service_code, require_chip=require_chip, require_pin=require_pin
        )

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


# EMV Track 2 Equivalent Data (tag 57), the shape a chip read yields — e.g.
# `emvy read` renders tag 57 compact (EMVyBomberCat.ino, `HexUtils::toCompact`
# into card.track2). It packs the same PAN/expiry/SC/discretionary as an ISO
# magstripe track but as BCD nibbles: 'D' is the field separator (in place of
# the magstripe '='), there are no '%'/';'/'?' sentinels, and a trailing 'F'
# nibble pads the last byte. (EMV Book 3, Annex B.)
_TRACK2_EQUIV_RE = re.compile(
    r"^(?P<pan>[0-9]{13,19})D(?P<exp>[0-9]{4})(?P<sc>[0-9]{3})(?P<disc>[0-9F]*)$"
)


def parse_track2_equivalent(track2: str) -> Optional[Track2Data]:
    """Parse an EMV Track 2 Equivalent (tag 57) compact-hex string
    (`PAN 'D' YYMM SC disc ['F'...]`) into the same `Track2Data` an ISO track
    parses to. Trailing 'F' nibble padding is stripped off the discretionary
    data. Returns None if it doesn't match that shape."""
    m = _TRACK2_EQUIV_RE.match(track2.strip().upper())
    if not m:
        return None
    return Track2Data(
        pan=m["pan"],
        expiration=m["exp"],
        service_code=m["sc"],
        discretionary=m["disc"].rstrip("F"),
    )


def to_iso_track2(track2: str) -> Optional[str]:
    """Coerce a Track 2 string to the ISO 7813 magstripe form (`;PAN=YYMMSC…?`),
    accepting either that form already or the EMV tag-57 equivalent (`…D…`) a
    chip read yields. Returns None if neither parses — letting a caller decode a
    scanned track without caring which of the two shapes the firmware handed it."""
    parsed = parse_track2(track2)
    if parsed is None:
        parsed = parse_track2_equivalent(track2)
    return parsed.to_track2() if parsed else None


def normalize_track2(
    track2: str, *, remove_chip: bool = True, remove_pin: bool = True
) -> Optional[str]:
    """Parse TRACK2 and rewrite its Service Code for magstripe fallback (no
    chip, no PIN required). Returns None if TRACK2 isn't valid Track 2.

    Args:
        track2: The track 2 string
        remove_chip: If True, remove chip requirement (default True)
        remove_pin: If True, remove PIN requirement (default True)
    """
    parsed = parse_track2(track2)
    if parsed is None:
        return None
    return parsed.to_track2(
        parsed.normalized_service_code(remove_chip=remove_chip, remove_pin=remove_pin)
    )
