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
    """Today NFCGate is the only board that answers the control handshake."""
    repl = {f.id for f in fw.repl_firmwares()}
    assert repl == {"nfcgate"}
    assert all(f.has_repl for f in fw.repl_firmwares())


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


def test_enrichment_fills_descriptions_when_available():
    descriptions = fw.load_descriptions()
    if not descriptions:
        pytest.skip("descriptions.json not locatable in this environment")
    nfcgate = fw.by_id("nfcgate")
    enriched = {f.id: f for f in fw.all_firmwares(enrich=True)}
    assert enriched["nfcgate"].description  # non-empty prose
    assert not nfcgate.description  # the base entry stays prose-free
