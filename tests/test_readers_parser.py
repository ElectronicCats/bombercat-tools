#!/usr/bin/env python3

# Electronic Cats
# test_readers_parser.py — ReaderParser (modules/readers/parser.py) turns
# DetectReaders serial output into Reader objects. Structured ":reader"
# events only — no legacy text mode.
# docs/IMPLEMENTATION_PLAN_DetectReaders_CLI.md §5 phase 2.

import pytest

from modules.readers.parser import Reader, ReaderParser


# ── structured ":reader" events ──────────────────────────────────────────────


def test_full_event_with_all_extras_is_parsed():
    parser = ReaderParser()

    reader = parser.feed(
        ":reader 1234 NFC-A ISODEP intf=ISODEP apdu=00A404000E325041592E5359532E4444463031 "
        "label=emv-payment n=3"
    )

    assert reader.ts_ms == 1234
    assert reader.tech == "NFC-A"
    assert reader.protocol == "ISODEP"
    assert reader.intf == "ISODEP"
    assert reader.apdu == "00A404000E325041592E5359532E4444463031"
    assert reader.label == "emv-payment"
    assert reader.n == 3
    assert reader.aid is None
    assert parser.structured is True


def test_minimal_rf_only_event_has_no_apdu():
    parser = ReaderParser()

    reader = parser.feed(
        ":reader 500 NFC-A FRAME intf=UNDETERMINED apdu=- label=unknown n=0"
    )

    assert reader.apdu is None
    assert reader.label == "unknown"
    assert reader.n == 0


def test_select_with_unclassified_aid_surfaces_it():
    parser = ReaderParser()

    reader = parser.feed(
        ":reader 10 NFC-A ISODEP intf=ISODEP apdu=00A4040007A000000000FF "
        "aid=A000000000FF label=unknown n=1"
    )

    assert reader.aid == "A000000000FF"
    assert reader.label == "unknown"


def test_prose_and_noise_lines_are_ignored():
    parser = ReaderParser()

    assert parser.feed(" - LISTEN MODE: Remote reader activated emulated card") is None
    assert parser.feed("\tTechnology: NFC-A") is None
    assert parser.feed("Waiting for a Reader ...") is None
    assert parser.feed("Re-armed. Emulation discovery running.") is None
    assert parser.structured is False


def test_non_hex_apdu_is_kept_as_raw_and_field_stays_none():
    parser = ReaderParser()

    reader = parser.feed(":reader 10 NFC-A ISODEP apdu=zzqq label=unknown n=1")

    assert reader.apdu is None
    assert reader.extra["raw_apdu"] == "ZZQQ"


def test_non_hex_aid_is_kept_as_raw_and_field_stays_none():
    parser = ReaderParser()

    reader = parser.feed(":reader 10 NFC-A ISODEP aid=zznotHEX label=unknown n=1")

    assert reader.aid is None
    assert reader.extra["raw_aid"] == "ZZNOTHEX"


def test_crlf_is_tolerated():
    parser = ReaderParser()

    reader = parser.feed(":reader 10 NFC-A ISODEP label=unknown n=0\r\n")

    assert reader is not None
    assert reader.ts_ms == 10


def test_unknown_extra_kv_tokens_are_kept_in_extra():
    parser = ReaderParser()

    reader = parser.feed(":reader 10 NFC-A ISODEP label=unknown n=0 rssi=-40")

    assert reader.extra == {"rssi": "-40"}


def test_keyless_extra_token_is_skipped():
    parser = ReaderParser()

    reader = parser.feed(":reader 10 NFC-A ISODEP =oops label=unknown n=0")

    assert "=oops" not in reader.extra
    assert reader.label == "unknown"


def test_structured_mode_is_sticky():
    parser = ReaderParser()
    parser.feed(":reader 10 NFC-A ISODEP label=unknown n=0")

    assert parser.structured is True
    assert parser.feed("some unrelated prose") is None
    assert parser.structured is True


# ── Reader.pretty_apdu / fingerprint ─────────────────────────────────────────


def test_pretty_apdu_formats_bytes_with_spaces():
    assert Reader(apdu="00A404000E").pretty_apdu == "00 A4 04 00 0E"


def test_pretty_apdu_dash_when_no_apdu():
    assert Reader(apdu=None).pretty_apdu == "-"


def test_fingerprint_uses_label_and_aid_when_known():
    r = Reader(label="visa", aid="A0000000031010", tech="NFC-A", protocol="ISODEP")
    assert r.fingerprint == "visa:A0000000031010"


def test_fingerprint_for_emv_payment_has_no_aid():
    r = Reader(label="emv-payment", aid=None, tech="NFC-A", protocol="ISODEP")
    assert r.fingerprint == "emv-payment"


def test_fingerprint_for_unknown_label_includes_tech_and_protocol():
    r1 = Reader(label="unknown", tech="NFC-A", protocol="ISODEP")
    r2 = Reader(label="unknown", tech="NFC-B", protocol="FRAME")
    assert r1.fingerprint == "unknown:NFC-A:ISODEP"
    assert r2.fingerprint == "unknown:NFC-B:FRAME"
    assert r1.fingerprint != r2.fingerprint
