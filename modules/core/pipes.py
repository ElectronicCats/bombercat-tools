#!/usr/bin/env python3

# Electronic Cats
# pipes.py — named-pipe (FIFO) transport + a Wireshark launcher, so a live
# capture can be streamed straight into Wireshark's "-k -i <fifo>" as pcap.
# Ported from catnip's modules/core/pipes.py (CatSniffer) and adapted for
# BomberCat's NFCGate APDU capture (docs/NFCGATE_PLAN.md Fase 8 / §16).
# Distributed as-is; no warranty is given.

import os
import platform
import shutil
import stat
import tempfile
import threading
import subprocess
import logging
from pathlib import Path

logger = logging.getLogger("rich")

DEFAULT_PIPELINE_NAME = "fbombercat"
DEFAULT_UNIX_PATH = f"/tmp/{DEFAULT_PIPELINE_NAME}"
DEFAULT_WINDOWS_PATH = f"\\\\.\\pipe\\{DEFAULT_PIPELINE_NAME}"


class PipelineError(Exception):
    """Raised by this module's pipe transports on setup/IO failure. Library
    code only raises typed errors; the CLI layer decides presentation and
    exit codes (see docs/AUDIT_ERROR_HANDLING.md H5)."""


# Windows named pipes need pywin32. Import it lazily so the module still loads
# (and the Unix path keeps working) on a box without it; only a Windows user who
# actually reaches WindowsPipe hits the error.
_WIN32_AVAILABLE = False
if platform.system().lower() == "windows":
    try:
        import win32pipe, win32file, pywintypes

        logger.info("[*] Windows library import done!")
        _WIN32_AVAILABLE = True
    except ImportError:
        logger.debug("pywin32 not installed; WindowsPipe unavailable until it is.")


def show_generic_error(title: str = "", e: object = "") -> None:
    logger.error(f"{title}: {e}")


# Where Wireshark usually lands, per OS. An ordered candidate list (the same
# idea as catnip's find_wireshark_path) plus a PATH lookup, so installs outside
# /usr/bin — Homebrew, snap, flatpak's exported wrapper — are found too.
_WIRESHARK_CANDIDATES = {
    "Windows": (
        "C:\\Program Files\\Wireshark\\Wireshark.exe",
        "C:\\Program Files (x86)\\Wireshark\\Wireshark.exe",
    ),
    "Linux": (
        "/usr/bin/wireshark",
        "/usr/local/bin/wireshark",
        "/snap/bin/wireshark",
    ),
    "Darwin": (
        "/Applications/Wireshark.app/Contents/MacOS/Wireshark",
        "/opt/homebrew/bin/wireshark",
        "/usr/local/bin/wireshark",
    ),
}


def find_wireshark_path():
    """Return the Wireshark executable as a Path, or None if it is not installed.

    Callers use the None to decide *before* creating a FIFO whether the live
    feed is possible at all — a FIFO nobody will ever read just hangs."""
    candidates = _WIRESHARK_CANDIDATES.get(platform.system())
    if candidates is None:
        show_generic_error("Unsupported OS", "We don't support this OS yet.")
        return None
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return path
    # Not in the usual places: let PATH have the last word.
    found = shutil.which("wireshark")
    if found is None and platform.system() == "Windows":
        found = shutil.which("Wireshark.exe")
    return Path(found) if found else None


class UnixPipe:
    """A POSIX FIFO Wireshark can read live. `open()` blocks until a reader
    (Wireshark) attaches, so it is normally called from a background thread and
    the caller waits on `ready_event`."""

    def __init__(self, path=None) -> None:
        # No explicit path: create a private, per-process directory
        # (mkdtemp defaults to mode 0700, owned by us) instead of a fixed,
        # predictable name under /tmp — another local user cannot pre-create
        # or symlink a path they can't guess. An explicit path is still
        # supported for callers/tests that need a known location.
        self._private_dir = None
        if path is None:
            self._private_dir = tempfile.mkdtemp(prefix=f"{DEFAULT_PIPELINE_NAME}-")
            path = os.path.join(self._private_dir, DEFAULT_PIPELINE_NAME)
        self.pipe_path = path
        self.pipe_writer = None
        self.ready_event = threading.Event()
        self.create()

    def create(self):
        try:
            os.mkfifo(self.pipe_path, mode=0o600)
            logger.info(f"[*] Pipeline created: {self.pipe_path}")
            return
        except FileExistsError:
            pass
        except OSError as e:
            raise PipelineError(f"creating pipeline {self.pipe_path}: {e}") from e

        # Something is already at pipe_path. Reusing it blindly is how a
        # local attacker gets us to stream a capture into a file/symlink
        # they planted, so only accept it if it is a FIFO we own.
        try:
            st = os.lstat(self.pipe_path)
        except OSError as e:
            raise PipelineError(f"creating pipeline {self.pipe_path}: {e}") from e
        if not stat.S_ISFIFO(st.st_mode):
            raise PipelineError(
                f"{self.pipe_path} already exists and is not a FIFO "
                "(refusing to reuse it — possible symlink attack)"
            )
        if st.st_uid != os.getuid():
            raise PipelineError(
                f"{self.pipe_path} already exists and is owned by another "
                f"user (uid {st.st_uid})"
            )
        logger.info(f"[-] Pipeline already exists, reusing: {self.pipe_path}")

    def open(self, mode="ab") -> None:
        logger.info(f"[*] Check if exist: {self.pipe_path}")
        if not os.path.exists(self.pipe_path):
            self.create()
        try:
            # Opening a FIFO for write blocks until a reader is present.
            self.pipe_writer = open(self.pipe_path, mode, buffering=0)
            self.ready_event.set()
            logger.info(f"[*] Pipeline Open ({mode}): {self.pipe_path}")
        except OSError as e:
            raise PipelineError(f"opening pipeline {self.pipe_path}: {e}") from e

    def read(self, size=1024) -> bytes:
        try:
            if self.pipe_writer:
                return self.pipe_writer.read(size)
            return b""
        except Exception as e:
            logger.debug("pipe read failed: %s", e)
            return b""

    def close(self) -> None:
        try:
            if self.pipe_writer:
                self.pipe_writer.close()
                self.pipe_writer = None
            self.ready_event.clear()
            logger.info(f"[*] Pipeline Closed: {self.pipe_path}")
        except Exception as e:
            show_generic_error("Closing Pipeline", e)

    def remove(self) -> None:
        try:
            if self.pipe_writer:
                self.pipe_writer.close()
                self.pipe_writer = None
            if os.path.exists(self.pipe_path):
                os.remove(self.pipe_path)
            if self._private_dir is not None:
                try:
                    os.rmdir(self._private_dir)
                except OSError:
                    pass
            logger.info(f"[*] Pipeline removed: {self.pipe_path}")
        except Exception as e:
            show_generic_error("Removing Pipeline", e)

    def write_packet(self, data: bytes) -> None:
        try:
            if self.pipe_writer:
                self.pipe_writer.write(data)
                self.pipe_writer.flush()
        except BrokenPipeError:
            # Wireshark went away; surface it so the capture loop can stop.
            show_generic_error("BrokenPipe", "Wireshark closed the pipe")
            self.remove()
            raise
        except Exception as e:
            show_generic_error("Writing Pipeline", e)


class WindowsPipe:
    """A Windows named pipe Wireshark can read live (mirror of UnixPipe)."""

    def __init__(self, path=DEFAULT_WINDOWS_PATH) -> None:
        if not _WIN32_AVAILABLE:
            raise PipelineError(
                "pywin32 is required for Windows named pipes — install it "
                "with: pip install pywin32"
            )
        self.pipe_path = path
        self.pipe_writer = None
        self.ready_event = threading.Event()
        self.create()

    def create(self):
        try:
            self.pipe_writer = win32pipe.CreateNamedPipe(
                self.pipe_path,
                win32pipe.PIPE_ACCESS_DUPLEX,
                win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_WAIT,
                2,
                65536,
                65536,
                0,
                None,
            )
        except FileExistsError:
            logger.info("[-] Pipeline already exists.")
        except pywintypes.error as e:
            raise PipelineError(f"creating pipeline {self.pipe_path}: {e}") from e

    def open(self) -> None:
        logger.info(f"[*] Waiting for a client on {self.pipe_path}.")
        try:
            win32pipe.ConnectNamedPipe(self.pipe_writer, None)
            self.ready_event.set()
            logger.info(f"[*] Pipeline Open: {self.pipe_path}")
        except pywintypes.error as e:
            if e.winerror == 535:  # ERROR_PIPE_CONNECTED
                self.ready_event.set()
                logger.info("[*] Client already connected")
            elif e.winerror == 232:  # ERROR_NO_DATA
                logger.warning("[!] Client connected and disconnected immediately")
                return
            else:
                show_generic_error("Opening Pipeline", e)
                raise

    def read(self, size=1024) -> bytes:
        try:
            if self.pipe_writer:
                _, available, _ = win32pipe.PeekNamedPipe(self.pipe_writer, 0)
                if available == 0:
                    return b""
                hr, data = win32file.ReadFile(self.pipe_writer, min(size, available))
                return data
            return b""
        except Exception as e:
            logger.debug("pipe read failed: %s", e)
            return b""

    def close(self) -> None:
        try:
            if self.pipe_writer:
                win32pipe.DisconnectNamedPipe(self.pipe_writer)
                win32file.CloseHandle(self.pipe_writer)
                self.pipe_writer = None
            self.ready_event.clear()
            logger.info(f"[*] Pipeline Closed: {self.pipe_path}")
        except Exception as e:
            show_generic_error("Closing Pipeline", e)

    def remove(self) -> None:
        try:
            if self.pipe_writer:
                # If ConnectNamedPipe is still pending in a background thread,
                # CloseHandle blocks until it completes. Unblock it first by
                # connecting a throwaway client so ConnectNamedPipe returns.
                if not self.ready_event.is_set():
                    try:
                        dummy = win32file.CreateFile(
                            self.pipe_path,
                            win32file.GENERIC_READ,
                            0,
                            None,
                            win32file.OPEN_EXISTING,
                            0,
                            None,
                        )
                        win32file.CloseHandle(dummy)
                    except Exception:
                        pass
                try:
                    win32pipe.DisconnectNamedPipe(self.pipe_writer)
                except Exception:
                    pass
                win32file.CloseHandle(self.pipe_writer)
                self.pipe_writer = None
            self.ready_event.clear()
            logger.info(f"[*] Pipeline removed: {self.pipe_path}")
        except Exception as e:
            show_generic_error("Removing Pipeline", e)

    def write_packet(self, data: bytes) -> None:
        try:
            win32file.WriteFile(self.pipe_writer, data)
            win32file.FlushFileBuffers(self.pipe_writer)
        except pywintypes.error as e:
            if e.winerror in (109, 232):  # broken pipe / no data
                logger.warning("[!] Wireshark disconnected")
                self.close()
                # Mirror UnixPipe: surface the disconnect so the capture loop
                # can react (drop the pipe, keep writing to a file if any)
                # instead of silently discarding subsequent frames.
                raise BrokenPipeError(str(e)) from e
            show_generic_error("Writing Pipeline", e)


class Wireshark(threading.Thread):
    def __init__(self, pipe_name=None, profile=None):
        super().__init__(daemon=True)
        self.system = platform.system()
        if pipe_name is None:
            self.pipe_name = (
                DEFAULT_WINDOWS_PATH if self.system == "Windows" else DEFAULT_UNIX_PATH
            )
        else:
            self.pipe_name = pipe_name
        self.profile = profile
        self.running = True
        self.wireshark_process: subprocess.Popen | None = None
        # Set by run() when the launch itself fails (binary vanished after the
        # caller's find_wireshark_path() check, exec failure, ...), so a caller
        # blocked waiting on the FIFO can report the real cause instead of a
        # generic "did not attach in time" timeout.
        self.spawn_error: str | None = None

    def get_wireshark_path(self):
        """The Wireshark executable (Path), or None when it is not installed."""
        return find_wireshark_path()

    def has_exited(self) -> bool:
        """True once Wireshark was launched AND has quit. A FIFO write end only
        breaks on the *next* write, so polling the process is how a capture
        notices the user closed Wireshark while no packets were flowing."""
        proc = self.wireshark_process
        return proc is not None and proc.poll() is not None

    def get_wireshark_pipepath(self):
        return self.pipe_name

    def get_wireshark_cmd(self):
        exe_path = self.get_wireshark_path()
        if exe_path is None:
            return None
        cmd = [str(exe_path), "-k", "-i", self.get_wireshark_pipepath()]
        if self.profile:
            cmd += ["-C", self.profile]
        return cmd

    def run(self):
        cmd = self.get_wireshark_cmd()
        if cmd is None:
            self.spawn_error = "executable not found"
            show_generic_error("Can't start Wireshark", self.spawn_error)
            return
        try:
            self.wireshark_process = subprocess.Popen(cmd)
            # Wait for the process to finish, otherwise the thread exits immediately
            self.wireshark_process.wait()
        except Exception as e:
            self.spawn_error = str(e)
            show_generic_error("Can't start Wireshark", e)
