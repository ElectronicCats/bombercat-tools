#!/usr/bin/env python3

# Electronic Cats
# test_mifare_parity.py — the firmware and the host must agree on the
# "decryption" logic (Plan de Implementacion Firmware MIFARE, Fase 4 /
# tarea 4.4).
#
# The firmware re-implements two pieces of the host's logic so it can recover
# keys and read sectors on its own:
#
#   * MifareAccessBits  <->  modules/tags/mifare/access_bits.py
#   * MifareBlock0      <->  modules/tags/mifare/block0.py
#
# "The same logic on both sides" is only worth something if it is CHECKED, so
# this test compiles the firmware's modules natively (g++, no Arduino) and
# compares their verdicts with the Python ones over a vector set: every
# access-condition triple (plus a corrupted one whose inverse check must
# fail) and a set of block-0 layouts.
#
# The test SKIPS (not fails) when the firmware sources are not next to the
# tools checkout, so the tools suite stays runnable on its own.

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from modules.tags.mifare.access_bits import parse_access_bits
from modules.tags.mifare.block0 import parse_block0

import mifare_card as mc

# Where the firmware's twin modules live. By default: the sibling checkout
# (../bombercat-firmware/MifareClassic). A CI job that checks out both repos
# can point at it with BOMBERCAT_FIRMWARE_DIR.
FIRMWARE_DIR = Path(
    os.environ.get(
        "BOMBERCAT_FIRMWARE_DIR",
        str(
            Path(__file__).resolve().parents[2] / "bombercat-firmware" / "MifareClassic"
        ),
    )
)
HARNESS = Path(__file__).parent / "parity" / "mifare_parity.cpp"

pytestmark = pytest.mark.skipif(
    not (FIRMWARE_DIR / "MifareAccessBits.cpp").exists()
    or not (FIRMWARE_DIR / "MifareBlock0.cpp").exists()
    or not HARNESS.exists(),
    reason="the MifareClassic firmware sources are not next to the tools checkout",
)


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    """The compiled parity harness, ready to be fed vectors."""
    if shutil.which("g++") is None:
        pytest.skip("g++ is not available")
    out = tmp_path_factory.mktemp("parity") / "mifare_parity"
    cmd = [
        "g++",
        "-std=c++17",
        "-O1",
        "-I",
        str(FIRMWARE_DIR),
        str(FIRMWARE_DIR / "MifareAccessBits.cpp"),
        str(FIRMWARE_DIR / "MifareBlock0.cpp"),
        str(HARNESS),
        "-o",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f"the parity harness does not build here: {proc.stderr[-400:]}")
    return out


def _run(harness, vectors):
    """Feed the vectors to the harness and return its output lines."""
    proc = subprocess.run(
        [str(harness)],
        input="\n".join(vectors) + "\n",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().splitlines()


# ── access bits ───────────────────────────────────────────────────────────────


# The host's permission names -> the space-free names the harness prints.
_PERM_CANONICAL = {
    "never": "never",
    "key A": "key_A",
    "key B": "key_B",
    "key A or B": "key_AB",
}


def _perm(name):
    return _PERM_CANONICAL[name]


def _py_access_line(ac_hex):
    """The canonical line the Python implementation produces for a trailer's
    3 access-condition bytes. The names are space-free on both sides (the
    line is compared token by token)."""
    parsed = parse_access_bits(ac_hex)
    assert parsed is not None
    parts = [f"A {1 if parsed.valid else 0}"]
    for block in parsed.blocks:
        parts.append(
            "|".join(
                [
                    _perm(block.read),
                    _perm(block.write),
                    _perm(block.increment),
                    _perm(block.decrement),
                ]
            )
        )
    t = parsed.trailer
    parts.append(
        "|".join(
            [
                _perm(t.key_a_write),
                _perm(t.ac_read),
                _perm(t.ac_write),
                _perm(t.key_b_read),
                _perm(t.key_b_write),
            ]
        )
    )
    return " ".join(parts)


def _access_vectors():
    """Every access-condition triple, encoded with valid inverses, plus one
    corrupted trailer whose inverse check must fail."""
    vectors = []
    for c1 in (0, 1):
        for c2 in (0, 1):
            for c3 in (0, 1):
                ac = mc.encode_access_bits([(c1, c2, c3)] * 3, (c1, c2, c3))
                vectors.append(ac[:6])
    # A trailer whose stored inverses disagree with the bits: the decode must
    # still happen, but `valid` must be false.
    vectors.append("000000")
    # The two profiles the card fixtures use.
    vectors.append(mc.AC_TRANSPORT[:6])
    vectors.append(mc.AC_HARDENED[:6])
    return vectors


def test_access_bits_parity(harness):
    vectors = _access_vectors()
    lines = _run(harness, [f"access {v}" for v in vectors])
    assert len(lines) == len(vectors)
    for vector, line in zip(vectors, lines):
        assert line == _py_access_line(vector), f"access bits differ for {vector}"


def test_access_bits_parity_covers_the_transport_profile(harness):
    # The transport profile is the one that lets Key B be read back with
    # Key A - the shortcut the key recovery relies on. Both sides must see it.
    vector = mc.AC_TRANSPORT[:6]
    line = _run(harness, [f"access {vector}"])[0]
    parsed = parse_access_bits(vector)
    assert parsed.trailer.key_b_read == "key A"
    trailer_group = line.split()[-1].split("|")
    assert len(trailer_group) == 5
    assert trailer_group[3] == "key_A"  # key_b_read


# ── block 0 ───────────────────────────────────────────────────────────────────


def _py_block0_line(data_hex):
    parsed = parse_block0(data_hex)
    assert parsed is not None
    return (
        f"B {parsed.uid} {parsed.bcc} {1 if parsed.bcc_valid else 0} "
        f"{parsed.sak} {parsed.atqa} {parsed.manufacturer_data} "
        f"{parsed.sak_name.replace(' ', '_')}|{parsed.atqa_name.replace(' ', '_')}"
    )


def _block0_vectors():
    """Block 0 layouts: the fixture cards' own block 0, one with a broken
    check byte, and one with an unknown SAK/ATQA."""
    vectors = []
    for card in (mc.card_1k(), mc.card_4k(), mc.magic_card_1k()):
        vectors.append(card.sectors[0].data[0])
    # Broken BCC: the UID stays, the check byte does not match.
    good = mc.card_1k().sectors[0].data[0]
    vectors.append(good[:8] + "00" + good[10:])
    # Unknown SAK/ATQA.
    vectors.append(good[:10] + "77" + "9999" + good[16:])
    return vectors


def test_block0_parity(harness):
    vectors = _block0_vectors()
    lines = _run(harness, [f"block0 {v}" for v in vectors])
    assert len(lines) == len(vectors)
    for vector, line in zip(vectors, lines):
        assert line == _py_block0_line(vector), f"block 0 differs for {vector}"


def test_block0_parity_reports_a_broken_check_byte(harness):
    good = mc.card_1k().sectors[0].data[0]
    broken = good[:8] + "00" + good[10:]
    line = _run(harness, [f"block0 {broken}"])[0]
    assert line.split()[3] == "0"  # bcc_valid == 0
    assert parse_block0(broken).bcc_valid is False
