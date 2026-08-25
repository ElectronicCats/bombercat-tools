#!/usr/bin/env python3

# Electronic Cats
# test_firmwares.py — the firmware registry (modules/core/firmwares.py) is the
# single source of truth the generalized CLI leans on. These tests keep it from
# drifting away from descriptions.json and keep its internal invariants honest.
# docs/GENERALIZE_CLI_PLAN.md §2.1, §5.1.

import pytest

from modules.core import firmwares as fw


def test_registry_covers_every_uf2_in_descriptions_and_vice_versa():
    """No firmware image goes unregistered, and the registry invents none."""
    descriptions = fw.load_descriptions()
    if not descriptions:
        pytest.skip("descriptions.json not locatable in this environment")

    described = set(descriptions)  # filename.lower() keys
    registered = {f.uf2.lower() for f in fw.all_firmwares(enrich=False)}
    assert registered == described, (
        f"registry drift: only-in-registry={registered - described}, "
        f"only-in-descriptions={described - registered}"
    )


def test_exactly_the_repl_firmwares_are_flagged():
    """Which sketches answer the control handshake, checked against firmware.

    NFCGate carries the full SerialControl; five more embed the small
    BomberCatControl. The legacy relay pair and the ESP32 passthrough include
    neither, so they must stay flagged REPL-less — `_match_banner` and
    `detect_firmware` both lean on this being true.
    """
    repl = {f.id for f in fw.repl_firmwares()}
    assert repl == {
        "nfcgate",
        "detecttags",
        "detectreaders",
        "magspoof",
        "magspoofcvsattack",
        "magspoofmqtt",
        "nfcgate_wifiwebserver",
    }
    assert all(f.has_repl for f in fw.repl_firmwares())


def test_only_repl_firmwares_claim_the_identify_capability():
    """`identify` is a REPL command; a board without one cannot blink on demand."""
    for f in fw.all_firmwares(enrich=False):
        if f.can(fw.CAP_IDENTIFY):
            assert f.has_repl, f"{f.id} claims identify without a REPL"


def test_no_banner_is_claimed_by_two_firmwares():
    """A banner shared by two entries can never identify either of them.

    _match_banner refuses ambiguous output, so a duplicate here does not cause a
    wrong answer — it silently costs both firmwares their only means of being
    recognised. Worth catching as drift instead.
    """
    seen = {}
    for f in fw.all_firmwares(enrich=False):
        for banner in f.banners:
            assert (
                banner not in seen
            ), f"{f.id} and {seen[banner]} both claim the banner {banner!r}"
            seen[banner] = f.id


def test_ids_and_uf2_names_are_unique():
    ids = [f.id for f in fw.all_firmwares(enrich=False)]
    uf2s = [f.uf2.lower() for f in fw.all_firmwares(enrich=False)]
    assert len(ids) == len(set(ids))
    assert len(uf2s) == len(set(uf2s))


def test_lookup_by_uf2_is_case_insensitive():
    assert fw.by_uf2("nfcgate.uf2").id == "nfcgate"
    assert fw.by_uf2("NFCGate.uf2").id == "nfcgate"
    assert fw.by_uf2("does-not-exist.uf2") is None


def test_by_id_falls_back_to_unknown():
    assert fw.by_id("nfcgate").display == "NFCGate"
    assert fw.by_id("no-such-firmware") is fw.UNKNOWN


def test_repl_firmware_has_the_full_capability_set():
    nfcgate = fw.by_id("nfcgate")
    for cap in (fw.CAP_RELAY, fw.CAP_CONFIG, fw.CAP_MONITOR, fw.CAP_IDENTIFY):
        assert nfcgate.can(cap)


def test_detecttags_claims_the_tags_capability():
    detecttags = fw.by_id("detecttags")
    assert detecttags.can(fw.CAP_TAGS)
    assert detecttags.has_repl


def test_only_detecttags_claims_the_tags_capability():
    for f in fw.all_firmwares(enrich=False):
        if f.can(fw.CAP_TAGS):
            assert f.id == "detecttags"


def test_detectreaders_claims_the_readers_capability():
    detectreaders = fw.by_id("detectreaders")
    assert detectreaders.can(fw.CAP_READERS)
    assert detectreaders.has_repl


def test_only_detectreaders_claims_the_readers_capability():
    for f in fw.all_firmwares(enrich=False):
        if f.can(fw.CAP_READERS):
            assert f.id == "detectreaders"


def test_lookup_by_uf2_finds_detectreaders():
    assert fw.by_uf2("DetectReaders.uf2").id == "detectreaders"
    assert fw.by_uf2("detectreaders.uf2").id == "detectreaders"


def test_a_non_object_descriptions_payload_is_ignored_instead_of_crashing():
    """descriptions.json is user-editable, remote-persisted cache data — a
    malformed top-level shape must degrade to "no descriptions", not raise
    AttributeError from `.values()` on a list/int (docs/AUDIT_ERROR_HANDLING.md
    M2)."""
    assert fw._parse_descriptions(b"[1, 2, 3]") == {}
    assert fw._parse_descriptions(b"42") == {}


def test_enrichment_fills_descriptions_when_available():
    descriptions = fw.load_descriptions()
    if not descriptions:
        pytest.skip("descriptions.json not locatable in this environment")
    nfcgate = fw.by_id("nfcgate")
    enriched = {f.id: f for f in fw.all_firmwares(enrich=True)}
    assert enriched["nfcgate"].description  # non-empty prose
    assert not nfcgate.description  # the base entry stays prose-free
