#!/usr/bin/env python3

# Electronic Cats
# mifare_card.py — synthetic MIFARE Classic card + hardware-free responder.
#
# Fase 0 of PLAN_Mejoras_MIFARE.md: a MIFARE Classic card modelled in memory so
# the `tags mifare` commands can run their REAL logic with no PN7150 attached.
# Tests use it to assert on key ordering (Fase 1) and on dump reports (Fase 4),
# and the baseline auth-count metric is measured by counting the auth lines it
# is asked (see test_mifare_phase0.py::test_baseline_*).
#
# Nothing here does cryptography. `auth` succeeds iff the caller already knows
# the sector's key — exactly the plan's frontier: it exercises the
# "weak/default keys" scenario, never Crypto-1 (nested/darkside/hardnested are
# permanently out of reach with this reader). It is NOT collected by pytest
# (filename isn't test_*.py); test_mifare_phase0.py imports it.
#
# This is a test/fixture helper, not shipped CLI code.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# tests/conftest.py already puts tools/ on sys.path for `modules.*`, and the
# FakeLink base lives beside us in conftest.
from conftest import FakeLink, err, ok

# ── key/geometry constants ────────────────────────────────────────────────────

KEY_DEFAULT = "FFFFFFFFFFFF"  # factory/transport default key
KEY_MAD = "A0A1A2A3A4A5"  # public MAD key A (NXP AN10787)
KEY_NDEF = "D3F7D3F7D3F7"  # public NFC Forum / NDEF key A
GPB_DEFAULT = 0x69  # trailer general-purpose byte, transport value

# key_b_read permission strings, as access_bits.parse_access_bits yields them.
_KEY_A = "key A"
_KEY_B = "key B"
_KEY_AB = "key A or B"
_NEVER = "never"


def sector_block_count(sector: int) -> int:
    """Blocks in SECTOR: 4 for a small sector, 16 for a 4K big sector (32-39)."""
    return 4 if sector < 32 else 16


def first_block(sector: int) -> int:
    """Absolute block index of SECTOR's first block."""
    if sector < 32:
        return sector * 4
    return 128 + (sector - 32) * 16


def sector_of_block(block: int) -> int:
    """Which sector contains absolute BLOCK (inverse of first_block)."""
    if block < 128:
        return block // 4
    return 32 + (block - 128) // 16


# ── low-level encoders (correct-by-construction; verified in fixtures) ─────────


def encode_access_bits(
    block_conditions: List[Tuple[int, int, int]],
    trailer_condition: Tuple[int, int, int],
    gpb: int = GPB_DEFAULT,
) -> str:
    """Inverse of access_bits.parse_access_bits: 3 data-block (C1,C2,C3) triples
    plus the trailer's triple → the 4 trailer access bytes (8 hex chars,
    including the general-purpose byte). The stored inverses are filled in, so
    the result always round-trips to ``valid=True``.
    """
    if len(block_conditions) != 3:
        raise ValueError("need exactly 3 data-block conditions")
    c = list(block_conditions) + [trailer_condition]  # index 3 = trailer
    c1 = [t[0] for t in c]
    c2 = [t[1] for t in c]
    c3 = [t[2] for t in c]
    inv = lambda bit: 1 - bit  # noqa: E731

    def pack(low: List[int], high: List[int]) -> int:
        v = 0
        for n in range(4):
            v |= (low[n] & 1) << n
            v |= (high[n] & 1) << (4 + n)
        return v

    b6 = pack([inv(x) for x in c1], [inv(x) for x in c2])
    b7 = pack([inv(x) for x in c3], c1)
    b8 = pack(c2, c3)
    return f"{b6:02X}{b7:02X}{b8:02X}{gpb & 0xFF:02X}"


# Two named trailer profiles used by the fixtures.
#   TRANSPORT: data blocks 000 (free r/w with A or B), trailer 001
#              -> key B is READABLE with key A. The insecure default.
#   HARDENED:  data blocks 100 (read A/B, write B), trailer 011
#              -> key B is NOT readable. What a secured card looks like.
AC_TRANSPORT = encode_access_bits([(0, 0, 0)] * 3, (0, 0, 1))
AC_HARDENED = encode_access_bits([(1, 0, 0)] * 3, (0, 1, 1))


def _crc8_mad(data: bytes) -> int:
    """CRC-8 over the MAD bytes (NXP AN10787): poly 0x1D, preset 0xC7,
    MSB-first, no final xor."""
    crc = 0xC7
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def mad1_blocks(aids: Dict[int, int], info: int = 0x01) -> Tuple[str, str]:
    """MAD1 for sectors 1..15 → (block1_hex, block2_hex).

    `aids` maps sector number (1..15) to its 16-bit application id; missing
    sectors are 0x0000 (free). Byte 0 of block 1 is the AN10787 CRC over the
    other 31 bytes, so a Fase 4 decoder can validate it.
    """
    body = bytearray()
    body.append(info & 0xFF)
    for sector in range(1, 16):
        body += int(aids.get(sector, 0)).to_bytes(2, "little")
    assert len(body) == 31, len(body)
    crc = _crc8_mad(bytes(body))
    full = bytes([crc]) + bytes(body)  # 32 bytes = block1 || block2
    return full[:16].hex().upper(), full[16:].hex().upper()


def value_block(value: int, addr: int) -> str:
    """A MIFARE value block (32 hex): value LE, ~value, value, then the address
    byte in the 4-times pattern addr, ~addr, addr, ~addr."""
    v = (value & 0xFFFFFFFF).to_bytes(4, "little")
    nv = bytes(b ^ 0xFF for b in v)
    a = addr & 0xFF
    tail = bytes([a, a ^ 0xFF, a, a ^ 0xFF])
    return (v + nv + v + tail).hex().upper()


# ── card model ────────────────────────────────────────────────────────────────


@dataclass
class Sector:
    key_a: str
    key_b: str
    access: str  # 8 hex chars: 3 access bytes + GPB
    data: List[str]  # data blocks, hex; length = sector_block_count - 1

    def trailer(self) -> str:
        """The trailer as stored on the card (real keys in place)."""
        return f"{self.key_a}{self.access}{self.key_b}".upper()

    def key_b_readable_with(self, key_type: str) -> bool:
        # Local import keeps this module usable even if access_bits moves.
        from modules.tags.mifare.access_bits import parse_access_bits

        parsed = parse_access_bits(self.access[:6])
        if parsed is None:
            return False
        return {
            _KEY_A: key_type == "A",
            _KEY_B: key_type == "B",
            _KEY_AB: True,
            _NEVER: False,
        }.get(parsed.trailer.key_b_read, False)


@dataclass
class Card:
    uid: str  # 8 hex (4-byte UID)
    sak: str  # 2 hex
    atqa: str  # 4 hex, byte order as read (e.g. "0400" for 1K)
    sectors: List[Sector] = field(default_factory=list)

    # -- what the firmware would answer -------------------------------------
    def auth(self, block: int, key_type: str, key: str) -> bool:
        s = sector_of_block(block)
        if not 0 <= s < len(self.sectors):
            return False
        want = self.sectors[s].key_a if key_type == "A" else self.sectors[s].key_b
        return key.upper() == want.upper()

    def read_sector(
        self, sector: int, key_type: str, key: str
    ) -> Tuple[Optional[str], str]:
        """Return (data_hex, reason) for a `mifare sector` read, mimicking a
        real card: key A never reads back; key B only when the access bits
        allow it for `key_type`; data blocks read as stored. 4-block sectors
        only (what the CLI addresses)."""
        if not 0 <= sector < len(self.sectors):
            return None, "no such sector"
        sec = self.sectors[sector]
        want = sec.key_a if key_type == "A" else sec.key_b
        if key.upper() != want.upper():
            return None, "authentication failed"
        shown_b = sec.key_b if sec.key_b_readable_with(key_type) else "0" * 12
        trailer = f"{'0' * 12}{sec.access}{shown_b}".upper()
        return "".join(sec.data[:3]) + trailer, "ok"

    # -- fixture serialisation ----------------------------------------------
    def to_keyfile_text(self) -> str:
        """`sector:keyA:keyB` file, as `mifare check --output-keys` writes it."""
        return "".join(f"{i}:{s.key_a}:{s.key_b}\n" for i, s in enumerate(self.sectors))

    def dictionary_keyfile_text(self, dictionary: set) -> str:
        """Like to_keyfile_text but blanks any key NOT in `dictionary` — the
        honest result a dictionary `check` would produce (private keys become
        declared gaps, never guessed)."""
        up = {k.upper() for k in dictionary}
        out = []
        for i, s in enumerate(self.sectors):
            a = s.key_a if s.key_a.upper() in up else ""
            b = s.key_b if s.key_b.upper() in up else ""
            out.append(f"{i}:{a}:{b}\n")
        return "".join(out)

    def to_dump_dict(
        self, size_label: str, source_keyfile: str = "", read_at: Optional[str] = None
    ) -> dict:
        """Canonical `mifare dump --out` JSON: real keys already substituted
        into each trailer (as dump.py does). Fully offline — this is what the
        Fase 4 analyzer will consume. `read_at` is fixed for committed fixtures
        so they don't churn; it defaults to now()."""
        sectors = []
        for i, s in enumerate(self.sectors):
            blocks = list(s.data) + [s.trailer()]
            sectors.append({"sector": i, "opened_with": "A", "blocks": blocks})
        return {
            "uid": self.uid,
            "size": size_label,
            "sectors_read": len(self.sectors),
            "sectors_total": len(self.sectors),
            "read_at": read_at
            or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_keyfile": source_keyfile,
            "sectors": sectors,
            "failed_sectors": [],
        }


class CardLink(FakeLink):
    """A FakeLink that answers `mifare auth`/`mifare sector` from a Card model
    instead of a hand-written responses dict. Every other command still
    succeeds (FakeLink default), so a test only scripts what it cares about.
    """

    def __init__(self, card: Card, **kw):
        super().__init__(**kw)
        self.card = card

    def command(self, line: str, read_timeout: Optional[float] = None):
        stripped = line.strip()
        parts = stripped.split()
        if parts[:2] == ["mifare", "auth"] and len(parts) == 5:
            self.sent.append(stripped)
            if self._trace is not None:
                self._trace("tx", stripped)
            block, kt, key = int(parts[2]), parts[3].upper(), parts[4]
            return (
                ok() if self.card.auth(block, kt, key) else err("authentication failed")
            )
        if parts[:2] == ["mifare", "sector"] and len(parts) == 5:
            self.sent.append(stripped)
            if self._trace is not None:
                self._trace("tx", stripped)
            sector, kt, key = int(parts[2]), parts[3].upper(), parts[4]
            data, reason = self.card.read_sector(sector, kt, key)
            return ok(mifare_sector=data) if data is not None else err(reason)
        return super().command(line, read_timeout)


# ── the two reference cards ───────────────────────────────────────────────────


def _block0(uid: str, sak: str, atqa: str, mfg: str = "C2000000B4030103") -> str:
    """Block 0 = UID(4) · BCC(1) · SAK(1) · ATQA(2) · manufacturer(8), 16 bytes.
    BCC is the XOR check byte over the 4 UID bytes."""
    ub = bytes.fromhex(uid)
    bcc = ub[0] ^ ub[1] ^ ub[2] ^ ub[3]
    return (uid + f"{bcc:02X}" + sak + atqa + mfg).upper()


def card_1k() -> Card:
    """A representative MIFARE Classic 1K (16 sectors):

    - sector 0: MAD key A (public), transport trailer, a real MAD1;
    - sectors 1-7: factory default keys — the weak/default scenario;
      sector 4 holds a value block;
    - sectors 8-14: the public NDEF key reused across all of them (key reuse,
      Fase 1's target); hardened trailers (key B not readable);
    - sector 15: a private key NOT in any dictionary — a genuine gap `check`
      cannot open and must declare, never conclude "secure".
    """
    b0 = _block0("DECAFBAD", "08", "0400")
    mad1, mad2 = mad1_blocks({1: 0x0004, 4: 0x1234})
    sectors: List[Sector] = []
    # sector 0
    sectors.append(Sector(KEY_MAD, KEY_DEFAULT, AC_TRANSPORT, [b0, mad1, mad2]))
    # sectors 1-7: default keys; sector 4 gets a value block
    for s in range(1, 8):
        data = ["00" * 16, "00" * 16, "00" * 16]
        if s == 4:
            data[0] = value_block(1000, addr=0x10)
        sectors.append(Sector(KEY_DEFAULT, KEY_DEFAULT, AC_TRANSPORT, data))
    # sectors 8-14: reused NDEF key, hardened
    for s in range(8, 15):
        sectors.append(Sector(KEY_NDEF, KEY_NDEF, AC_HARDENED, ["11" * 16] * 3))
    # sector 15: private, unknown to any dictionary
    sectors.append(Sector("445566778899", "998877665544", AC_HARDENED, ["22" * 16] * 3))
    return Card("DECAFBAD", "08", "0400", sectors)


def card_4k() -> Card:
    """A representative MIFARE Classic 4K (40 sectors). Sectors 0-31 are
    4-block, 32-39 are 16-block. MAD1 in sector 0 and MAD2 in sector 16, a
    reused vendor key across a run of sectors, one private-key gap. Only the
    dump-file view of this card is exercised in Fase 0 — the CLI does not yet
    address 4K's 16-block sectors (see common._sector_first_block)."""
    b0 = _block0("F00DBABE", "18", "0200")
    mad1, mad2 = mad1_blocks({1: 0x0004, 2: 0x0004, 20: 0x1111})
    mad2_b1, mad2_b2 = mad1_blocks({17: 0x2222}, info=0x01)
    sectors: List[Sector] = []
    for s in range(40):
        n_data = sector_block_count(s) - 1
        if s == 0:
            sectors.append(Sector(KEY_MAD, KEY_DEFAULT, AC_TRANSPORT, [b0, mad1, mad2]))
        elif s == 16:
            # MAD2 GPB sector: MAD key, MAD2 directory in its first data blocks.
            data = [mad2_b1, mad2_b2, "00" * 16]
            sectors.append(Sector(KEY_MAD, KEY_DEFAULT, AC_TRANSPORT, data))
        elif 1 <= s <= 8:
            data = ["00" * 16] * n_data
            if s == 3:
                data[0] = value_block(500, addr=0x30)
            sectors.append(Sector(KEY_DEFAULT, KEY_DEFAULT, AC_TRANSPORT, data))
        elif 9 <= s <= 15:
            sectors.append(
                Sector(KEY_NDEF, KEY_NDEF, AC_HARDENED, ["11" * 16] * n_data)
            )
        elif s == 39:
            sectors.append(
                Sector(
                    "445566778899", "998877665544", AC_HARDENED, ["22" * 16] * n_data
                )
            )
        else:
            sectors.append(
                Sector(KEY_DEFAULT, KEY_DEFAULT, AC_TRANSPORT, ["00" * 16] * n_data)
            )
    return Card("F00DBABE", "18", "0200", sectors)
