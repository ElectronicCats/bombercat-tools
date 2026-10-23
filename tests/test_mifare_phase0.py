#!/usr/bin/env python3

# Electronic Cats
# test_mifare_phase0.py — Fase 0 of PLAN_Mejoras_MIFARE.md: the hardware-free
# base of tests. Proves the synthetic-card responder (mifare_card.py) behaves
# like a real card against the REAL `check`/`dump` logic, that the committed
# fixtures are internally consistent, and it pins the baseline auth counts that
# Fase 1 (key-reuse propagation) must reduce without losing coverage.
#
# No PN7150 is involved: every command runs through mifare_card.CardLink.

import json
from pathlib import Path

from conftest import flat
from modules.tags.mifare.access_bits import parse_access_bits
from modules.tags.mifare.block0 import parse_block0
from modules.tags.mifare.check import mifare_check_cmd
from modules.tags.mifare.dump import mifare_dump_cmd
from modules.tags.mifare.keyfile import default_keyfile, load_keys, load_sector_keys
from modules.tags.mifare import session as mifare_session

import mifare_card as mc

FIXTURES = Path(__file__).parent / "fixtures" / "mifare"


# ── fixture integrity ─────────────────────────────────────────────────────────


def test_fixtures_exist():
    for name in ("card_1k", "card_4k"):
        assert (FIXTURES / f"{name}.json").is_file()
        assert (FIXTURES / f"{name}.keys").is_file()
        assert (FIXTURES / f"{name}.dict.keys").is_file()


def test_1k_dump_fixture_is_internally_consistent():
    dump = json.loads((FIXTURES / "card_1k.json").read_text())
    assert dump["size"] == "1K"
    assert dump["sectors_total"] == 16
    assert len(dump["sectors"]) == 16

    # every trailer decodes to VALID access bits
    for s in dump["sectors"]:
        trailer = s["blocks"][-1]
        parsed = parse_access_bits(trailer[12:18])
        assert parsed is not None and parsed.valid, s["sector"]

    # block 0 dissects and its BCC checks out
    b0 = parse_block0(dump["sectors"][0]["blocks"][0])
    assert b0 is not None and b0.bcc_valid
    assert b0.uid == dump["uid"] == "DECAFBAD"
    assert "1K" in b0.sak_name


def test_4k_dump_fixture_has_40_sectors_with_big_sector_geometry():
    dump = json.loads((FIXTURES / "card_4k.json").read_text())
    assert dump["size"] == "4K"
    assert len(dump["sectors"]) == 40
    # sectors 0-31 are 4-block, 32-39 are 16-block
    assert len(dump["sectors"][0]["blocks"]) == 4
    assert len(dump["sectors"][39]["blocks"]) == 16
    b0 = parse_block0(dump["sectors"][0]["blocks"][0])
    assert "4K" in b0.sak_name


def test_1k_mad_crc_is_valid():
    # Fase 4 will validate the MAD; the fixture must carry a correct AN10787 CRC.
    dump = json.loads((FIXTURES / "card_1k.json").read_text())
    mad = bytes.fromhex(
        dump["sectors"][0]["blocks"][1] + dump["sectors"][0]["blocks"][2]
    )
    assert mad[0] == mc._crc8_mad(mad[1:])


def test_1k_has_a_value_block_in_sector_4():
    dump = json.loads((FIXTURES / "card_1k.json").read_text())
    vb = bytes.fromhex(dump["sectors"][4]["blocks"][0])
    value = int.from_bytes(vb[0:4], "little")
    inv = int.from_bytes(vb[4:8], "little")
    assert value == 1000
    assert value ^ inv == 0xFFFFFFFF  # the value/complement invariant


def test_dictionary_keyfile_declares_the_private_sector_as_a_gap():
    # sector 15's key is not in any dictionary: check can't open it, so the
    # dictionary-view keyfile leaves it blank — a declared hole, never a guess.
    lines = (FIXTURES / "card_1k.dict.keys").read_text().splitlines()
    assert lines[15] == "15::"
    table = load_sector_keys(str(FIXTURES / "card_1k.dict.keys"))
    assert table[15] == (None, None)
    # the full keyfile still records the real (known) key for that sector
    full = load_sector_keys(str(FIXTURES / "card_1k.keys"))
    assert full[15] == ("445566778899", "998877665544")


# ── mock fidelity: the responder behaves like a card ──────────────────────────


def test_cardlink_auth_accepts_the_right_key_and_rejects_others():
    card = mc.card_1k()
    link = mc.CardLink(card)
    assert link.command("mifare auth 0 A A0A1A2A3A4A5").ok  # sector 0 key A
    assert not link.command("mifare auth 0 A FFFFFFFFFFFF").ok  # wrong key A
    assert link.command("mifare auth 4 A FFFFFFFFFFFF").ok  # sector 1 default
    # unrelated commands still succeed via FakeLink's default
    assert link.command("mifare read 4").ok


def test_cardlink_sector_read_reveals_key_b_only_when_access_allows():
    card = mc.card_1k()
    link = mc.CardLink(card)
    # transport sector 0: key B readable with key A -> real bytes come back
    r = link.command("mifare sector 0 A A0A1A2A3A4A5")
    assert r.ok and r.data["mifare_sector"][-12:] == "FFFFFFFFFFFF"
    # hardened sector 8: key B protected -> zeros
    r = link.command("mifare sector 8 A D3F7D3F7D3F7")
    assert r.ok and r.data["mifare_sector"][-12:] == "000000000000"


# ── check/dump run their real logic against the card ──────────────────────────


def _check(runner, use_link, card, args):
    """Run `mifare check` against CARD, returning (result, fake)."""
    fake = use_link(mifare_session, mc.CardLink(card))
    result = runner.invoke(mifare_check_cmd, args)
    return result, fake


def test_check_recovers_exactly_the_dictionary_view_of_the_1k_card(runner, use_link):
    card = mc.card_1k()
    result, _ = _check(runner, use_link, card, ["--sectors", "16", "--json"])
    payload = json.loads(result.stdout)
    # 15 of 16 sectors open; sector 15 (private key) stays unknown.
    recovered = {s["sector"]: (s["key_a"], s["key_b"]) for s in payload["sectors"]}
    expected = load_sector_keys(str(FIXTURES / "card_1k.dict.keys"))
    for sec, (a, b) in expected.items():
        assert recovered[sec] == (a, b), sec
    assert recovered[15] == (None, None)
    assert result.exit_code == 1  # not fully recovered -> honest non-zero exit


def test_dump_with_full_keyfile_reproduces_the_fixture_blocks(runner, use_link):
    card = mc.card_1k()
    fake = use_link(mifare_session, mc.CardLink(card))
    result = runner.invoke(
        mifare_dump_cmd,
        ["--keys-file", str(FIXTURES / "card_1k.keys"), "--sectors", "16", "--json"],
    )
    payload = json.loads(result.stdout)
    fixture = json.loads((FIXTURES / "card_1k.json").read_text())

    assert result.exit_code == 0
    assert payload["uid"] == fixture["uid"]
    # every sector's blocks (real keys substituted into the trailer) match
    got = {s["sector"]: s["blocks"] for s in payload["sectors"]}
    want = {s["sector"]: s["blocks"] for s in fixture["sectors"]}
    assert got == want


# ── baseline metrics (pinned; Fase 1 must not exceed these) ───────────────────
# `check` already does known-keys-first + early-cut per sector (check.py). These
# counts are the CURRENT state's auth traffic; Fase 1 tightens the numbers on
# cards that reuse keys and must never lose a recovered sector doing so.

_DICT_SIZE = len(load_keys([str(default_keyfile())]))


def _auth_count(fake):
    return sum(1 for line in fake.sent if line.startswith("mifare auth"))


def test_baseline_1k_auth_counts_key_type_a(runner, use_link):
    card = mc.card_1k()
    result, fake = _check(
        runner, use_link, card, ["--sectors", "16", "--key-type", "A", "--json"]
    )
    auths = _auth_count(fake)
    # 15 sectors open early via known-first; sector 15 sweeps the whole dict.
    assert auths == 2514, auths
    # sanity: the unknown sector alone accounts for a full dictionary pass
    assert auths > _DICT_SIZE


def test_baseline_1k_auth_counts_both_key_types(runner, use_link):
    card = mc.card_1k()
    result, fake = _check(runner, use_link, card, ["--sectors", "16", "--json"])
    auths = _auth_count(fake)
    # sector 15 is swept for BOTH key types -> ~2x the dictionary on that sector.
    assert auths == 5028, auths


def test_baseline_2k_is_the_current_cli_ceiling(runner, use_link):
    # The current CLI caps --sectors at 32 (2K); 4K's sectors 32-39 aren't
    # addressable (common._sector_first_block). This pins that ceiling so a
    # future 4K change is a deliberate, visible edit.
    card = mc.card_4k()  # a 40-sector card, but only 32 are reachable here
    result, fake = _check(
        runner, use_link, card, ["--sectors", "32", "--key-type", "A", "--json"]
    )
    # the private-key sector (39) is out of reach, so all 32 reachable sectors
    # open and the run is a clean success — the ceiling, not a failure.
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload["sectors"]) == 32
    assert _auth_count(fake) == 70  # all found early via known-keys-first

    # asking for more than the ceiling is rejected by the CLI, not silently run
    over = runner.invoke(
        mifare_check_cmd, ["--sectors", "40", "--key-type", "A", "--json"]
    )
    assert over.exit_code != 0
