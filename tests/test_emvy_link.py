#!/usr/bin/env python3

# Electronic Cats
# test_emvy_link.py — EmvyLink, the operative-dialect serial client for
# EMVyBomberCat (modules/emvy/link.py). Driven against the scripted FakeSerial
# (conftest) so the WAIT/APDU:/RESP:/SCAN/EMU: flows and their error and timeout
# branches are covered with no board attached. Fase 1 of
# docs/IMPLEMENTATION_PLAN_EMVyBomberCat_CLI.md.

import pytest

from conftest import FakeSerial
from modules.emvy import link as link_mod
from modules.emvy.link import EmvyError, EmvyLink
from modules.emvy.parser import parse_apdu_resp, parse_emu_event, parse_scan_json


@pytest.fixture(autouse=True)
def no_settle_sleep(monkeypatch):
    """`open()` sleeps 0.3 s to let the CDC settle; the fake needs no settling."""
    monkeypatch.setattr(link_mod.time, "sleep", lambda _s: None)


@pytest.fixture
def linked(monkeypatch):
    """Open an EmvyLink on a FakeSerial built from ``script``."""

    def _open(script=None, timeout=0.2, **kwargs):
        ser = FakeSerial(script, **kwargs)
        monkeypatch.setattr(link_mod, "open_serial", lambda *a, **k: ser)
        return EmvyLink("/dev/fake0", timeout=timeout).open(), ser

    return _open


# ── exchange() ───────────────────────────────────────────────────────────────


def test_exchange_apdu_resp(linked):
    link, ser = linked({"APDU:00A4040007A0000000031010": ["# tx", "RESP:6F1A9000"]})
    lines = link.exchange("APDU:00A4040007A0000000031010")
    assert lines == ["# tx", "RESP:6F1A9000"]
    assert parse_apdu_resp(lines) == bytes.fromhex("6F1A9000")
    assert ser.written == ["APDU:00A4040007A0000000031010"]


def test_exchange_stops_at_first_terminator(linked):
    # A trailing line after the terminator must not be consumed.
    link, ser = linked({"RELEASE": ["OK", "RESP:LEFTOVER"]})
    lines = link.exchange("RELEASE")
    assert lines == ["OK"]


def test_exchange_wait_ready(linked):
    link, _ = linked({"WAIT": ["READY:"]})
    assert link.exchange("WAIT 30000", timeout=0.5) == ["READY:"]


def test_exchange_err_is_terminal(linked):
    # ERR: ends the exchange; the parser layer turns it into EmvyError.
    link, _ = linked({"WAIT": ["ERR:NOCARD"]})
    assert link.exchange("WAIT 3000") == ["ERR:NOCARD"]


def test_exchange_custom_terminators_for_tag(linked):
    link, _ = linked({"TAG": ["TAG:ISO14443A UID:04A2B3C4"]})
    lines = link.exchange("TAG", terminators=("TAG:", "ERR:"))
    assert lines == ["TAG:ISO14443A UID:04A2B3C4"]


def test_exchange_times_out_without_terminator(linked):
    # Only log noise, never a terminator → exchange must give up on its deadline.
    link, _ = linked({"NFCINFO": ["# chip alive"]}, timeout=0.05)
    with pytest.raises(EmvyError, match="timed out"):
        link.exchange("NFCINFO", terminators=("NFCINFO:",))


def test_exchange_on_closed_link_raises(linked):
    link, _ = linked({})
    link.close()
    with pytest.raises(EmvyError, match="not open"):
        link.exchange("TAG")


# ── stream_until() ───────────────────────────────────────────────────────────


def test_stream_until_scan_stops_at_json_end(linked):
    link, ser = linked({})
    ser.feed(
        "# EMV flow",
        "JSON_START",
        '{"pan": "4111111111111111"}',
        "JSON_END",
        "# trailing",
    )
    lines = link.stream_until("JSON_END")
    assert lines[-1] == "JSON_END"
    assert parse_scan_json(lines) == {"pan": "4111111111111111"}


def test_stream_until_emu_done_and_on_line(linked):
    link, ser = linked({})
    ser.feed("EMU:START mode=ndef", "EMU:RX 00A4", "EMU:TX 9000", "EMU:DONE sent=1")
    events = []
    lines = link.stream_until("EMU:DONE", on_line=lambda l: events.append(l))
    assert lines[-1] == "EMU:DONE sent=1"
    kinds = [parse_emu_event(l)["kind"] for l in events]
    assert kinds == ["START", "RX", "TX", "DONE"]


def test_stream_until_ctrl_c_sends_stop_and_reraises(linked):
    link, ser = linked({})
    ser.feed("EMU:START mode=emv", "EMU:RX 00A4")

    def _interrupt(_line):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        link.stream_until("EMU:DONE", on_line=_interrupt, stop_cmd="STOP")
    assert "STOP" in ser.written


def test_stream_until_times_out_when_sentinel_never_arrives(linked):
    link, ser = linked({})
    ser.feed("EMU:RX 00A4")  # no EMU:DONE ever
    with pytest.raises(EmvyError, match="timed out"):
        link.stream_until("EMU:DONE", timeout=0.05)


# ── lifecycle ────────────────────────────────────────────────────────────────


def test_context_manager_opens_and_closes(linked, monkeypatch):
    ser = FakeSerial({"TAG": ["TAG:ISO14443A UID:0102"]})
    monkeypatch.setattr(link_mod, "open_serial", lambda *a, **k: ser)
    with EmvyLink("/dev/fake0", timeout=0.2) as link:
        assert link.exchange("TAG", terminators=("TAG:",)) == ["TAG:ISO14443A UID:0102"]
    assert ser.closed
