#!/usr/bin/env python3

# Electronic Cats
# test_core_protocol.py — DeviceLink, the line protocol the CLI speaks to the
# board (modules/core/bombercat.py). Unit-level counterpart to
# serialctl_hosttest.py: the same rules, but against a scripted FakeSerial so
# the failure modes a pty can't produce (write timeouts, dropped links) are
# covered too. Mirrors firmware SerialControl.

import pytest
import serial

from conftest import FakeSerial
from modules.core import bombercat as core
from modules.core.bombercat import DeviceError, DeviceLink, Response


@pytest.fixture(autouse=True)
def no_settle_sleep(monkeypatch):
    """`open()` sleeps 0.3 s to let the CDC settle; the fake needs no settling."""
    monkeypatch.setattr(core.time, "sleep", lambda _s: None)


@pytest.fixture
def linked(monkeypatch):
    """Open a DeviceLink on a FakeSerial built from ``script``."""

    def _open(script=None, timeout=0.2, **kwargs):
        ser = FakeSerial(script, **kwargs)
        monkeypatch.setattr(core, "open_serial", lambda *a, **k: ser)
        return DeviceLink("/dev/fake0", timeout=timeout).open(), ser

    return _open


# ── Response ─────────────────────────────────────────────────────────────────


def test_response_is_truthy_when_ok():
    assert bool(Response(ok=True))
    assert not bool(Response(ok=False, message="nope"))


# ── Reply parsing ────────────────────────────────────────────────────────────


def test_command_collects_data_lines_and_ignores_log_noise(linked):
    link, ser = linked(
        {
            "info": [
                "[boot] NFCGate 0.8.0",  # no marker: device log noise
                ":fw 0.8.0",
                ":role reader",
                ":state idle",
                "+OK",
            ]
        }
    )
    r = link.command("info")

    assert r.ok and r.data == {"fw": "0.8.0", "role": "reader", "state": "idle"}
    assert ser.written == ["info"]


def test_readline_calls_are_size_capped_to_bound_memory(linked):
    """A wedged/noisy firmware emitting continuous non-newline bytes must not
    grow memory without bound (M14)."""
    link, ser = linked({"ping": ["+OK bombercat"]})
    link.command("ping")

    assert ser.readline_sizes
    assert all(size == core._MAX_LINE_BYTES for size in ser.readline_sizes)


def test_command_keeps_spaces_in_values(linked):
    link, _ = linked({"info": [":ssid My Home Net", "+OK"]})
    assert link.command("info").data["ssid"] == "My Home Net"


def test_command_reports_error_replies_without_raising(linked):
    link, _ = linked({"set": ["-ERR session must be 1..255"]})
    r = link.set("session", "999")

    assert not r.ok
    assert r.message == "session must be 1..255"


def test_command_returns_data_gathered_before_an_error(linked):
    link, _ = linked({"run": [":detail wifi", "-ERR no ssid"]})
    r = link.run()

    assert not r.ok and r.data == {"detail": "wifi"}


def test_ok_message_is_parsed(linked):
    link, _ = linked({"ping": ["+OK bombercat 0.8.0"]})
    assert link.command("ping").message == "bombercat 0.8.0"


def test_unknown_command_surfaces_as_a_failed_response(linked):
    link, _ = linked()  # FakeSerial answers -ERR unknown command by default
    r = link.command("identify-nope")

    assert not r.ok and "unknown" in r.message


def test_command_drops_stale_input_before_writing(linked):
    """Strict request/response: whatever the board said earlier is discarded."""
    link, ser = linked({"ping": ["+OK bombercat"]})
    ser.feed("[log] leftover chatter")
    assert link.ping()


# ── Failure modes ────────────────────────────────────────────────────────────


def test_command_without_open_link_raises():
    with pytest.raises(DeviceError, match="not open"):
        DeviceLink("/dev/fake0").command("info")


def test_command_times_out_when_no_terminator_arrives(linked):
    link, _ = linked({"info": [":fw 0.8.0"]})  # data line, never a +OK/-ERR
    with pytest.raises(DeviceError, match="timed out"):
        link.command("info", read_timeout=0.05)


def test_write_timeout_reports_a_wedged_device(linked):
    link, _ = linked(write_error=serial.SerialTimeoutException("write timeout"))
    with pytest.raises(DeviceError, match="wedged"):
        link.command("ping")


def test_serial_error_while_sending_is_wrapped(linked):
    link, _ = linked(write_error=serial.SerialException("port gone"))
    with pytest.raises(DeviceError, match="serial error sending"):
        link.command("ping")


def test_serial_error_while_reading_is_wrapped(linked):
    link, _ = linked(read_error=serial.SerialException("unplugged"))
    with pytest.raises(DeviceError, match="serial error reading"):
        link.command("ping")


# ── ping / lifecycle ─────────────────────────────────────────────────────────


def test_ping_true_only_on_the_bombercat_handshake(linked):
    link, _ = linked({"ping": ["+OK bombercat"]})
    assert link.ping() is True


def test_ping_false_when_another_device_answers(linked):
    link, _ = linked({"ping": ["+OK something-else"]})
    assert link.ping() is False


def test_ping_false_on_timeout_instead_of_raising(linked):
    link, _ = linked({"ping": []}, timeout=0.05)
    assert link.ping() is False


def test_context_manager_opens_and_closes_the_port(monkeypatch):
    ser = FakeSerial({"ping": ["+OK bombercat"]})
    monkeypatch.setattr(core, "open_serial", lambda *a, **k: ser)
    with DeviceLink("/dev/fake0") as link:
        assert link.ping()
    assert ser.closed


def test_close_is_idempotent(linked):
    link, ser = linked()
    link.close()
    link.close()
    assert ser.closed


# ── stream ───────────────────────────────────────────────────────────────────


def test_stream_yields_decoded_lines_and_skips_read_timeouts(linked):
    link, ser = linked()
    ser.feed(":apdu cmd 1000 00a4", "RelayEngine: alive")

    lines = []
    for line in link.stream():
        lines.append(line)
        if len(lines) == 2:
            break
    assert lines == [":apdu cmd 1000 00a4", "RelayEngine: alive"]


def test_stream_needs_an_open_link():
    with pytest.raises(DeviceError, match="not open"):
        next(DeviceLink("/dev/fake0").stream())


def test_stream_reports_a_lost_link(linked):
    link, _ = linked(read_error=serial.SerialException("device disconnected"))
    with pytest.raises(DeviceError, match="serial link lost"):
        next(link.stream())


# ── command wrappers ─────────────────────────────────────────────────────────


def test_wrappers_send_the_documented_command_lines(linked):
    link, ser = linked(
        {
            k: ["+OK"]
            for k in ("info", "status", "set", "save", "run", "stop", "identify")
        }
    )
    link.info()
    link.status()
    link.set("ssid", "HomeNet")
    link.save()
    link.run()
    link.stop()
    link.identify()

    assert ser.written == [
        "info",
        "status",
        "set ssid HomeNet",
        "save",
        "run",
        "stop",
        "identify",
    ]
