#!/usr/bin/env python3

# Electronic Cats
# test_emvy_parser.py — the EMVyBomberCat operative-dialect parsers
# (modules/emvy/parser.py). Pure functions, no serial: they turn the raw reply
# lines EmvyLink collects into Python values, and raise EmvyError on the
# firmware's `ERR:*` branches. Fase 1 of
# docs/IMPLEMENTATION_PLAN_EMVyBomberCat_CLI.md.

import pytest

from modules.emvy.parser import (
    EmvyError,
    parse_apdu_resp,
    parse_cardscan,
    parse_emu_event,
    parse_scan_json,
    parse_tag,
)


# ── APDU passthrough ─────────────────────────────────────────────────────────


def test_parse_apdu_resp_returns_bytes():
    lines = ["# passthrough armed", "RESP:6F1A840E325041592E5359"]
    assert parse_apdu_resp(lines) == bytes.fromhex("6F1A840E325041592E5359")


def test_parse_apdu_resp_ignores_logs_before_resp():
    assert parse_apdu_resp(["# waiting", "# tx", "RESP:9000"]) == b"\x90\x00"


@pytest.mark.parametrize("reason", ["NOCARD", "BADAPDU", "TXFAIL"])
def test_parse_apdu_resp_raises_on_err(reason):
    with pytest.raises(EmvyError) as exc:
        parse_apdu_resp([f"ERR:{reason}"])
    assert reason in str(exc.value)


def test_parse_apdu_resp_raises_on_bad_hex():
    with pytest.raises(EmvyError):
        parse_apdu_resp(["RESP:ZZZZ"])


def test_parse_apdu_resp_raises_when_no_resp():
    with pytest.raises(EmvyError):
        parse_apdu_resp(["# nothing here"])


# ── SCAN → JSON ──────────────────────────────────────────────────────────────


def test_parse_scan_json_extracts_block():
    lines = [
        "# starting EMV flow",
        "JSON_START",
        '{"pan": "4111111111111111",',
        '"expiry": "2812"}',
        "JSON_END",
        "# done",
    ]
    out = parse_scan_json(lines)
    assert out == {"pan": "4111111111111111", "expiry": "2812"}


def test_parse_scan_json_single_line_block():
    lines = ["JSON_START", '{"aid": "A0000000031010"}', "JSON_END"]
    assert parse_scan_json(lines) == {"aid": "A0000000031010"}


def test_parse_scan_json_raises_on_error_log():
    with pytest.raises(EmvyError) as exc:
        parse_scan_json(["# ...", "# ERROR: Timeout"])
    assert "Timeout" in str(exc.value)


def test_parse_scan_json_raises_when_no_block():
    with pytest.raises(EmvyError):
        parse_scan_json(["# only logs", "# more logs"])


def test_parse_scan_json_raises_on_malformed_json():
    with pytest.raises(EmvyError):
        parse_scan_json(["JSON_START", "{not json", "JSON_END"])


# ── TAG ──────────────────────────────────────────────────────────────────────


def test_parse_tag():
    assert parse_tag("TAG:ISO14443A UID:04A2B3C4") == {
        "proto": "ISO14443A",
        "uid": "04A2B3C4",
    }


def test_parse_tag_is_case_insensitive_on_marker():
    assert parse_tag("tag:NFC-F uid:0102")["uid"] == "0102"


def test_parse_tag_raises_on_err():
    with pytest.raises(EmvyError):
        parse_tag("ERR:NOCARD")


def test_parse_tag_raises_on_garbage():
    with pytest.raises(EmvyError):
        parse_tag("something unrelated")


# ── CARDSCAN ─────────────────────────────────────────────────────────────────


def test_parse_cardscan_fields():
    line = "EMU:SCANNED aid=A0000000031010 pan=4111111111111111 exp=2812 t2=41111D2812"
    assert parse_cardscan(line) == {
        "aid": "A0000000031010",
        "pan": "4111111111111111",
        "exp": "2812",
        "t2": "41111D2812",
    }


def test_parse_cardscan_raises_on_err():
    with pytest.raises(EmvyError):
        parse_cardscan("ERR:SCANFAIL")


# ── EMU streaming events ─────────────────────────────────────────────────────


def test_parse_emu_event_rx_hex():
    ev = parse_emu_event("EMU:RX 00A404000E")
    assert ev == {"kind": "RX", "raw": "00A404000E", "hex": "00A404000E"}


def test_parse_emu_event_start_with_fields():
    ev = parse_emu_event("EMU:START mode=emv")
    assert ev["kind"] == "START"
    assert ev["fields"] == {"mode": "emv"}


def test_parse_emu_event_done_sent_count():
    ev = parse_emu_event("EMU:DONE sent=3")
    assert ev["kind"] == "DONE"
    assert ev["fields"] == {"sent": "3"}


def test_parse_emu_event_ignores_non_emu_line():
    assert parse_emu_event("# just a log") is None
