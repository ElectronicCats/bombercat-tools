#!/usr/bin/env python3

# Electronic Cats
# test_mifare_phase6.py — Fase 6 of PLAN_Mejoras_MIFARE.md: verify whether THIS
# hardware can rewrite a Mifare Classic UID (block 0), and classify the card.
#
# The whole point of Fase 6 is an HONEST verdict, so these tests pin exactly
# that:
#   - a gen2/CUID (direct-write) card: block 0 writable → UID rewrite SUPPORTED,
#     and writing a source UID reads back byte-for-byte (a real 1:1 clone), with
#     the BCC recomputed for the new UID;
#   - a genuine/locked card: block 0 NAKs the write → reported as needing the
#     gen1a backdoor, which the PN7150 cannot emit — and the command NEVER sends
#     a UID write to it (no backdoor is ever attempted);
#   - the read-only probe (no --uid) never changes the card.
#
# No PN7150: everything runs through mifare_card.CardLink, which models block-0
# writability. No key is guessed — sector 0 opens with the public default key.

import json

from modules.tags.mifare.clone_uid import (
    MAGIC_GEN2,
    MAGIC_LOCKED,
    craft_block0,
    mifare_clone_uid_cmd,
    verify_uid_clone,
)
from modules.tags.mifare import session as mifare_session

import mifare_card as mc


def _run(runner, use_link, args, card):
    """Invoke `mifare clone-uid` against a fresh CardLink over `card`, returning
    (result, fake). Hold onto `card` to assert on the mutated model."""
    fake = use_link(mifare_session, mc.CardLink(card))
    return runner.invoke(mifare_clone_uid_cmd, args), fake


# ── the pure block-0 crafter ────────────────────────────────────────────────────


def test_craft_block0_recomputes_bcc_and_keeps_the_rest():
    # UID AABBCCDD → BCC = 0xAA^0xBB^0xCC^0xDD = 0x00.
    got = craft_block0("AABBCCDD", "08", "0400", "C2000000B4030103")
    assert got == "AABBCCDD00080400C2000000B4030103"
    assert got[8:10] == "00"  # the recomputed check byte


def test_craft_block0_is_uppercase_and_full_length():
    got = craft_block0("decafbad", "08", "0400", "c2000000b4030103")
    assert got == got.upper() and len(got) == 32


# ── probe only (read-only): does NOT change the card ────────────────────────────


def test_probe_only_reports_gen2_writable_and_leaves_uid_untouched(runner, use_link):
    card = mc.magic_card_1k("gen2", uid="DECAFBAD")
    r, fake = _run(runner, use_link, ["--json"], card)
    assert r.exit_code == 0
    v = json.loads(r.output)
    assert v["block0_writable"] is True
    assert v["magic_type"] == MAGIC_GEN2
    assert v["uid_rewrite_supported"] is True
    assert v["target_uid"] is None and v["verified"] is None
    assert card.uid == "DECAFBAD"  # probe never rewrote the UID
    # The only block-0 write sent was the echo probe of the CURRENT block 0.
    writes = [s for s in fake.sent if s.startswith("mifare write 0 ")]
    assert writes == ["mifare write 0 " + v["block0_current"]]


def test_probe_only_on_locked_card_names_gen1a_limit_and_exits_1(runner, use_link):
    card = mc.magic_card_1k("locked", uid="DECAFBAD")
    r, fake = _run(runner, use_link, ["--json"], card)
    assert r.exit_code == 1  # "UID rewrite possible here?" → no
    v = json.loads(r.output)
    assert v["block0_writable"] is False
    assert v["magic_type"] == MAGIC_LOCKED
    assert v["uid_rewrite_supported"] is False
    assert "gen1a" in v["detail"] and "PN7150" in v["detail"]
    assert card.uid == "DECAFBAD"


# ── writing a source UID + read-back verification ───────────────────────────────


def test_gen2_write_uid_verifies_by_readback(runner, use_link):
    card = mc.magic_card_1k("gen2", uid="DECAFBAD")
    r, fake = _run(runner, use_link, ["--uid", "AABBCCDD", "--yes", "--json"], card)
    assert r.exit_code == 0
    v = json.loads(r.output)
    assert v["target_uid"] == "AABBCCDD"
    assert v["verified"] is True
    assert v["block0_written"] == "AABBCCDD00080400C2000000B4030103"
    # The model really took the new identity, BCC and all.
    assert card.uid == "AABBCCDD"
    assert card.block_hex(0) == "AABBCCDD00080400C2000000B4030103"


def test_locked_card_write_uid_is_refused_and_no_backdoor_attempted(runner, use_link):
    card = mc.magic_card_1k("locked", uid="DECAFBAD")
    r, fake = _run(runner, use_link, ["--uid", "AABBCCDD", "--yes", "--json"], card)
    assert r.exit_code == 1
    v = json.loads(r.output)
    assert v["block0_writable"] is False
    assert v["block0_written"] is None and v["verified"] is None
    # Crucial honesty check: the new UID was NEVER written — only the harmless
    # echo probe of the current block 0 was ever sent. No gen1a backdoor, no
    # blind UID write onto a card that can't take it.
    writes = [s for s in fake.sent if s.startswith("mifare write 0 ")]
    assert writes == ["mifare write 0 " + card.block_hex(0)]
    assert card.uid == "DECAFBAD"


def test_sak_and_atqa_overrides_land_in_the_written_block0(runner, use_link):
    card = mc.magic_card_1k("gen2", uid="DECAFBAD")
    r, _ = _run(
        runner,
        use_link,
        ["--uid", "11223344", "--sak", "18", "--atqa", "0200", "--yes", "--json"],
        card,
    )
    v = json.loads(r.output)
    # UID(4) BCC(1) SAK(1) ATQA(2) mfg(8); BCC(11^22^33^44)=0x44.
    assert v["block0_written"] == "1122334444180200C2000000B4030103"
    assert v["verified"] is True


# ── UID sourced from a dump ─────────────────────────────────────────────────────


def test_from_dump_sources_the_uid_from_block0(runner, use_link, tmp_path):
    src = mc.magic_card_1k("gen2", uid="F00DF00D")
    dump = tmp_path / "src.json"
    dump.write_text(json.dumps(src.to_dump_dict("1K")))

    target = mc.magic_card_1k("gen2", uid="DECAFBAD")
    r, _ = _run(runner, use_link, ["--from-dump", str(dump), "--yes", "--json"], target)
    v = json.loads(r.output)
    assert v["target_uid"] == "F00DF00D"
    assert v["verified"] is True
    assert target.uid == "F00DF00D"


def test_uid_and_from_dump_are_mutually_exclusive(runner, use_link, tmp_path):
    dump = tmp_path / "src.json"
    dump.write_text(json.dumps(mc.magic_card_1k("gen2").to_dump_dict("1K")))
    r, _ = _run(
        runner,
        use_link,
        ["--uid", "AABBCCDD", "--from-dump", str(dump), "--yes"],
        mc.magic_card_1k("gen2"),
    )
    assert r.exit_code == 1
    assert "either --uid or --from-dump" in r.output


# ── input validation and auth ───────────────────────────────────────────────────


def test_bad_uid_is_rejected_before_touching_the_device(runner, use_link):
    r, fake = _run(runner, use_link, ["--uid", "ZZ", "--yes"], mc.magic_card_1k("gen2"))
    assert r.exit_code == 1
    assert "--uid" in r.output
    assert fake.sent == []  # never opened a session


def test_wrong_sector0_key_fails_cleanly(runner, use_link):
    # Card sector 0 uses the default key; force a wrong key so auth fails.
    card = mc.magic_card_1k("gen2")
    r, _ = _run(runner, use_link, ["--key", "A0A1A2A3A4A5", "--json"], card)
    v = json.loads(r.output)
    assert r.exit_code == 1
    assert v["sector0_auth"] is None
    assert "authentication failed" in v["reason"]


# ── engine unit: the read-back mismatch path ────────────────────────────────────


class _MismatchLink(mc.CardLink):
    """A gen2 CardLink that corrupts the very next block-0 read after a write —
    to exercise verify_uid_clone's read-back mismatch branch without hardware."""

    def command(self, line, read_timeout=None):
        r = super().command(line, read_timeout)
        if line.strip().startswith("mifare write 0 "):
            self._corrupt_next = True
        elif line.strip() == "mifare read 0" and getattr(self, "_corrupt_next", False):
            self._corrupt_next = False
            return mc.ok(mifare_data="0 " + "FF" * 16)
        return r


def test_engine_flags_a_readback_mismatch(monkeypatch):
    card = mc.magic_card_1k("gen2", uid="DECAFBAD")
    link = _MismatchLink(card)
    v = verify_uid_clone(
        link, key_a=mc.KEY_DEFAULT, key_b=None, timeout=1.0, target_uid="AABBCCDD"
    )
    assert v["verified"] is False
    assert "read-back mismatch" in v["reason"]
