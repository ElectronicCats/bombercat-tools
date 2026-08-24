#!/usr/bin/env python3

# Electronic Cats
# test_core_pipes.py — the live-capture transport (modules/core/pipes.py): the
# FIFO Wireshark reads and the launcher that starts it. The FIFO is exercised
# for real (in a tmp dir, with a reader on the other end); Wireshark itself is
# never started. docs/NFCGATE_PLAN.md Fase 8 / §16.

import os
import stat
import subprocess
import threading

import pytest

from modules.core import pipes
from modules.core.pipes import UnixPipe, Wireshark, find_wireshark_path

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX FIFOs; the Windows pipe needs pywin32"
)


@pytest.fixture
def fifo_path(tmp_path):
    return str(tmp_path / "fbombercat")


@pytest.fixture
def reader(fifo_path):
    """A non-blocking read end, so `open()` does not block the test."""
    fds = []

    def _attach():
        fd = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)
        fds.append(fd)
        return fd

    yield _attach
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


# ── UnixPipe ─────────────────────────────────────────────────────────────────


def test_creating_the_pipe_makes_a_fifo(fifo_path):
    UnixPipe(fifo_path)
    assert stat.S_ISFIFO(os.stat(fifo_path).st_mode)


def test_creating_over_an_existing_fifo_we_own_is_not_an_error(fifo_path):
    UnixPipe(fifo_path)
    UnixPipe(fifo_path)  # a leftover FIFO we own is reused, not fatal
    assert stat.S_ISFIFO(os.stat(fifo_path).st_mode)


def test_default_path_is_private_and_unpredictable():
    """No fixed /tmp/fbombercat: each instance gets its own mode-0700 dir, so
    another local user can neither pre-create nor guess the path (C3)."""
    pipe = UnixPipe()
    try:
        assert pipe.pipe_path != pipes.DEFAULT_UNIX_PATH
        parent = os.path.dirname(pipe.pipe_path)
        assert stat.S_IMODE(os.stat(parent).st_mode) == 0o700
        assert stat.S_ISFIFO(os.stat(pipe.pipe_path).st_mode)
    finally:
        pipe.remove()


def test_removing_the_default_pipe_cleans_up_its_private_dir():
    pipe = UnixPipe()
    private_dir = pipe._private_dir
    pipe.remove()
    assert not os.path.exists(private_dir)


def test_creating_over_a_symlink_refuses_to_reuse_it(tmp_path, fifo_path):
    target = tmp_path / "elsewhere"
    target.write_text("attacker-controlled")
    os.symlink(target, fifo_path)

    with pytest.raises(SystemExit):
        UnixPipe(fifo_path)
    assert target.read_text() == "attacker-controlled"  # never opened/written


def test_creating_over_a_fifo_owned_by_another_user_refuses_to_reuse_it(
    monkeypatch, fifo_path
):
    UnixPipe(fifo_path)  # a FIFO that "belongs" to someone else on disk
    real_getuid = os.getuid
    monkeypatch.setattr(pipes.os, "getuid", lambda: real_getuid() + 1)

    with pytest.raises(SystemExit):
        UnixPipe(fifo_path)


def test_a_pipe_that_cannot_be_created_stops_the_capture(monkeypatch, fifo_path):
    monkeypatch.setattr(
        pipes.os,
        "mkfifo",
        lambda p, *a, **k: (_ for _ in ()).throw(OSError("read-only file system")),
    )
    with pytest.raises(SystemExit):
        UnixPipe(fifo_path)


def test_writing_before_a_reader_attaches_is_a_no_op(fifo_path):
    UnixPipe(fifo_path).write_packet(b"pcap")  # nothing is open yet: dropped


def test_packets_reach_the_reader(fifo_path, reader):
    pipe = UnixPipe(fifo_path)
    fd = reader()
    pipe.open()

    assert pipe.ready_event.is_set()  # what `capture start` waits on
    pipe.write_packet(b"\xd4\xc3\xb2\xa1")
    assert os.read(fd, 64) == b"\xd4\xc3\xb2\xa1"


def test_open_recreates_a_fifo_that_was_removed(fifo_path, reader):
    pipe = UnixPipe(fifo_path)
    os.remove(fifo_path)

    opened = threading.Thread(target=pipe.open, daemon=True)
    opened.start()
    while not os.path.exists(fifo_path):  # the writer thread re-creates it
        pass
    reader()
    opened.join(timeout=5)

    assert pipe.ready_event.is_set()


def test_closing_releases_the_writer(fifo_path, reader):
    pipe = UnixPipe(fifo_path)
    reader()
    pipe.open()
    pipe.close()

    assert pipe.pipe_writer is None
    assert not pipe.ready_event.is_set()
    assert os.path.exists(fifo_path)  # closed, but not removed


def test_removing_deletes_the_fifo(fifo_path, reader):
    pipe = UnixPipe(fifo_path)
    reader()
    pipe.open()
    pipe.remove()

    assert pipe.pipe_writer is None
    assert not os.path.exists(fifo_path)


def test_removing_a_pipe_twice_is_harmless(fifo_path):
    pipe = UnixPipe(fifo_path)
    pipe.remove()
    pipe.remove()


def test_a_closed_reader_breaks_the_pipe_and_cleans_it_up(fifo_path, reader):
    """Wireshark quitting must surface: `_pump` stops or falls back to the file."""
    pipe = UnixPipe(fifo_path)
    fd = reader()
    pipe.open()
    os.close(fd)

    with pytest.raises(BrokenPipeError):
        pipe.write_packet(b"pcap")
    assert not os.path.exists(fifo_path)


def test_reading_from_a_write_only_pipe_returns_nothing(fifo_path, reader):
    pipe = UnixPipe(fifo_path)
    reader()
    pipe.open()

    assert pipe.read() == b""


def test_reading_before_opening_returns_nothing(fifo_path):
    assert UnixPipe(fifo_path).read() == b""


# ── find_wireshark_path ──────────────────────────────────────────────────────


def test_wireshark_is_found_in_the_usual_place(monkeypatch, tmp_path):
    exe = tmp_path / "wireshark"
    exe.touch()
    monkeypatch.setattr(pipes.platform, "system", lambda: "Linux")
    monkeypatch.setattr(pipes, "_WIRESHARK_CANDIDATES", {"Linux": (str(exe),)})

    assert find_wireshark_path() == exe


def test_wireshark_is_found_on_path_when_installed_elsewhere(monkeypatch, tmp_path):
    """Homebrew, snap and flatpak wrappers live outside the candidate list."""
    exe = tmp_path / "flatpak-wireshark"
    monkeypatch.setattr(pipes.platform, "system", lambda: "Linux")
    monkeypatch.setattr(pipes, "_WIRESHARK_CANDIDATES", {"Linux": ("/nope",)})
    monkeypatch.setattr(pipes.shutil, "which", lambda name: str(exe))

    assert find_wireshark_path() == exe


def test_missing_wireshark_is_reported_as_none(monkeypatch):
    monkeypatch.setattr(pipes.platform, "system", lambda: "Linux")
    monkeypatch.setattr(pipes, "_WIRESHARK_CANDIDATES", {"Linux": ("/nope",)})
    monkeypatch.setattr(pipes.shutil, "which", lambda name: None)

    assert find_wireshark_path() is None


def test_unsupported_os_reports_none(monkeypatch):
    monkeypatch.setattr(pipes.platform, "system", lambda: "Plan9")
    assert find_wireshark_path() is None


# ── Wireshark launcher ───────────────────────────────────────────────────────


@pytest.fixture
def wireshark_installed(monkeypatch, tmp_path):
    exe = tmp_path / "wireshark"
    exe.touch()
    monkeypatch.setattr(pipes, "find_wireshark_path", lambda: exe)
    return exe


def test_launcher_reads_the_fifo_as_a_live_interface(wireshark_installed):
    cmd = Wireshark("/tmp/fbombercat").get_wireshark_cmd()
    assert cmd == [str(wireshark_installed), "-k", "-i", "/tmp/fbombercat"]


def test_launcher_passes_a_configuration_profile(wireshark_installed):
    cmd = Wireshark("/tmp/fbombercat", profile="nfc").get_wireshark_cmd()
    assert cmd[-2:] == ["-C", "nfc"]


def test_launcher_defaults_to_the_platform_pipe_path(monkeypatch, wireshark_installed):
    monkeypatch.setattr(pipes.platform, "system", lambda: "Linux")
    assert Wireshark().get_wireshark_pipepath() == pipes.DEFAULT_UNIX_PATH


def test_launcher_has_no_command_without_wireshark(monkeypatch):
    monkeypatch.setattr(pipes, "find_wireshark_path", lambda: None)
    assert Wireshark("/tmp/fbombercat").get_wireshark_cmd() is None


def test_launcher_starts_the_process_and_waits_for_it(monkeypatch, wireshark_installed):
    started = {}

    class _Proc:
        def __init__(self, cmd):
            started["cmd"] = cmd
            self.waited = False

        def wait(self):
            self.waited = True

        def poll(self):
            return 0 if self.waited else None

    monkeypatch.setattr(subprocess, "Popen", _Proc)
    ws = Wireshark("/tmp/fbombercat")
    ws.run()

    assert started["cmd"][-1] == "/tmp/fbombercat"
    assert ws.wireshark_process.waited


def test_launcher_survives_a_process_that_will_not_start(
    monkeypatch, wireshark_installed
):
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")),
    )
    ws = Wireshark("/tmp/fbombercat")
    ws.run()  # logged, not raised: the capture continues to its file

    assert ws.wireshark_process is None


def test_has_exited_is_false_before_wireshark_is_launched():
    assert Wireshark("/tmp/fbombercat").has_exited() is False


def test_has_exited_tracks_the_running_process(wireshark_installed):
    ws = Wireshark("/tmp/fbombercat")
    ws.wireshark_process = type("P", (), {"poll": lambda self: None})()
    assert ws.has_exited() is False

    ws.wireshark_process = type("P", (), {"poll": lambda self: 0})()
    assert ws.has_exited() is True
