#!/usr/bin/env python3

# Electronic Cats
# test_mifare_phase1.py — Fase 1 of PLAN_Mejoras_MIFARE.md: propagation by key
# reuse. The known-first reuse, try-as-A-and-B, dedup and per-sector early cut
# were already in check.py (see the Fase 0 finding); this file pins the ONE
# remaining task — exposing the "authentications saved vs. a naive sweep of the
# same candidate universe" statistic — and proves it is honest:
#   - the naive baseline equals sectors x universe (matching the Fase 0 pins);
#   - saved = naive - attempts, always non-negative, and strictly positive on a
#     card with reused keys (the reuse shortcut is what saves them);
#   - turning on every family + a UID scheme keeps attempts a tiny fraction of
#     the (now much larger) naive sweep — the Fase 1 win made visible;
#   - coverage is unchanged: the stat is purely additive, and the private
#     sector 15 stays a declared gap, never guessed.
#
# No PN7150: check runs through mifare_card.CardLink. No key is ever guessed.

import json

from modules.tags.mifare.check import mifare_check_cmd
from modules.tags.mifare.dicts import CandidatePlan, select
from modules.tags.mifare.keyfile import default_keyfile, load_keys
from modules.tags.mifare import session as mifare_session

import mifare_card as mc


def _run(runner, use_link, args, card):
    fake = use_link(mifare_session, mc.CardLink(card))
    return runner.invoke(mifare_check_cmd, args), fake


def _auths(fake):
    return sum(1 for line in fake.sent if line.startswith("mifare auth"))


def _base_universe() -> int:
    return CandidatePlan(load_keys([str(default_keyfile())]), [], []).size


def _stats(result) -> dict:
    return json.loads(result.stdout)["stats"]


# ── the statistic is present, self-consistent, and matches the auth counter ─────


def test_stats_block_is_present_and_self_consistent(runner, use_link):
    r, fake = _run(
        runner, use_link, ["--sectors", "16", "--key-type", "A", "--json"], mc.card_1k()
    )
    st = _stats(r)
    # attempts equals the auths actually issued (the Fase 0 counter's own view).
    assert st["attempts"] == _auths(fake)
    # saved is exactly the gap between the naive sweep and what we issued.
    assert st["auths_saved"] == st["naive_attempts"] - st["attempts"]
    assert 0 <= st["attempts"] <= st["naive_attempts"]
    assert st["interrupted"] is False


def test_naive_baseline_is_sectors_times_universe(runner, use_link):
    # No --dict/--uid-derived, key-type A: the naive sweep is one full base
    # dictionary per sector — the same 16 x universe the Fase 5 review uses.
    r, _ = _run(
        runner, use_link, ["--sectors", "16", "--key-type", "A", "--json"], mc.card_1k()
    )
    st = _stats(r)
    assert st["naive_attempts"] == 16 * _base_universe()
    # attempts stays on the Fase 0 baseline — the stat is observational only.
    assert st["attempts"] == 2514


def test_reuse_saves_authentications_on_a_card_with_reused_keys(runner, use_link):
    # card_1k reuses the NDEF key across sectors 8-14 and the default key across
    # 1-7: known-first reuse + early cut must save a positive, sizeable number.
    r, _ = _run(
        runner, use_link, ["--sectors", "16", "--key-type", "A", "--json"], mc.card_1k()
    )
    st = _stats(r)
    assert st["auths_saved"] > 0
    assert st["saved_pct"] > 0.0


# ── the Fase 1 win: candidate expansion is absorbed, made visible by the stat ───


def test_expansion_shows_up_as_a_high_saved_percentage(runner, use_link):
    # Turn on every family + a UID scheme: the naive sweep balloons (16 x a much
    # larger universe) but attempts barely move, so nearly all of it is "saved".
    r, _ = _run(
        runner,
        use_link,
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
        ],
        mc.card_1k(),
    )
    st = _stats(r)
    expanded = CandidatePlan(
        load_keys([str(default_keyfile())]), select(["all"]), []
    ).size
    # naive grew with the universe; attempts stayed a tiny bounded delta (2527,
    # pinned in Fase 5), so >90% of the naive sweep is avoided.
    assert st["naive_attempts"] >= 16 * expanded
    assert st["attempts"] == 2527
    assert st["saved_pct"] > 90.0


# ── honesty: the stat changes nothing about coverage or the declared gap ────────


def test_stat_does_not_change_coverage_or_hide_the_gap(runner, use_link):
    r, _ = _run(
        runner, use_link, ["--sectors", "16", "--key-type", "A", "--json"], mc.card_1k()
    )
    doc = json.loads(r.stdout)
    # sector 15 is a genuine gap: never guessed, reported blank, exit non-zero.
    assert doc["sectors"][15]["key_a"] is None
    assert r.exit_code == 1
    # the rest opened with known/public keys — coverage untouched by the stat.
    assert doc["recovered"] == 15


def test_saved_stat_appears_in_the_human_readable_summary(runner, use_link):
    r, _ = _run(runner, use_link, ["--sectors", "16", "--key-type", "A"], mc.card_1k())
    assert "auths saved" in r.stdout
    assert "naive sweep" in r.stdout
