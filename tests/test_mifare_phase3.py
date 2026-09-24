#!/usr/bin/env python3

# Electronic Cats
# test_mifare_phase3.py — Fase 3 of PLAN_Mejoras_MIFARE.md: UID-derived key
# candidates from PUBLIC diversification schemes. Proves:
#   - the MiZip generator reproduces the published algorithm exactly (pinned
#     vectors guard against drift);
#   - schemes are selectable/combinable and the guardrail refuses secret-key
#     schemes and unknown names loudly, up front;
#   - `check --uid-derived mizip` opens sectors the base dictionary cannot, by
#     recomputing keys from the UID read off block 0;
#   - `--uid-max` bounds the extra authentications;
#   - a card whose sector 0 never opens degrades cleanly (UID unavailable →
#     schemes skipped), never crashes or invents keys;
#   - selecting nothing changes nothing (baseline intact).
#
# No PN7150 is involved: every command runs through mifare_card.CardLink, and no
# key is ever guessed — only recomputed from a public, documented algorithm.

import json

from modules.tags.mifare import uidkeys
from modules.tags.mifare.check import mifare_check_cmd
from modules.tags.mifare.keyfile import default_keyfile, load_keys
from modules.tags.mifare import session as mifare_session

import mifare_card as mc

# A concrete 4-byte UID and the MiZip keys it must produce, computed by hand from
# proxmark3's published XOR tables (hf_mf_uidkeycalc_mizip.lua). Pinning them
# here means a regression in the algorithm fails loudly instead of silently
# changing which keys `check` tries.
UID = "04123456"
MIZIP_VECTORS = {
    0: ("A0A1A2A3A4A5", "B4C132439EEF"),  # public MAD sector, no UID
    1: ("0D006E738DF7", "C57A8041EC77"),
    2: ("AF67FD61963D", "47B19DEC0617"),
}


# ── the generator reproduces the published algorithm ──────────────────────────


def test_mizip_matches_pinned_vectors():
    sc = uidkeys.SCHEMES["mizip"]
    for sector, expected in MIZIP_VECTORS.items():
        assert tuple(sc.generate(UID, sector)) == expected, sector


def test_mizip_ignores_uncovered_sectors_and_bad_uid_length():
    sc = uidkeys.SCHEMES["mizip"]
    assert sc.generate(UID, 5) == []  # MIFARE Mini is sectors 0-4 only
    # a 7-byte UID isn't what this scheme covers -> candidates_for skips it,
    # never fabricates a key from the wrong number of bytes.
    assert uidkeys.candidates_for("04123456789ABC", [sc], 1) == []


# ── selection layer + guardrail ───────────────────────────────────────────────


def test_select_all_excludes_secret_key_schemes():
    # 'all' must sweep in only what can actually be generated — never the
    # secret-key placeholder.
    names = [s.name for s in uidkeys.select(["all"])]
    assert names == ["mizip"]
    assert "aes-diversified" not in names


def test_select_secret_scheme_refuses_up_front():
    try:
        uidkeys.select(["aes-diversified"])
    except uidkeys.SecretKeyRequiredError as e:
        assert "secret" in str(e).lower() and "out of scope" in str(e).lower()
    else:
        raise AssertionError("expected SecretKeyRequiredError")


def test_secret_scheme_generate_also_refuses():
    # even if reached directly, the generator itself refuses — the boundary is
    # not just enforced at selection time.
    try:
        uidkeys.SCHEMES["aes-diversified"].generate(UID, 1)
    except uidkeys.SecretKeyRequiredError:
        pass
    else:
        raise AssertionError("expected SecretKeyRequiredError")


def test_select_rejects_unknown_names_loudly():
    try:
        uidkeys.select(["mizip,bogus"])
    except uidkeys.UnknownUidSchemeError as e:
        assert "bogus" in str(e)
    else:
        raise AssertionError("expected UnknownUidSchemeError")


def test_uid_max_caps_candidates_per_sector():
    sc = uidkeys.SCHEMES["mizip"]
    full = uidkeys.candidates_for(UID, [sc], 1, cap=0)
    assert len(full) == 2
    assert uidkeys.candidates_for(UID, [sc], 1, cap=1) == full[:1]


# ── coverage: UID-derived keys open sectors the base dictionary cannot ─────────


def test_mizip_keys_are_absent_from_the_base_dictionary():
    # Premise of the coverage test: the derived keys are NOT in the bundled
    # dictionary, so opening those sectors is genuinely down to UID derivation.
    base = {k.upper() for k in load_keys([str(default_keyfile())])}
    for sector in (1, 2):
        for key in MIZIP_VECTORS[sector]:
            assert key not in base, key


def _mizip_card():
    """A MIFARE Mini (5 sectors) whose sectors 1-4 use MiZip UID-derived keys.

    Sector 0 keeps the public MAD key A and the factory default as key B, so the
    UID is readable off block 0 under either key type (what the CLI needs before
    it can derive the rest)."""
    b0 = mc._block0(UID, "09", "0400")  # SAK 09 = MIFARE Mini
    sc = uidkeys.SCHEMES["mizip"]
    sectors = [
        mc.Sector(
            mc.KEY_MAD, mc.KEY_DEFAULT, mc.AC_TRANSPORT, [b0, "00" * 16, "00" * 16]
        )
    ]
    for s in range(1, 5):
        key_a, key_b = sc.generate(UID, s)
        sectors.append(mc.Sector(key_a, key_b, mc.AC_TRANSPORT, ["00" * 16] * 3))
    return mc.Card(UID, "09", "0400", sectors)


def _check(runner, use_link, card, args):
    fake = use_link(mifare_session, mc.CardLink(card))
    return runner.invoke(mifare_check_cmd, args), fake


def test_base_dictionary_cannot_open_the_mizip_sectors(runner, use_link):
    result, _ = _check(
        runner, use_link, _mizip_card(), ["--sectors", "5", "--key-type", "A", "--json"]
    )
    payload = json.loads(result.stdout)
    by_sector = {s["sector"]: s for s in payload["sectors"]}
    for sector in (1, 2, 3, 4):
        assert by_sector[sector]["key_a"] is None  # declared gaps, never guessed
    assert result.exit_code == 1


def test_uid_derived_mizip_opens_the_sectors(runner, use_link):
    result, _ = _check(
        runner,
        use_link,
        _mizip_card(),
        ["--sectors", "5", "--key-type", "A", "--uid-derived", "mizip", "--json"],
    )
    payload = json.loads(result.stdout)
    by_sector = {s["sector"]: s for s in payload["sectors"]}
    for sector in (1, 2):
        assert by_sector[sector]["key_a"] == MIZIP_VECTORS[sector][0]
    assert payload["recovered"] == 5
    assert result.exit_code == 0


def test_uid_max_flag_bounds_the_derived_candidates(runner, use_link):
    # key B is the SECOND MiZip candidate per sector; --uid-max 1 stops before
    # it, so those sectors stay gaps. (Sector 0 key B is the factory default, in
    # the base dict, so the UID is still read.)
    result, _ = _check(
        runner,
        use_link,
        _mizip_card(),
        [
            "--sectors",
            "5",
            "--key-type",
            "B",
            "--uid-derived",
            "mizip",
            "--uid-max",
            "1",
            "--json",
        ],
    )
    payload = json.loads(result.stdout)
    by_sector = {s["sector"]: s for s in payload["sectors"]}
    for sector in (1, 2, 3, 4):
        assert by_sector[sector]["key_b"] is None  # capped out
    assert result.exit_code == 1


def test_uid_derived_recovers_key_b_without_the_cap(runner, use_link):
    result, _ = _check(
        runner,
        use_link,
        _mizip_card(),
        ["--sectors", "5", "--key-type", "B", "--uid-derived", "mizip", "--json"],
    )
    by_sector = {s["sector"]: s for s in json.loads(result.stdout)["sectors"]}
    for sector in (1, 2):
        assert by_sector[sector]["key_b"] == MIZIP_VECTORS[sector][1]


# ── graceful degradation: no UID, no derivation, no crash ──────────────────────


def _sealed_sector0_card():
    """A card whose sector 0 opens with no known key — the UID can't be read, so
    UID derivation simply can't run. It must be skipped, not crash."""
    b0 = mc._block0(UID, "09", "0400")
    private = mc.Sector(
        "445566778899", "998877665544", mc.AC_HARDENED, [b0, "00" * 16, "00" * 16]
    )
    others = [
        mc.Sector(mc.KEY_DEFAULT, mc.KEY_DEFAULT, mc.AC_TRANSPORT, ["00" * 16] * 3)
        for _ in range(1, 5)
    ]
    return mc.Card(UID, "09", "0400", [private] + others)


def test_unreadable_uid_skips_schemes_without_crashing(runner, use_link):
    result, _ = _check(
        runner,
        use_link,
        _sealed_sector0_card(),
        ["--sectors", "5", "--key-type", "A", "--uid-derived", "mizip", "--json"],
    )
    payload = json.loads(result.stdout)
    assert payload["sectors"][0]["key_a"] is None  # sector 0 was the gap
    assert result.exit_code == 1  # not a crash, an honest partial result


# ── CLI surface + regression ───────────────────────────────────────────────────


def test_list_uid_schemes_prints_every_scheme_and_exits_zero(runner):
    result = runner.invoke(mifare_check_cmd, ["--list-uid-schemes"])
    assert result.exit_code == 0
    for name in uidkeys.SCHEMES:
        assert name in result.stdout


def test_unknown_uid_scheme_fails_before_touching_the_device(runner):
    result = runner.invoke(
        mifare_check_cmd, ["--uid-derived", "nope", "--sectors", "1", "--json"]
    )
    assert result.exit_code == 1
    assert "nope" in result.output


def test_secret_uid_scheme_fails_before_touching_the_device(runner):
    result = runner.invoke(
        mifare_check_cmd,
        ["--uid-derived", "aes-diversified", "--sectors", "1", "--json"],
    )
    assert result.exit_code == 1
    assert "out of scope" in result.output.lower()


def test_no_uid_flag_keeps_the_1k_baseline_auth_count(runner, use_link):
    # mirrors the Fase 0/2 baseline: the Fase 3 plumbing must not change a single
    # auth when no scheme is selected.
    fake = use_link(mifare_session, mc.CardLink(mc.card_1k()))
    result = runner.invoke(
        mifare_check_cmd, ["--sectors", "16", "--key-type", "A", "--json"]
    )
    auths = sum(1 for line in fake.sent if line.startswith("mifare auth"))
    assert auths == 2514, auths
    assert result.exit_code == 1
