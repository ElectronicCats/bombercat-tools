#!/usr/bin/env python3

# Electronic Cats
# test_cli_capture.py — `bombercat capture` (modules/capture/cli.py): arming the
# device tap, turning ":apdu" events into pcap frames and tearing everything
# down again. Wireshark and the FIFO are stubbed, so the whole path runs on a
# host with neither installed. docs/NFCGATE_PLAN.md Fase 8.

import struct
import threading
from io import BytesIO

import pytest

from conftest import DeviceError, FakeLink, err, flat
from modules.capture import cli as cap
from modules.capture.cli import _APDU_RE, _CaptureSink, _pump, capture
from modules.capture.pcap import EVT_PCD_TO_PICC, EVT_PICC_TO_PCD, PCAP_MAGIC

TRANSCRIPT = [
    ":apdu cmd 1000 00a404000e325041592e5359532e444446303100",
    "RelayEngine: reader alive",  # log noise: not an APDU
    ":apdu resp 1180 6f1a840e325041592e5359532e4444463031a5089000",
]


@pytest.fixture(autouse=True)
def no_wireshark_binary(monkeypatch):
    """Default: no Wireshark installed. Tests that need one say so."""
    monkeypatch.setattr(cap, "find_wireshark_path", lambda: None)


# ── the ":apdu" event regex ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line, direction, ts, payload",
    [
        (":apdu cmd 1000 00a4", "cmd", "1000", "00a4"),
        (":apdu resp 1180 9000", "resp", "1180", "9000"),
        (":apdu cmd 12 00A4B4", "cmd", "12", "00A4B4"),  # upper-case hex
        (":apdu resp 5 ", "resp", "5", ""),  # empty payload, still well formed
    ],
)
def test_apdu_events_are_parsed(line, direction, ts, payload):
    m = _APDU_RE.match(line)
    assert m and m.groups() == (direction, ts, payload)


@pytest.mark.parametrize(
    "line",
    [
        "RelayEngine: reader alive",
        ":state relaying",
        ":apdu both 1000 00a4",  # unknown direction
        ":apdu cmd notatime 00a4",
        ":apdu cmd 1000 zz",  # not hex
    ],
)
def test_non_apdu_lines_are_ignored(line):
    assert _APDU_RE.match(line) is None


# ── _CaptureSink ─────────────────────────────────────────────────────────────


class _FakePipe:
    def __init__(self):
        self.pipe_path = "/tmp/fake.fifo"
        self.packets = []
        self.removed = False
        self.ready_event = threading.Event()
        self.ready_event.set()

    def open(self):
        pass

    def write_packet(self, data):
        self.packets.append(data)

    def remove(self):
        self.removed = True


def test_sink_writes_the_file_header_only_to_the_file():
    """A FIFO with no reader blocks on write, so it gets its header later."""
    pipe, buf = _FakePipe(), BytesIO()
    sink = _CaptureSink(pipe=pipe, fileobj=buf)
    sink.file_header()

    assert struct.unpack("<L", buf.getvalue()[:4])[0] == PCAP_MAGIC
    assert pipe.packets == []


def test_sink_fans_frames_out_to_both_sinks():
    pipe, buf = _FakePipe(), BytesIO()
    sink = _CaptureSink(pipe=pipe, fileobj=buf)
    sink.pipe_header()
    sink.frame(b"frame")

    assert buf.getvalue() == b"frame"
    assert pipe.packets[-1] == b"frame"


def test_sink_without_sinks_is_harmless():
    _CaptureSink().frame(b"frame")  # no file, no pipe: nothing to do


# ── _pump: serial events -> pcap frames ──────────────────────────────────────


def test_pump_converts_apdu_events_into_pcap_frames(capsys):
    buf = BytesIO()
    sink = _CaptureSink(fileobj=buf)
    _pump(FakeLink(stream_lines=TRANSCRIPT), sink)

    data = buf.getvalue()
    # Two records, header-less (the global header is written by the caller).
    first_len = struct.unpack("<L", data[8:12])[0]
    assert data[16 + 1] == EVT_PCD_TO_PICC
    second = data[16 + first_len :]
    assert second[16 + 1] == EVT_PICC_TO_PCD
    assert "2 APDU frame(s)" in capsys.readouterr().out


def test_pump_anchors_device_millis_to_wall_clock(monkeypatch):
    """Frame deltas must match the device's clock (1180 ms - 1000 ms = 0.18 s)."""
    monkeypatch.setattr(cap.time, "time", lambda: 1_700_000_000.0)
    buf = BytesIO()
    _pump(FakeLink(stream_lines=TRANSCRIPT), _CaptureSink(fileobj=buf))

    data = buf.getvalue()
    first_len = struct.unpack("<L", data[8:12])[0]
    sec1, usec1 = struct.unpack("<LL", data[:8])
    sec2, usec2 = struct.unpack("<LL", data[16 + first_len : 16 + first_len + 8])

    assert (sec1, usec1) == (1_700_000_000, 0)
    assert (sec2 - sec1) + (usec2 - usec1) / 1e6 == pytest.approx(0.18)


def test_pump_skips_malformed_hex_without_stopping(capsys):
    buf = BytesIO()
    _pump(
        FakeLink(stream_lines=[":apdu cmd 1000 0a0", ":apdu cmd 1100 00a4"]),
        _CaptureSink(fileobj=buf),
    )

    assert "skipping malformed APDU hex" in flat(capsys.readouterr().out)
    assert len(buf.getvalue()) > 0  # the good frame still made it


def test_pump_reanchors_when_the_device_clock_goes_backwards(monkeypatch, capsys):
    """A board reset / re-enumeration mid-capture makes ts_ms jump backwards;
    the pump must re-anchor to wall-clock 'now' instead of feeding a negative
    delta into the pcap record (M7)."""
    times = iter([1_700_000_000.0, 1_700_000_010.0])
    monkeypatch.setattr(cap.time, "time", lambda: next(times))
    buf = BytesIO()
    _pump(
        FakeLink(stream_lines=[":apdu cmd 5000 00a4", ":apdu cmd 100 00a4"]),
        _CaptureSink(fileobj=buf),
    )

    data = buf.getvalue()
    first_len = struct.unpack("<L", data[8:12])[0]
    sec1, _ = struct.unpack("<LL", data[:8])
    sec2, _ = struct.unpack("<LL", data[16 + first_len : 16 + first_len + 8])

    assert sec1 == 1_700_000_000
    assert sec2 == 1_700_000_010  # re-anchored, not negative
    assert "device clock reset" in flat(capsys.readouterr().out)


def test_pump_ignores_empty_apdus():
    buf = BytesIO()
    _pump(FakeLink(stream_lines=[":apdu cmd 1000 "]), _CaptureSink(fileobj=buf))
    assert buf.getvalue() == b""


def test_pump_falls_back_to_the_file_when_wireshark_closes_the_pipe(capsys):
    class _BrokenPipe(_FakePipe):
        def write_packet(self, data):
            raise BrokenPipeError

    buf = BytesIO()
    sink = _CaptureSink(pipe=_BrokenPipe(), fileobj=buf)
    _pump(FakeLink(stream_lines=TRANSCRIPT), sink)

    assert sink.pipe is None
    assert "continuing to the file only" in flat(capsys.readouterr().out)
    assert len(buf.getvalue()) > 0


def test_pump_stops_when_the_pipe_breaks_and_there_is_no_file(capsys):
    class _BrokenPipe(_FakePipe):
        def write_packet(self, data):
            raise BrokenPipeError

    sink = _CaptureSink(pipe=_BrokenPipe())
    _pump(FakeLink(stream_lines=TRANSCRIPT), sink)

    assert "stopping capture" in flat(capsys.readouterr().out)


# ── capture start ────────────────────────────────────────────────────────────


def test_start_refuses_to_do_nothing(runner):
    result = runner.invoke(capture, ["start"])

    assert result.exit_code == 1
    assert "nothing to do" in flat(result.output)


def test_start_records_to_a_file_and_disarms_the_tap(runner, fake_session, tmp_path):
    link = fake_session(cap, FakeLink(stream_lines=TRANSCRIPT))
    out = tmp_path / "emv.pcap"
    result = runner.invoke(capture, ["start", "-o", str(out)])

    assert result.exit_code == 0
    assert link.sent == ["capture on", "capture off"]
    assert struct.unpack("<L", out.read_bytes()[:4])[0] == PCAP_MAGIC
    assert out.stat().st_size > 24  # header + at least one frame


def test_start_reports_firmware_without_the_capture_command(
    runner, fake_session, tmp_path
):
    fake_session(cap, FakeLink({"capture on": err("unknown command")}))
    result = runner.invoke(capture, ["start", "-o", str(tmp_path / "x.pcap")])
    out = flat(result.output)

    assert result.exit_code == 1
    assert "could not arm capture" in out
    assert "reflash" in out and ">= v0.8.0" in out


def test_start_needs_wireshark_for_a_live_feed(runner, fake_session):
    fake_session(cap, FakeLink())
    result = runner.invoke(capture, ["start", "-ws"])

    assert result.exit_code == 1
    assert "Wireshark not found" in flat(result.output)


def test_start_degrades_to_the_file_when_wireshark_is_missing(
    runner, fake_session, tmp_path
):
    link = fake_session(cap, FakeLink(stream_lines=TRANSCRIPT))
    out = tmp_path / "emv.pcap"
    result = runner.invoke(capture, ["start", "-ws", "-o", str(out)])

    assert result.exit_code == 0
    assert "capturing to the file only" in flat(result.output)
    assert link.sent == ["capture on", "capture off"]
    assert out.exists()


def test_start_streams_live_to_an_attached_wireshark(
    runner, fake_session, monkeypatch, tmp_path
):
    pipe = _FakePipe()
    started = {}

    class _FakeWireshark:
        def __init__(self, path, profile=None):
            started["path"], started["profile"] = path, profile

        def start(self):
            started["started"] = True

        def has_exited(self):
            return False

    monkeypatch.setattr(cap, "find_wireshark_path", lambda: "/usr/bin/wireshark")
    monkeypatch.setattr(cap, "_new_pipe", lambda: pipe)
    monkeypatch.setattr(cap, "Wireshark", _FakeWireshark)

    link = fake_session(cap, FakeLink(stream_lines=TRANSCRIPT))
    result = runner.invoke(capture, ["start", "-ws", "--profile", "nfc"])

    assert result.exit_code == 0
    assert started == {"path": pipe.pipe_path, "profile": "nfc", "started": True}
    assert struct.unpack("<L", pipe.packets[0][:4])[0] == PCAP_MAGIC
    assert len(pipe.packets) == 3  # global header + two APDU frames
    assert pipe.removed  # the FIFO is cleaned up on the way out
    assert link.sent == ["capture on", "capture off"]


def test_start_gives_up_when_wireshark_never_attaches(
    runner, fake_session, monkeypatch
):
    pipe = _FakePipe()
    pipe.ready_event.clear()

    monkeypatch.setattr(cap, "find_wireshark_path", lambda: "/usr/bin/wireshark")
    monkeypatch.setattr(cap, "_new_pipe", lambda: pipe)
    monkeypatch.setattr(
        cap,
        "Wireshark",
        lambda *a, **k: type(
            "W", (), {"start": lambda self: None, "has_exited": lambda self: False}
        )(),
    )
    monkeypatch.setattr(cap, "_WIRESHARK_PIPE_TIMEOUT", 0.01)

    fake_session(cap, FakeLink())
    result = runner.invoke(capture, ["start", "-ws"])

    assert result.exit_code == 1
    assert "did not attach to the pipe in time" in flat(result.output)
    assert pipe.removed


def test_start_disarms_the_tap_on_ctrl_c(runner, fake_session, tmp_path):
    def _lines():
        yield ":apdu cmd 1000 00a4"
        raise KeyboardInterrupt

    link = fake_session(cap, FakeLink(stream_lines=_lines()))
    result = runner.invoke(
        capture, ["start", "-o", str(tmp_path / "x.pcap")], catch_exceptions=False
    )

    assert result.exit_code == 0
    assert "stopping capture" in flat(result.output)
    assert link.sent[-1] == "capture off"


def test_start_survives_a_dead_link_while_disarming(runner, fake_session, tmp_path):
    link = fake_session(
        cap,
        FakeLink(
            {"capture off": DeviceError("serial link lost")}, stream_lines=TRANSCRIPT
        ),
    )
    result = runner.invoke(capture, ["start", "-o", str(tmp_path / "x.pcap")])

    assert result.exit_code == 0
    assert link.closed
    # M8: a failed disarm must not vanish silently — the board is left armed.
    out = flat(result.output)
    assert "could not disarm capture" in out
    assert "bombercat capture stop" in out


def test_start_cleans_up_the_pipe_even_when_closing_the_file_fails(
    runner, fake_session, monkeypatch, tmp_path
):
    """M9: a failing fileobj.close() (disk full) must not skip pipe cleanup."""
    pipe = _FakePipe()
    out_path = str(tmp_path / "x.pcap")
    real_open = open

    class _BadFile(BytesIO):
        def close(self):
            raise OSError("disk full")

    def _fake_open(path, mode="r", *a, **k):
        if str(path) == out_path:
            return _BadFile()
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr("builtins.open", _fake_open)
    monkeypatch.setattr(cap, "find_wireshark_path", lambda: "/usr/bin/wireshark")
    monkeypatch.setattr(cap, "_new_pipe", lambda: pipe)
    monkeypatch.setattr(
        cap,
        "Wireshark",
        lambda *a, **k: type(
            "W", (), {"start": lambda self: None, "has_exited": lambda self: False}
        )(),
    )

    fake_session(cap, FakeLink(stream_lines=TRANSCRIPT))
    result = runner.invoke(capture, ["start", "-ws", "-o", out_path])

    assert result.exit_code == 0
    assert pipe.removed
    assert "could not close pcap file cleanly" in flat(result.output)


def test_start_refuses_to_overwrite_an_existing_pcap_without_force(
    runner, fake_session, tmp_path
):
    out = tmp_path / "existing.pcap"
    out.write_bytes(b"already here")
    fake_session(cap, FakeLink())
    result = runner.invoke(capture, ["start", "-o", str(out)])

    assert result.exit_code == 1
    assert "already exists" in flat(result.output)
    assert out.read_bytes() == b"already here"


def test_start_force_overwrites_an_existing_pcap(runner, fake_session, tmp_path):
    out = tmp_path / "existing.pcap"
    out.write_bytes(b"already here")
    fake_session(cap, FakeLink(stream_lines=TRANSCRIPT))
    result = runner.invoke(capture, ["start", "-o", str(out), "--force"])

    assert result.exit_code == 0
    assert out.read_bytes() != b"already here"


# ── capture stop ─────────────────────────────────────────────────────────────


def test_stop_disarms_a_board_left_armed(runner, fake_session):
    link = fake_session(cap, FakeLink())
    result = runner.invoke(capture, ["stop"])

    assert result.exit_code == 0
    assert link.sent == ["capture off"]
    assert "capture disarmed on /dev/fake0" in flat(result.output)


def test_stop_reports_a_refusal(runner, fake_session):
    fake_session(cap, FakeLink({"capture off": err("not armed")}))
    result = runner.invoke(capture, ["stop"])

    assert result.exit_code == 1
    assert "capture off failed: not armed" in flat(result.output)


# ── watching Wireshark ───────────────────────────────────────────────────────


class _Ticks:
    """A stop-event stand-in whose `wait` returns instantly (no 0.5 s sleeps)."""

    def __init__(self, stop=False):
        self.stop = stop

    def wait(self, _timeout):
        return self.stop


def test_new_pipe_matches_the_platform(monkeypatch):
    monkeypatch.setattr(cap, "UnixPipe", lambda: "unix")
    monkeypatch.setattr(cap, "WindowsPipe", lambda: "windows")

    monkeypatch.setattr(cap.platform, "system", lambda: "Linux")
    assert cap._new_pipe() == "unix"

    monkeypatch.setattr(cap.platform, "system", lambda: "Windows")
    assert cap._new_pipe() == "windows"


def test_watcher_keeps_the_file_going_when_wireshark_quits(capsys):
    """The FIFO write end only breaks on the next write, so the process is polled."""
    sink = _CaptureSink(pipe=_FakePipe(), fileobj=BytesIO())
    cap._watch_wireshark(
        type("W", (), {"has_exited": lambda self: True})(), sink, _Ticks()
    )

    assert sink.pipe is None
    assert "continuing to the file only" in flat(capsys.readouterr().out)


def test_watcher_interrupts_a_capture_that_has_nowhere_left_to_write(
    monkeypatch, capsys
):
    interrupted = []
    monkeypatch.setattr(cap._thread, "interrupt_main", lambda: interrupted.append(True))
    sink = _CaptureSink(pipe=_FakePipe())
    cap._watch_wireshark(
        type("W", (), {"has_exited": lambda self: True})(), sink, _Ticks()
    )

    assert interrupted == [True]
    assert "stopping capture" in flat(capsys.readouterr().out)


def test_watcher_stops_when_the_capture_ends_first():
    sink = _CaptureSink(pipe=_FakePipe(), fileobj=BytesIO())
    cap._watch_wireshark(
        type("W", (), {"has_exited": lambda self: True})(), sink, _Ticks(stop=True)
    )

    assert sink.pipe is not None  # never reached the exit check
