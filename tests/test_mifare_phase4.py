#!/usr/bin/env python3

# Electronic Cats
# test_mifare_phase4.py — Fase 4 of PLAN_Mejoras_MIFARE.md: the offline
# dump analyzer (`mifare analyze`, Mejora B). Proves:
#   - MAD1/MAD2 decode + AN10787 CRC validation (round-trips the fixture encoder);
#   - value blocks are told apart from data by their exact ±value/addr layout;
#   - weak (default/known) keys vs. custom keys are classified, with a keyfile
#     disambiguating a genuine all-zero key from a hidden one;
#   - insecure access bits are flagged (transport) and hardened trailers are not;
#   - gaps (unread / failed sectors) are ALWAYS declared and never called secure;
#   - the CLI emits parseable JSON and fails loudly on a non-dump file.
#
# 100% offline — no PN7150, no CardLink even: the analyzer only reads JSON a
# `mifare dump --out` already wrote. No key is ever guessed.

import copy
import json
from pathlib import Path

from modules.tags.mifare.analyze import (
    analyze_dump,
    detect_value_block,
    mifare_analyze_cmd,
)
from modules.tags.mifare.mad import crc8_mad, parse_mad
from modules.tags.mifare.keyfile import default_keyfile, load_keys, load_sector_keys

import mifare_card as mc

FIXTURES = Path(__file__).parent / "fixtures" / "mifare"
DEFAULT_KEYS = set(load_keys([str(default_keyfile())]))


def _dump(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _report(name, keyfile=None):
    kf = load_sector_keys(str(FIXTURES / keyfile)) if keyfile else None
    return analyze_dump(_dump(name), DEFAULT_KEYS, kf, source=name)


def _sector(report, n):
    return next(s for s in report["sectors"] if s["sector"] == n)


# ── MAD decode + CRC ────────────────────────────────────────────────────────────


def test_mad1_decodes_the_allocated_sectors():
    mad = _report("card_1k")["mad"]
    assert mad is not None and mad["version"] == 1 and mad["crc_valid"]
    allocated = {e["sector"]: e["aid"] for e in mad["entries"] if e["allocated"]}
    # card_1k() built its MAD with {1: 0x0004, 4: 0x1234}.
    assert allocated == {1: "0004", 4: "1234"}


def test_mad2_present_and_valid_on_4k():
    mad2 = _report("card_4k")["mad2"]
    assert mad2 is not None and mad2["version"] == 2 and mad2["crc_valid"]


def test_mad_crc_mismatch_is_reported_not_hidden():
    dump = _dump("card_1k")
    # Corrupt a MAD body byte (not the CRC byte itself) -> CRC must fail, but the
    # entries are still decoded so the operator sees what's claimed.
    blk = dump["sectors"][0]["blocks"][1]
    dump["sectors"][0]["blocks"][1] = "FF" + blk[2:]  # was 80 (CRC); change byte 1
    mad = analyze_dump(dump, DEFAULT_KEYS)["mad"]
    assert mad["crc_valid"] is False
    assert any(e["allocated"] for e in mad["entries"])  # still decoded


def test_crc8_mad_roundtrips_the_encoder():
    b1, b2 = mc.mad1_blocks({1: 0x0004, 4: 0x1234})
    body = bytes.fromhex(b1 + b2)[1:32]
    assert crc8_mad(body) == bytes.fromhex(b1)[0]


def test_parse_mad_rejects_short_blocks():
    assert parse_mad("00", "00" * 16) is None


# ── value blocks ────────────────────────────────────────────────────────────────


def test_value_block_detected_with_correct_value_and_address():
    s4 = _sector(_report("card_1k"), 4)
    assert s4["value_blocks"] == [
        {"block": 16, "index": 0, "value": 1000, "address": 0x10}
    ]


def test_detect_value_block_unit():
    assert detect_value_block(mc.value_block(1000, 0x10)) == (1000, 0x10)
    assert detect_value_block(mc.value_block(-5, 0x02)) == (-5, 0x02)  # signed
    assert detect_value_block("00" * 16) is None  # all-zero data is not a value block
    assert detect_value_block("11" * 16) is None
    assert detect_value_block("zz") is None  # not hex


def test_manufacturer_block_is_never_a_value_block():
    # sector 0 block 0 is UID/manufacturer data; it must be skipped even if it
    # ever coincidentally matched the layout.
    assert _sector(_report("card_1k"), 0)["value_blocks"] == []


# ── weak vs. custom keys ────────────────────────────────────────────────────────


def test_default_keys_are_weak_and_private_key_is_custom():
    r = _report("card_1k")
    # sectors 0-14 use public/default keys (MAD, factory, NDEF); 15 is private.
    assert r["summary"]["weak_key_sectors"] == list(range(15))
    assert r["summary"]["custom_key_sectors"] == [15]
    s15 = _sector(r, 15)
    assert s15["key_a_class"] == "custom" and s15["weak_key"] is False


def test_keyfile_disambiguates_a_genuine_all_zero_key(tmp_path):
    # A trailer whose key A reads back as all-zeros is "not recovered" on its
    # own, but a keyfile that lists 000000000000 makes it a real default key.
    dump = _dump("card_1k")
    dump["sectors"][1]["blocks"][3] = "0" * 12 + "FF078069" + "FFFFFFFFFFFF"

    no_kf = analyze_dump(dump, DEFAULT_KEYS)
    assert _sector(no_kf, 1)["key_a_class"] == "not_recovered"

    kf = tmp_path / "k.keys"
    kf.write_text("1:000000000000:FFFFFFFFFFFF\n")
    with_kf = analyze_dump(dump, DEFAULT_KEYS, load_sector_keys(str(kf)))
    assert _sector(with_kf, 1)["key_a_class"] == "default"  # 000000000000 is a default


# ── access bits ──────────────────────────────────────────────────────────────────


def test_transport_sectors_flag_issues_and_hardened_ones_do_not():
    r = _report("card_1k")
    s0 = _sector(r, 0)  # transport trailer
    assert s0["access"]["issues"]  # key B readable + unprotected data blocks
    assert any("key B is readable" in i for i in s0["access"]["issues"])
    s8 = _sector(r, 8)  # hardened trailer
    assert s8["access"]["issues"] == []
    assert 0 in r["summary"]["insecure_access_sectors"]
    assert 8 not in r["summary"]["insecure_access_sectors"]


def test_invalid_access_bits_are_flagged():
    dump = _dump("card_1k")
    # Break the AC bytes so they fail their own inverse check.
    tr = dump["sectors"][1]["blocks"][3]
    dump["sectors"][1]["blocks"][3] = tr[:12] + "000000" + tr[18:]
    s1 = _sector(analyze_dump(dump, DEFAULT_KEYS), 1)
    assert any("inverse check" in i for i in s1["access"]["issues"])


# ── gaps: never concluded "secure" ───────────────────────────────────────────────


def test_absent_sector_is_declared_a_gap():
    dump = _dump("card_1k")
    dump["sectors"] = [s for s in dump["sectors"] if s["sector"] != 7]
    r = analyze_dump(dump, DEFAULT_KEYS)
    assert {"sector": 7, "reason": "not present in dump (never read)"} in r["gaps"]
    assert 7 in r["summary"]["gap_sectors"]
    assert all(s["sector"] != 7 for s in r["sectors"])  # not analyzed, only declared


def test_failed_sector_reason_is_carried_into_gaps():
    dump = _dump("card_1k")
    dump["sectors"] = [s for s in dump["sectors"] if s["sector"] != 9]
    dump["failed_sectors"] = [{"sector": 9, "reason": "no key available"}]
    r = analyze_dump(dump, DEFAULT_KEYS)
    assert {"sector": 9, "reason": "no key available"} in r["gaps"]


def test_dictionary_view_declares_the_private_sector_gap():
    # The honest `check` result (dictionary keyfile) leaves sector 15 blank; a
    # dump built from it would carry that gap. Here we simulate the keyfile path:
    # the .dict.keys file blanks sector 15's keys.
    kf = load_sector_keys(str(FIXTURES / "card_1k.dict.keys"))
    r = analyze_dump(_dump("card_1k"), DEFAULT_KEYS, kf)
    s15 = _sector(r, 15)
    assert s15["key_a_class"] == "not_recovered"
    assert s15["key_b_class"] == "not_recovered"


# ── CLI surface ──────────────────────────────────────────────────────────────────


def test_cli_json_is_parseable(runner):
    result = runner.invoke(
        mifare_analyze_cmd, [str(FIXTURES / "card_1k.json"), "--json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["uid"] == "DECAFBAD"
    assert payload["summary"]["value_block_count"] == 1


def test_cli_text_runs_and_names_gaps(runner):
    dump = _dump("card_1k")
    dump["sectors"] = [s for s in dump["sectors"] if s["sector"] != 5]
    p = FIXTURES / "._tmp_phase4.json"
    try:
        p.write_text(json.dumps(dump))
        result = runner.invoke(mifare_analyze_cmd, [str(p)])
        assert result.exit_code == 0
        assert "Gaps" in result.output and "sector 5" in result.output
    finally:
        p.unlink(missing_ok=True)


def test_cli_rejects_a_non_dump_file(runner, tmp_path):
    bad = tmp_path / "notadump.json"
    bad.write_text('{"hello": 1}')
    result = runner.invoke(mifare_analyze_cmd, [str(bad)])
    assert result.exit_code == 1
    assert "not a canonical" in result.output


def test_cli_reports_broken_json(runner, tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")
    result = runner.invoke(mifare_analyze_cmd, [str(bad)])
    assert result.exit_code == 1
