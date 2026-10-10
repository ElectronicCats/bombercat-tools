#!/usr/bin/env python3

# Electronic Cats
# test_emvy_link.py — EmvyLink, the operative-dialect serial client for
# EMVyBomberCat (modules/emvy/link.py). Driven against the scripted FakeSerial
# (conftest) so the WAIT/APDU:/RESP:/SCAN/EMU: flows and their error and timeout
# branches are covered with no board attached. Fase 1 of
# docs/IMPLEMENTATION_PLAN_EMVyBomberCat_CLI.md.

import pytest
import serial

from typing import List, Optional

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


# ── stream_until(): reconnect after an MCU reset ─────────────────────────────
#
# The EMVyBomberCat MCU resets when USB re-enumerates (or on some errors), which
# kills the open fd with a SerialException mid-stream. With ``restart_cmd`` the
# link reopens the port, re-sends the trigger and keeps streaming; without it
# the error propagates as before.


class _FlakySerial(FakeSerial):
    """Fails one readline() mid-stream with a SerialException (the open fd
    dying when the MCU resets), reading normally otherwise."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_on_read: Optional[int] = None
        self._reads = 0

    def readline(self, size: int = -1) -> bytes:
        self._reads += 1
        if self._reads == self.fail_on_read:
            raise serial.SerialException("device disconnected")
        return super().readline(size)


def _patched_open(monkeypatch, *serials):
    """open_serial() returning the given serials in order, then raising."""
    pending = list(serials)

    def _open(*args, **kwargs):
        if pending:
            return pending.pop(0)
        raise serial.SerialException("no such device")

    monkeypatch.setattr(link_mod, "open_serial", _open)


def test_stream_until_reconnects_and_resends_the_trigger(monkeypatch):
    monkeypatch.setattr(link_mod.time, "sleep", lambda _s: None)
    flaky = _FlakySerial({})
    fresh = FakeSerial(
        {"EMUEMV:RAM|80": ["# relaunched", "EMU:DONE sent=0 reason=stop"]}
    )
    _patched_open(monkeypatch, flaky, fresh)

    link = EmvyLink("/dev/fake0", timeout=0.2).open()
    flaky.feed("EMU:START mode=emv")
    flaky.fail_on_read = 2  # first line lands, then the fd dies mid-stream

    seen: List[str] = []
    lines = link.stream_until(
        "EMU:DONE", on_line=seen.append, restart_cmd="EMUEMV:RAM|80"
    )

    # The pre-reset line was delivered, the reconnect was reported, and the
    # session resumed on the fresh port — ending at the sentinel.
    assert seen[0] == "EMU:START mode=emv"
    assert any(l.startswith("# RECONNECT attempt=1/") for l in seen)
    assert "# RECONNECT ok: resuming the session" in seen
    assert lines[-1] == "EMU:DONE sent=0 reason=stop"
    # The trigger was re-sent on the reopened port.
    assert fresh.written == ["EMUEMV:RAM|80"]


def test_stream_until_reconnect_notice_goes_to_stderr_without_on_line(
    monkeypatch, capsys
):
    monkeypatch.setattr(link_mod.time, "sleep", lambda _s: None)
    flaky = _FlakySerial({})
    fresh = FakeSerial({"EMU:": ["EMU:DONE sent=0 reason=stop"]})
    _patched_open(monkeypatch, flaky, fresh)

    link = EmvyLink("/dev/fake0", timeout=0.2).open()
    flaky.fail_on_read = 1

    lines = link.stream_until("EMU:DONE", restart_cmd="EMU:")
    assert lines[-1] == "EMU:DONE sent=0 reason=stop"
    assert "# RECONNECT attempt=1/" in capsys.readouterr().err


def test_stream_until_gives_up_when_the_port_never_comes_back(monkeypatch):
    monkeypatch.setattr(link_mod.time, "sleep", lambda _s: None)
    flaky = _FlakySerial({})
    flaky.fail_on_read = 1
    _patched_open(monkeypatch, flaky)  # the first open works, every retry fails

    link = EmvyLink("/dev/fake0", timeout=0.2).open()
    seen: List[str] = []
    with pytest.raises(EmvyError, match="link not open"):
        link.stream_until(
            "EMU:DONE", on_line=seen.append, restart_cmd="EMUEMV", max_reconnects=3
        )
    # Every bounded attempt was reported, then the error surfaced.
    assert any(l.startswith("# RECONNECT attempt=3/3") for l in seen)
    assert not any(l.startswith("# RECONNECT ok") for l in seen)


def test_stream_until_fails_fast_without_restart_cmd(monkeypatch):
    # Callers that don't opt in keep the old behavior: a mid-stream serial
    # error propagates immediately, with no reopen attempts.
    monkeypatch.setattr(link_mod.time, "sleep", lambda _s: None)
    flaky = _FlakySerial({})
    flaky.fail_on_read = 1
    _patched_open(monkeypatch, flaky, _FlakySerial({}))

    link = EmvyLink("/dev/fake0", timeout=0.2).open()
    with pytest.raises(EmvyError, match="serial error"):
        link.stream_until("EMU:DONE")


# ── lifecycle ────────────────────────────────────────────────────────────────


def test_context_manager_opens_and_closes(linked, monkeypatch):
    ser = FakeSerial({"TAG": ["TAG:ISO14443A UID:0102"]})
    monkeypatch.setattr(link_mod, "open_serial", lambda *a, **k: ser)
    with EmvyLink("/dev/fake0", timeout=0.2) as link:
        assert link.exchange("TAG", terminators=("TAG:",)) == ["TAG:ISO14443A UID:0102"]
    assert ser.closed
