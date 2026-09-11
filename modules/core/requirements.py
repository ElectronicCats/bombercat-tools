#!/usr/bin/env python3

# Electronic Cats
# requirements.py — capability -> canonical firmware provider mapping and the
# Requirement value object that ensure_firmware() (docs/AUTOFLASH_PLAN.md F3)
# consumes. Requiring a capability rather than a firmware id (D-3.1) lets a
# board that already qualifies — e.g. MagspoofCVSAttack for CAP_MAGSPOOF —
# skip a reflash even when its id doesn't match the canonical provider.
# Distributed as-is; no warranty is given.

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from .firmwares import (
    CAP_CAPTURE,
    CAP_CONFIG,
    CAP_MAGSPOOF,
    CAP_MIFARE,
    CAP_READERS,
    CAP_RELAY,
    CAP_TAGS,
    Firmware,
    by_id,
)

# capability -> id of the firmware ensure_firmware() flashes when nothing
# already on the board satisfies the capability. See docs/AUTOFLASH_PLAN.md
# D-3.1: CAP_MONITOR/CAP_IDENTIFY/CAP_PASSTHROUGH are deliberately absent —
# too generic to justify a reflash.
CAPABILITY_PROVIDER: Dict[str, str] = {
    CAP_RELAY: "nfcgate",
    CAP_CONFIG: "nfcgate",
    CAP_CAPTURE: "nfcgate",
    CAP_TAGS: "detecttags",
    CAP_READERS: "detectreaders",
    CAP_MIFARE: "mifareclassic",
    CAP_MAGSPOOF: "magspoof",
}


@dataclass(frozen=True)
class Requirement:
    """What a command needs to run, resolved to a concrete image to flash."""

    capability: str
    command: str  # e.g. "tags mifare" — used in messages to the user
    provider_id: str  # e.g. "mifareclassic"

    @property
    def provider(self) -> Firmware:
        return by_id(self.provider_id)

    @property
    def image_name(self) -> str:
        """The stem ReleaseCache.find() expects, e.g. 'MifareClassic'."""
        return self.provider.uf2.rsplit(".", 1)[0]


def requirement_for(capability: str, command: str) -> Requirement:
    """Resolve a capability to its Requirement.

    Raises ValueError if the capability has no canonical provider — asking
    for CAP_MONITOR/CAP_IDENTIFY/CAP_PASSTHROUGH here is a caller bug, so it
    must fail at import/test time, not as a runtime surprise for the user.
    """
    provider_id = CAPABILITY_PROVIDER.get(capability)
    if provider_id is None:
        raise ValueError(f"capability {capability!r} has no canonical provider")
    return Requirement(capability=capability, command=command, provider_id=provider_id)


def satisfied_by(fw: Firmware, capability: str) -> bool:
    return fw.can(capability)
