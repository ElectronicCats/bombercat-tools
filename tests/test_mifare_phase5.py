#!/usr/bin/env python3

# Electronic Cats
# test_mifare_phase5.py — Fase 5 of PLAN_Mejoras_MIFARE.md: integration,
# documentation and hardening (Mejoras A + B together). Proves the whole
# offensive-then-report pipeline hangs together end to end and that the CLI is
# coherent across commands:
#   - check --output-keys -> dump -k --out -> analyze -k: the `sector:keyA:keyB`
#     and canonical-dump contracts flow through untouched, hand-off to hand-off;
#   - a sector `check` can't open stays a declared GAP the whole way down and is
#     never concluded "secure" — the plan's non-negotiable honesty rule;
#   - `analyze`'s dictionary flags mirror `check`'s (`--dict NAMES` families +
#     `--dict-file FILE`), including the same loud failure on an unknown name;
#   - performance/hardening: Fase 1's known-first reuse + early-cut ABSORBS the
#     Fase 2-3 candidate expansion — adding every family and a UID scheme costs a
#     tiny bounded delta over baseline, not the sectors x universe of a naive
#     sweep (the review the plan's Fase 5 asks for, pinned as a regression).
#
# No PN7150: check/dump run through mifare_card.CardLink; analyze is pure offline.
# No key is ever guessed — every recovered key is a known/public value.

import json
from pathlib import Path

from modules.tags.mifare.analyze import mifare_analyze_cmd
from modules.tags.mifare.check import mifare_check_cmd
from modules.tags.mifare.dump import mifare_dump_cmd
from modules.tags.mifare.dicts import CandidatePlan, select
from modules.tags.mifare.keyfile import default_keyfile, load_keys, load_sector_keys
from modules.tags.mifare import session as mifare_session

import mifare_card as mc

FIXTURES = Path(__file__).parent / "fixtures" / "mifare"


def _auths(fake):
    return sum(1 for line in fake.sent if line.startswith("mifare auth"))


def _run(runner, use_link, cmd, args, card=None):
    """Invoke a device-backed mifare command against a fresh CardLink, returning
    (result, fake) so a caller can both assert on output and count auths. `card`
    defaults to a representative 1K; pass None for the offline `analyze`."""
    fake = use_link(mifare_session, mc.CardLink(card)) if card is not None else None
    return runner.invoke(cmd, args), fake


# ── end-to-end: check -> dump -> analyze ────────────────────────────────────────


def test_check_dump_analyze_round_trips_through_the_file_contracts(
    runner, use_link, tmp_path
):
    keys = tmp_path / "card.keys"
    dump = tmp_path / "card.json"

    # 1) check writes the sector:keyA:keyB file (exit 1: sector 15 is a real gap).
    r, _ = _run(
        runner,
        use_link,
        mifare_check_cmd,
        ["--sectors", "16", "-o", str(keys)],
        card=mc.card_1k(),
    )
    assert r.exit_code == 1
    assert keys.is_file()
    table = load_sector_keys(str(keys))
    assert table[0] != (None, None)  # MAD sector opened
    assert table[15] == (None, None)  # private sector: declared blank, not guessed

    # 2) dump consumes that keyfile and writes canonical JSON (exit 1: same gap).
    r, _ = _run(
        runner,
        use_link,
        mifare_dump_cmd,
        ["-k", str(keys), "--sectors", "16", "--out", str(dump)],
        card=mc.card_1k(),
    )
    assert r.exit_code == 1
    assert dump.is_file()
    dumped = json.loads(dump.read_text())
    assert dumped["uid"] == "DECAFBAD"
    assert {f["sector"] for f in dumped["failed_sectors"]} == {15}

    # 3) analyze consumes the dump + the same keyfile, 100% offline.
    r = runner.invoke(mifare_analyze_cmd, [str(dump), "-k", str(keys), "--json"])
    assert r.exit_code == 0
    report = json.loads(r.stdout)

    # the report reads what the pipeline carried: UID, MAD, the value block…
    assert report["uid"] == "DECAFBAD"
    assert report["mad"] and report["mad"]["crc_valid"]
    allocated = {
        e["sector"]: e["aid"] for e in report["mad"]["entries"] if e["allocated"]
    }
    assert allocated == {1: "0004", 4: "1234"}
    s4 = next(s for s in report["sectors"] if s["sector"] == 4)
    assert s4["value_blocks"] and s4["value_blocks"][0]["value"] == 1000

    # …every opened sector on this card is default/known -> flagged weak…
    assert set(report["summary"]["weak_key_sectors"]) == set(range(15))
    assert report["summary"]["custom_key_sectors"] == []


def test_the_private_sector_stays_a_declared_gap_end_to_end(runner, use_link, tmp_path):
    """The one sector no dictionary opens must survive as a gap through every
    stage and never be concluded secure — the plan's honesty rule, tested on the
    real check->dump->analyze chain rather than a hand-built dump."""
    keys = tmp_path / "card.keys"
    dump = tmp_path / "card.json"

    _run(
        runner,
        use_link,
        mifare_check_cmd,
        ["--sectors", "16", "-o", str(keys)],
        card=mc.card_1k(),
    )
    _run(
        runner,
        use_link,
        mifare_dump_cmd,
        ["-k", str(keys), "--sectors", "16", "--out", str(dump)],
        card=mc.card_1k(),
    )
    report = json.loads(
        runner.invoke(mifare_analyze_cmd, [str(dump), "-k", str(keys), "--json"]).stdout
    )

    assert 15 in report["summary"]["gap_sectors"]
    assert any(g["sector"] == 15 for g in report["gaps"])
    # a gap is NOT analyzed as a sector — it never becomes a security claim.
    assert all(s["sector"] != 15 for s in report["sectors"])

    # and the human-readable report says so out loud.
    text = runner.invoke(mifare_analyze_cmd, [str(dump), "-k", str(keys)]).output
    assert "no security claim is made about them" in " ".join(text.split())


# ── flag unification: analyze mirrors check ─────────────────────────────────────


def test_analyze_dict_flags_mirror_check(runner, tmp_path):
    dump = str(FIXTURES / "card_1k.json")

    # --dict takes named families, exactly like `mifare check --dict`.
    r = runner.invoke(mifare_analyze_cmd, [dump, "--dict", "transport,mad", "--json"])
    assert r.exit_code == 0

    # an unknown family name fails loudly (same UnknownDictError path as check),
    # never silently ignored.
    r = runner.invoke(mifare_analyze_cmd, [dump, "--dict", "nope", "--json"])
    assert r.exit_code == 1
    assert "unknown dictionary" in r.output

    # --dict-file takes a plain key file (what analyze's `--dict` used to mean).
    extra = tmp_path / "extra.dic"
    extra.write_text("A1B2C3D4E5F6\n")
    r = runner.invoke(mifare_analyze_cmd, [dump, "--dict-file", str(extra), "--json"])
    assert r.exit_code == 0


def test_analyze_dict_reclassifies_a_family_key_as_weak(runner, tmp_path):
    """A key that is a KNOWN family key but not in the base dictionary should be
    counted as weak once its family is named — the point of giving analyze the
    same `--dict` families as check."""
    dump = tmp_path / "d.json"
    # a card whose sole key is a public lock-vendor key that the base dictionary
    # does NOT contain (it lives only in the "locks" family — see locks.dic).
    key = "A1B2C3D4E5F6"
    assert key not in set(load_keys([str(default_keyfile())]))
    trailer = f"{key}FF078069{key}"
    dump.write_text(
        json.dumps(
            {
                "uid": "DEADBEEF",
                "size": "1K",
                "sectors_total": 1,
                "sectors": [
                    {
                        "sector": 0,
                        "opened_with": "A",
                        "blocks": ["00" * 16, "00" * 16, "00" * 16, trailer],
                    }
                ],
                "failed_sectors": [],
            }
        )
    )
    # without the family: the key isn't in the base dict -> classed custom.
    plain = json.loads(runner.invoke(mifare_analyze_cmd, [str(dump), "--json"]).stdout)
    # with the family named: same key is now known/weak.
    withfam = json.loads(
        runner.invoke(
            mifare_analyze_cmd, [str(dump), "--dict", "locks", "--json"]
        ).stdout
    )
    assert plain["sectors"][0]["key_a_class"] == "custom"
    assert withfam["sectors"][0]["key_a_class"] == "default"


# ── performance review (plan Fase 5 task 5) ─────────────────────────────────────
# Fase 1's known-first reuse + per-sector early-cut is already in check.py; these
# pins prove it ABSORBS the Fase 2-3 candidate expansion instead of multiplying
# the auth traffic by it. Numbers are the CardLink auth counts for card_1k
# (key-type A), consistent with the 2514 baseline pinned in Fase 0.


def test_dict_and_uid_expansion_is_absorbed_by_known_first_reuse(runner, use_link):
    def auths(args):
        _, fake = _run(runner, use_link, mifare_check_cmd, args, card=mc.card_1k())
        return _auths(fake)

    a_base = auths(["--sectors", "16", "--key-type", "A", "--json"])
    a_all = auths(["--sectors", "16", "--key-type", "A", "--dict", "all", "--json"])
    a_uid = auths(
        ["--sectors", "16", "--key-type", "A", "--uid-derived", "mizip", "--json"]
    )
    a_both = auths(
        [
            "--sectors",
            "16",
            "--key-type",
            "A",
            "--dict",
            "all",
            "--uid-derived",
            "mizip",
            "--json",
        ]
    )

    # exact pins: any drift is a deliberate, visible change (like Fase 0/2/3).
    assert a_base == 2514, a_base
    assert a_all == 2525, a_all
    assert a_uid == 2516, a_uid
    assert a_both == 2527, a_both

    # the review's conclusion, made mechanical: turning on every family AND a UID
    # scheme adds only a tiny bounded delta over baseline…
    assert a_both - a_base <= 20
    # …nowhere near the sectors x universe a naive per-sector sweep would cost.
    universe = CandidatePlan(
        load_keys([str(default_keyfile())]), select(["all"]), []
    ).size
    assert a_both < 0.1 * 16 * universe
