#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

# UID-derived key candidate generators for `mifare check` — Fase 3 of
# PLAN_Mejoras_MIFARE.md.
#
# Some low-security MIFARE Classic deployments derive their sector keys from the
# card UID with a *published* algorithm. `block0` already exposes the UID, so we
# can recompute those exact candidate keys on the host and feed them into the
# same `check` queue. This is NOT cryptography and NOT key recovery: every key a
# scheme yields is a value the public, documented algorithm fully specifies for a
# given UID — we recompute known values, we never guess, brute-force or deduce.
#
# HARD BOUNDARY (plan §Fase 3 guardrail). A scheme whose keys come from a
# *secret* master key (HMAC/AES diversification) is NOT generable here — that is
# Crypto out of scope, the same wall as the nested/darkside attacks. Such a
# scheme is registered only so the CLI can name it and refuse: selecting it (or
# calling generate()) raises `SecretKeyRequiredError`. Nothing in this module
# attempts to recover, brute-force, or guess a secret master key.

from __future__ import annotations

from typing import Dict, FrozenSet, Iterable, List

_KEY_HEX_LEN = 12
_UID4_HEX_LEN = 8  # 4-byte UID, as parse_block0 yields it (uppercased hex)


class UnknownUidSchemeError(ValueError):
    """A `--uid-derived` name wasn't registered. Carries a message the CLI turns
    into a clean error + exit rather than a traceback."""


class UidSchemeError(ValueError):
    """A scheme couldn't produce candidates for a reason worth reporting cleanly
    (e.g. a UID length the scheme doesn't cover)."""


class SecretKeyRequiredError(UidSchemeError):
    """The requested scheme diversifies keys from a SECRET master key
    (HMAC/AES). Reproducing its keys would require knowing or breaking that
    secret — cryptography out of scope for this tool, exactly like the nested
    attack. The generator refuses rather than pretend it can."""


def _uid_bytes(uid: str, expect_len: int = _UID4_HEX_LEN) -> List[int]:
    """UID hex string → list of byte values, or `UidSchemeError` if it isn't
    exactly `expect_len` hex chars. Schemes call this so a 7-byte UID (or a
    missing one) is skipped cleanly instead of producing bogus keys."""
    s = uid.strip().upper()
    if len(s) != expect_len or any(c not in "0123456789ABCDEF" for c in s):
        raise UidSchemeError(f"scheme needs a {expect_len // 2}-byte UID (got {uid!r})")
    return [int(s[i : i + 2], 16) for i in range(0, len(s), 2)]


class MizipScheme:
    """MiZip / MIFARE Mini public UID key diversification.

    Verbatim port of proxmark3's ``client/luascripts/hf_mf_uidkeycalc_mizip.lua``
    (GPLv3): a fully published algorithm with fixed per-sector XOR tables and no
    secret input, so the keys are recomputed, never guessed. 4-byte UID only.
    Sector 0 uses the public MiZip MAD keys (no UID involved)."""

    name = "mizip"
    description = "MiZip / MIFARE Mini public UID diversification (proxmark3, GPLv3)."
    requires_secret = False
    sectors: FrozenSet[int] = frozenset({0, 1, 2, 3, 4})

    # Per-sector (key A xor, key B xor) tables — bytes as published.
    _XOR: Dict[int, tuple] = {
        1: ("09125A2589E5", "F12C8453D821"),
        2: ("AB75C937922F", "73E799FE3241"),
        3: ("E27241AF2C09", "AA4D137656AE"),
        4: ("317AB72F4490", "B01327272DFD"),
    }
    # Sector 0 is the public MiZip MAD sector: static keys, no diversification.
    _SECTOR0 = ("A0A1A2A3A4A5", "B4C132439EEF")

    def generate(self, uid: str, sector: int) -> List[str]:
        """Candidate keys (key A then key B) this scheme yields for SECTOR of a
        card with UID. Empty for sectors the scheme doesn't cover; raises
        `UidSchemeError` if the UID isn't the 4 bytes the scheme needs."""
        if sector == 0:
            return list(self._SECTOR0)
        if sector not in self._XOR:
            return []
        u = _uid_bytes(uid)  # raises on a non-4-byte UID -> caller skips scheme
        xa = bytes.fromhex(self._XOR[sector][0])
        xb = bytes.fromhex(self._XOR[sector][1])
        # proxmark calckey(): key A mixes UID bytes [1,2,3,4,1,2], key B mixes
        # [3,4,1,2,3,4] (1-indexed in the Lua source) with the XOR table.
        key_a = bytes(
            (
                u[0] ^ xa[0],
                u[1] ^ xa[1],
                u[2] ^ xa[2],
                u[3] ^ xa[3],
                u[0] ^ xa[4],
                u[1] ^ xa[5],
            )
        )
        key_b = bytes(
            (
                u[2] ^ xb[0],
                u[3] ^ xb[1],
                u[0] ^ xb[2],
                u[1] ^ xb[3],
                u[2] ^ xb[4],
                u[3] ^ xb[5],
            )
        )
        return [key_a.hex().upper(), key_b.hex().upper()]


class SecretDiversifiedScheme:
    """Placeholder for the whole class of UID diversification built on a SECRET
    master key (AES/HMAC, e.g. DESFire-style or vendor site keys). Registered so
    the CLI can name the boundary and refuse; it never produces a key."""

    name = "aes-diversified"
    description = (
        "AES/HMAC UID diversification with a SECRET master key — OUT OF SCOPE "
        "(cannot be generated without the secret; refuses)."
    )
    requires_secret = True
    sectors: FrozenSet[int] = frozenset()

    def generate(self, uid: str, sector: int) -> List[str]:
        raise SecretKeyRequiredError(
            "scheme 'aes-diversified' derives keys from a SECRET master key "
            "(AES/HMAC). Reproducing them needs that secret — cryptography out "
            "of scope for this tool (same wall as the nested attack). Refusing."
        )


# The registry. To add a public scheme: implement a class with (name,
# description, requires_secret=False, sectors, generate) and add an instance.
SCHEMES: Dict[str, "MizipScheme"] = {
    s.name: s for s in (MizipScheme(), SecretDiversifiedScheme())
}


def select(names: Iterable[str]) -> List["MizipScheme"]:
    """Resolve `--uid-derived` values to registered, *generable* schemes.

    NAMES are the raw strings (repeatable, each may be comma/space-separated).
    ``all`` expands to every public scheme (secret-key schemes are never swept
    in). An unknown name raises `UnknownUidSchemeError`; naming a secret-key
    scheme raises `SecretKeyRequiredError` — the guardrail fires up front with a
    clear message instead of silently doing nothing.
    """
    wanted: List[str] = []
    for raw in names:
        for token in raw.replace(",", " ").split():
            token = token.lower()
            if token == "all":
                wanted += [n for n, s in SCHEMES.items() if not s.requires_secret]
            elif token in SCHEMES:
                wanted.append(token)
            else:
                raise UnknownUidSchemeError(
                    f"unknown UID scheme {token!r} — known: "
                    f"{', '.join(SCHEMES)} (or 'all')"
                )
    unique = list(dict.fromkeys(wanted))
    for name in unique:
        if SCHEMES[name].requires_secret:
            raise SecretKeyRequiredError(
                f"UID scheme {name!r}: {SCHEMES[name].description}"
            )
    return [SCHEMES[n] for n in unique]


def candidates_for(
    uid: str, schemes: Iterable["MizipScheme"], sector: int, cap: int = 0
) -> List[str]:
    """De-duplicated UID-derived candidates for SECTOR across SCHEMES.

    A scheme that can't apply to this UID (wrong length) is skipped silently.
    ``cap`` (>0) trims the list to keep the extra authentications bounded — the
    latency guardrail; 0 means no cap.
    """
    out: List[str] = []
    for scheme in schemes:
        try:
            out.extend(scheme.generate(uid, sector))
        except UidSchemeError:
            continue  # UID this scheme can't use (e.g. 7-byte); just skip it
    unique = list(dict.fromkeys(k.upper() for k in out))
    return unique[:cap] if cap and cap > 0 else unique
