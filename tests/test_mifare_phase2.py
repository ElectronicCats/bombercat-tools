#!/usr/bin/env python3

# Electronic Cats
# test_mifare_phase2.py — Fase 2 of PLAN_Mejoras_MIFARE.md: named key
# dictionaries by vendor/sector. Proves the new selection/ordering layer
# (dicts.py) and its `check` wiring:
#   - families are selectable and combine without breaking the current use;
#   - the combined queue is ordered by probability (default → app → vendor),
#     with MAD keys tried first on the MAD sectors;
#   - selecting a vendor family increases coverage on a card that only opens
#     with a vendor key (Fase 2 success criterion);
#   - selecting nothing is byte-for-byte the pre-Fase-2 behaviour.
#
# No PN7150 is involved: every command runs through mifare_card.CardLink.

import json

from modules.tags.mifare import dicts
from modules.tags.mifare.check import mifare_check_cmd
from modules.tags.mifare.keyfile import default_keyfile, load_keys
from modules.tags.mifare import session as mifare_session

import mifare_card as mc

# In locks.dic, absent from the bundled default dictionary — the whole point of
# the vendor family. Guarded by test_vendor_key_is_a_true_vendor_only_key below.
VENDOR_KEY = "A1B2C3D4E5F6"


# ── the registry / selection layer ────────────────────────────────────────────


def test_registry_files_all_exist_and_load():
    for name, nd in dicts.REGISTRY.items():
        assert nd.path().is_file(), name
        assert nd.load(), f"{name} loaded no keys"


def test_select_orders_by_tier_then_registry_order():
    # given out of order and across several --dict tokens, comes back
    # default(0) → app(1) → vendor(2).
    sel = dicts.select(["locks,mad", "transport"])
    assert [d.name for d in sel] == ["transport", "mad", "locks"]


def test_select_all_expands_and_dedups():
    sel = dicts.select(["all", "mad"])  # 'all' + a repeat must not duplicate
    names = [d.name for d in sel]
    assert set(names) == set(dicts.REGISTRY)
    assert len(names) == len(set(names)) == len(dicts.REGISTRY)
    # still tier-ordered (non-decreasing tier across the list)
    tiers = [d.tier for d in sel]
    assert tiers == sorted(tiers)


def test_select_rejects_unknown_names_loudly():
    try:
        dicts.select(["mad,bogus"])
    except dicts.UnknownDictError as e:
        assert "bogus" in str(e)
    else:
        raise AssertionError("expected UnknownDictError")


# ── the candidate plan (ordering + sector bias) ───────────────────────────────


def test_empty_plan_is_the_base_dictionary_unchanged():
    base = ["FFFFFFFFFFFF", "A0A1A2A3A4A5", "000000000000"]
    plan = dicts.CandidatePlan(base)
    # same object order for every sector, byte-identical to base -> no baseline move
    assert plan.for_sector(0) == base
    assert plan.for_sector(9) == base
    assert plan.size == len(base)


def test_mad_keys_are_tried_first_on_the_mad_sectors_only():
    base = ["FFFFFFFFFFFF"]
    plan = dicts.CandidatePlan(base, dicts.select(["mad", "transport"]))
    # sector 0 and 16 (MAD / MAD2): the MAD key jumps ahead of tier-0 transport.
    assert plan.for_sector(0)[0] == "A0A1A2A3A4A5"
    assert plan.for_sector(16)[0] == "A0A1A2A3A4A5"
    # elsewhere: default/transport keys come first, MAD falls back to its tier.
    order5 = plan.for_sector(5)
    assert order5[0] == "FFFFFFFFFFFF"
    assert order5.index("FFFFFFFFFFFF") < order5.index("A0A1A2A3A4A5")


def test_named_and_extra_keys_are_deduped_against_the_base():
    base = ["FFFFFFFFFFFF", VENDOR_KEY]  # base already holds the vendor key
    plan = dicts.CandidatePlan(base, dicts.select(["locks"]), extra=["FFFFFFFFFFFF"])
    seq = plan.for_sector(3)
    assert seq.count(VENDOR_KEY) == 1
    assert seq.count("FFFFFFFFFFFF") == 1


# ── coverage: a vendor family opens a card the base dictionary cannot ──────────


def test_vendor_key_is_a_true_vendor_only_key():
    # The premise of the coverage test: this key is in locks.dic but NOT in the
    # bundled default dictionary. If someone adds it to the base, this fails
    # loudly instead of the coverage test silently passing for the wrong reason.
    base = {k.upper() for k in load_keys([str(default_keyfile())])}
    locks = {k.upper() for k in dicts.REGISTRY["locks"].load()}
    assert VENDOR_KEY in locks
    assert VENDOR_KEY not in base


def _vendor_card():
    """A 1K whose sector 15 opens only with VENDOR_KEY (a locks.dic key absent
    from the base dict); every other sector uses factory defaults."""
    b0 = mc._block0("DECAFBAD", "08", "0400")
    sectors = [
        mc.Sector(
            mc.KEY_MAD, mc.KEY_DEFAULT, mc.AC_TRANSPORT, [b0, "00" * 16, "00" * 16]
        )
    ]
    for _ in range(1, 15):
        sectors.append(
            mc.Sector(mc.KEY_DEFAULT, mc.KEY_DEFAULT, mc.AC_TRANSPORT, ["00" * 16] * 3)
        )
    sectors.append(mc.Sector(VENDOR_KEY, VENDOR_KEY, mc.AC_TRANSPORT, ["22" * 16] * 3))
    return mc.Card("DECAFBAD", "08", "0400", sectors)


def _check(runner, use_link, card, args):
    use_link(mifare_session, mc.CardLink(card))
    return runner.invoke(mifare_check_cmd, args)


def test_base_dictionary_cannot_open_the_vendor_sector(runner, use_link):
    result = _check(
        runner,
        use_link,
        _vendor_card(),
        ["--sectors", "16", "--key-type", "A", "--json"],
    )
    payload = json.loads(result.stdout)
    by_sector = {s["sector"]: s for s in payload["sectors"]}
    assert by_sector[15]["key_a"] is None  # declared gap, never guessed
    assert result.exit_code == 1


def test_locks_family_opens_the_vendor_sector(runner, use_link):
    result = _check(
        runner,
        use_link,
        _vendor_card(),
        ["--sectors", "16", "--key-type", "A", "--dict", "locks", "--json"],
    )
    payload = json.loads(result.stdout)
    by_sector = {s["sector"]: s for s in payload["sectors"]}
    assert by_sector[15]["key_a"] == VENDOR_KEY  # coverage gained
    assert payload["recovered"] == 16
    assert result.exit_code == 0  # every requested key recovered


def test_dict_file_also_opens_the_vendor_sector(runner, use_link, tmp_path):
    # the ad-hoc path: an unregistered file passed straight in.
    extra = tmp_path / "mine.dic"
    extra.write_text(f"# my engagement keys\n{VENDOR_KEY}\n")
    result = _check(
        runner,
        use_link,
        _vendor_card(),
        ["--sectors", "16", "--key-type", "A", "--dict-file", str(extra), "--json"],
    )
    payload = json.loads(result.stdout)
    assert {s["sector"]: s["key_a"] for s in payload["sectors"]}[15] == VENDOR_KEY
    assert result.exit_code == 0


# ── CLI surface: --list-dicts and bad input, no device needed ─────────────────


def test_list_dicts_prints_every_family_and_exits_zero(runner):
    result = runner.invoke(mifare_check_cmd, ["--list-dicts"])
    assert result.exit_code == 0
    for name in dicts.REGISTRY:
        assert name in result.stdout


def test_unknown_dict_name_fails_before_touching_the_device(runner):
    # no use_link: if it reached a session it would blow up differently.
    result = runner.invoke(
        mifare_check_cmd, ["--dict", "nope", "--sectors", "1", "--json"]
    )
    assert result.exit_code == 1
    assert "nope" in result.output


# ── regression: opting into nothing keeps the pinned baseline ─────────────────


def test_no_dict_flags_keep_the_1k_baseline_auth_count(runner, use_link):
    # mirrors test_mifare_phase0.test_baseline_1k_auth_counts_key_type_a: the
    # Fase 2 plumbing must not change a single auth when no family is selected.
    fake = use_link(mifare_session, mc.CardLink(mc.card_1k()))
    result = runner.invoke(
        mifare_check_cmd, ["--sectors", "16", "--key-type", "A", "--json"]
    )
    auths = sum(1 for line in fake.sent if line.startswith("mifare auth"))
    assert auths == 2514, auths
    assert result.exit_code == 1  # sector 15 still a genuine gap
