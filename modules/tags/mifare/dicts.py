#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

# Named key dictionaries for `mifare check` — Fase 2 of PLAN_Mejoras_MIFARE.md.
#
# Optional, opt-in candidate sets (data/dicts/*.dic) layered on top of the
# bundled default dictionary. Selecting none leaves `check` byte-for-byte
# unchanged (the baseline the Fase 0 tests pin). Nothing here does cryptography:
# every key is a *published, known* value, and the families only change which
# known keys are tried and in what order — never how authentication works.
# The Crypto-1 wall stays out of reach by design (see the plan).

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List

from .keyfile import load_keys

_DICTS_DIR = Path(__file__).parent / "data" / "dicts"

# Priority tiers — lower tries first. Plan §Fase 2: "por-defecto → vendor
# específico → genérico". A sector-biased family still jumps ahead of these on
# its own sector (see CandidatePlan.for_sector).
TIER_DEFAULT = 0  # factory/transport keys: most cards, highest probability
TIER_APP = 1  # public application keys (MAD, NDEF)
TIER_VENDOR = 2  # vendor/product-specific sets (locks, ...)


@dataclass(frozen=True)
class NamedDict:
    """A registered `.dic` family under data/dicts/."""

    name: str
    filename: str
    tier: int
    description: str
    # Sectors these keys are *extra* likely to open; tried first there. Empty =
    # no bias (the family sits at its tier's priority on every sector).
    sectors: FrozenSet[int] = field(default_factory=frozenset)

    def path(self) -> Path:
        return _DICTS_DIR / self.filename

    def load(self) -> List[str]:
        """The family's keys (12-hex, uppercased, de-duplicated)."""
        return load_keys([str(self.path())])


# The registry. To add a family: drop `<name>.dic` in data/dicts/ and add one
# entry here (see data/dicts/README.md).
REGISTRY: Dict[str, NamedDict] = {
    d.name: d
    for d in (
        NamedDict(
            "transport",
            "transport.dic",
            TIER_DEFAULT,
            "Factory / transport default keys (FFFF…, A0A1…, 0000…).",
        ),
        NamedDict(
            "mad",
            "mad.dic",
            TIER_APP,
            "MIFARE Application Directory keys (NXP AN10787).",
            sectors=frozenset({0, 16}),
        ),
        NamedDict(
            "ndef",
            "ndef.dic",
            TIER_APP,
            "NFC Forum / NDEF public keys.",
        ),
        NamedDict(
            "locks",
            "locks.dic",
            TIER_VENDOR,
            "Access-control / lock vendor default keys.",
        ),
    )
}

# Registry insertion order → stable tiebreaker for equal tiers.
_ORDER: Dict[str, int] = {name: i for i, name in enumerate(REGISTRY)}


class UnknownDictError(ValueError):
    """A `--dict` name wasn't in the registry. Carries a message the CLI turns
    into a clean error + exit rather than a traceback."""


def select(names: Iterable[str]) -> List[NamedDict]:
    """Resolve `--dict` values to registered families, ordered by probability.

    NAMES are the raw `--dict` strings (repeatable, and each may be a
    comma- or space-separated list). ``all`` expands to every family. The result
    is de-duplicated and sorted by (tier, registry order) — the "por-defecto →
    vendor → genérico" order the queue is built in. An unknown name raises
    `UnknownDictError` (loud: a typo shouldn't silently skip a dictionary).
    """
    wanted: List[str] = []
    for raw in names:
        for token in raw.replace(",", " ").split():
            token = token.lower()
            if token == "all":
                wanted.extend(REGISTRY)
            elif token in REGISTRY:
                wanted.append(token)
            else:
                raise UnknownDictError(
                    f"unknown dictionary {token!r} — known: "
                    f"{', '.join(REGISTRY)} (or 'all')"
                )
    unique = list(dict.fromkeys(wanted))
    unique.sort(key=lambda n: (REGISTRY[n].tier, _ORDER[n]))
    return [REGISTRY[n] for n in unique]


class CandidatePlan:
    """Ordered dictionary candidates per sector.

    ``base`` is the current dictionary (bundled default, or the user's
    ``--keys`` files), kept at its existing order. ``named`` / ``extra`` are the
    Fase 2 additions, layered *ahead* of the base so their higher-probability
    keys (and any sector-biased family, e.g. MAD on sector 0) hit the per-sector
    early-cut first.

    With no named families and no extra files, ``for_sector`` returns ``base``
    unchanged for every sector — byte-identical to the pre-Fase-2 behaviour, so
    the pinned baseline auth counts don't move unless the user opts in.
    """

    def __init__(
        self,
        base: Iterable[str],
        named: Iterable[NamedDict] = (),
        extra: Iterable[str] = (),
    ):
        self.base: List[str] = [k.upper() for k in base]
        self._named: List[NamedDict] = list(named)
        self._extra: List[str] = [k.upper() for k in extra]
        self._named_keys: Dict[str, List[str]] = {
            nd.name: nd.load() for nd in self._named
        }
        # Full de-duplicated candidate universe. Order-independent in size, so
        # any sector's view has this many distinct keys — used for progress.
        self._universe: List[str] = self.for_sector(-1)

    @property
    def size(self) -> int:
        return len(self._universe)

    def __bool__(self) -> bool:
        return bool(self._universe)

    def for_sector(self, sector: int) -> List[str]:
        """The candidate keys to try on SECTOR, ordered by probability and
        de-duplicated (first occurrence wins)."""
        if not self._named and not self._extra:
            return self.base  # unchanged: exact pre-Fase-2 behaviour

        def rank(nd: NamedDict):
            hinted = bool(nd.sectors) and sector in nd.sectors
            return (0 if hinted else 1, nd.tier, _ORDER[nd.name])

        ordered: List[str] = []
        for nd in sorted(self._named, key=rank):
            ordered.extend(self._named_keys[nd.name])
        ordered.extend(self.base)
        ordered.extend(self._extra)
        return list(dict.fromkeys(ordered))
