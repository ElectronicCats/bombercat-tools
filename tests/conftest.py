#!/usr/bin/env python3

# Electronic Cats
# conftest.py — shared fixtures for the pytest unit suite (tools/tests/test_*.py).
#
# The unit tests never touch hardware: every test drives the CLI through Click's
# CliRunner with the serial layer replaced by the fakes below, so the whole
# command surface (docs/NFCGATE_PLAN.md Fase 6/8) can be exercised on a host
# with no BomberCat attached. The end-to-end *_hosttest.py scripts still cover
# the protocol against a real pty.
#
# Run:  pytest tools/tests

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

import pytest
from click.testing import CliRunner

# tools/tests/conftest.py -> tools/ on sys.path, so `modules.*` imports resolve
# the same way they do for bombercat.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.core.bombercat import DeviceError, Response  # noqa: E402
from modules.core.usb_connection import BomberCatDevice, PortInfo  # noqa: E402

# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeSerial:
    """A `serial.Serial` stand-in scripted with the lines a device would reply.

    ``script`` maps a command line (or its first word) to the raw lines the
    firmware answers with; anything unscripted gets ``-ERR unknown command``,
    as SerialControl does. ``readline`` returns b"" once a reply is exhausted,
    which is exactly what a real port does when its read timeout ticks.
    """

    def __init__(
        self,
        script: Optional[Dict[str, Iterable[str]]] = None,
        write_error: Optional[Exception] = None,
        read_error: Optional[Exception] = None,
    ):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.write_error = write_error
        self.read_error = read_error
        self.written: List[str] = []
        self.pending: List[bytes] = []
        self.closed = False
        self.resets = 0

    # -- serial.Serial API used by DeviceLink --------------------------------
    def reset_input_buffer(self) -> None:
        self.resets += 1
        self.pending.clear()

    def write(self, data: bytes) -> int:
        if self.write_error is not None:
            raise self.write_error
        line = data.decode("ascii", "replace").strip()
        self.written.append(line)
        self.pending.extend(
            (reply + "\r\n").encode() for reply in self._reply_for(line)
        )
        return len(data)

    def flush(self) -> None:
        pass

    def readline(self) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        return self.pending.pop(0) if self.pending else b""

    def close(self) -> None:
        self.closed = True

    # -- helpers -------------------------------------------------------------
    def _reply_for(self, line: str) -> List[str]:
        for key in (line, line.split(" ")[0]):
            if key in self.script:
                return self.script[key]
        return ["-ERR unknown command"]

    def feed(self, *lines: str) -> None:
        """Queue unsolicited lines (device logs, `:apdu` events) for `stream`."""
        self.pending.extend((line + "\r\n").encode() for line in lines)


class FakeLink:
    """A `DeviceLink` stand-in for the command-level tests.

    ``responses`` maps a command line (or its first word) to what `command`
    should return: a ``Response``, an ``Exception`` to raise, or a list used as
    a queue (the last entry repeats, so a `status` poll can report progress and
    then settle). Unscripted commands succeed, which keeps a test's script to
    just the commands it actually cares about.
    """

    def __init__(
        self,
        responses: Optional[Dict[str, object]] = None,
        ping_ok: bool = True,
        stream_lines: Iterable[str] = (),
        port: str = "/dev/fake0",
    ):
        self.port = port
        self.responses = dict(responses or {})
        self.ping_ok = ping_ok
        # Kept as given (not listified): a generator lets a test raise from
        # inside the stream, e.g. the Ctrl-C a `monitor` run must survive.
        self.stream_lines = stream_lines
        self.sent: List[str] = []
        self.opened = False
        self.closed = False

    # -- lifecycle -----------------------------------------------------------
    def open(self) -> "FakeLink":
        self.opened = True
        return self

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "FakeLink":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- protocol ------------------------------------------------------------
    def command(self, line: str, read_timeout: Optional[float] = None) -> Response:
        line = line.strip()
        self.sent.append(line)
        for key in (line, line.split(" ")[0]):
            if key in self.responses:
                return self._resolve(self.responses[key])
        return Response(ok=True)

    def _resolve(self, value) -> Response:
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        if isinstance(value, Exception):
            raise value
        return value

    def ping(self) -> bool:
        return self.ping_ok

    def info(self) -> Response:
        return self.command("info")

    def status(self) -> Response:
        return self.command("status")

    def set(self, key: str, value: str) -> Response:
        return self.command(f"set {key} {value}")

    def save(self) -> Response:
        return self.command("save")

    def run(self) -> Response:
        return self.command("run")

    def stop(self) -> Response:
        return self.command("stop")

    def identify(self) -> Response:
        return self.command("identify")

    def stream(self) -> Iterator[str]:
        yield from self.stream_lines


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def link() -> FakeLink:
    """A device that answers the handshake and accepts every command."""
    return FakeLink()


@pytest.fixture
def use_link(monkeypatch):
    """Point a CLI module's device access at a FakeLink.

    Patches the module's ``resolve_port``/``DeviceLink`` (the two names every
    command reaches the hardware through), so the command under test runs its
    real logic against the fake.
    """

    def _use(module, fake: FakeLink, target: str = "/dev/fake0"):
        monkeypatch.setattr(
            module, "resolve_port", lambda *a, **k: target, raising=False
        )
        monkeypatch.setattr(module, "DeviceLink", lambda *a, **k: fake, raising=False)
        return fake

    return _use


@pytest.fixture
def fake_session(monkeypatch):
    """Replace ``_device_session`` in a module with one yielding a FakeLink.

    ``modules.capture.cli`` imports the context manager from nfcgate, so its
    tests patch it here instead of the port/link pair underneath.
    """

    def _use(module, fake: FakeLink, target: str = "/dev/fake0"):
        @contextmanager
        def _session(port, device_id=None):
            fake.open()
            try:
                yield target, fake
            finally:
                fake.close()

        monkeypatch.setattr(module, "_device_session", _session, raising=False)
        return fake

    return _use


# ── Builders for the USB layer ───────────────────────────────────────────────


def make_port(
    device: str = "/dev/ttyACM0",
    vid: Optional[int] = 0x1209,
    pid: Optional[int] = 0x005E,
    serial_number: Optional[str] = None,
    description: str = "BomberCat",
    location: Optional[str] = None,
) -> PortInfo:
    """A PortInfo shaped like what pyserial reports for a BomberCat CDC."""
    hwid = f"USB VID:PID={vid:04X}:{pid:04X}" if vid is not None else "n/a"
    return PortInfo(
        device=device,
        description=description,
        hwid=hwid,
        vid=vid,
        pid=pid,
        serial_number=serial_number,
        location=location,
    )


def make_device(device_id: int = 1, port: str = "/dev/ttyACM0", **kw):
    return BomberCatDevice(device_id=device_id, port=port, **kw)


def flat(text: str) -> str:
    """Collapse Rich's line wrapping so assertions can match plain sentences."""
    return " ".join(text.split())


def ok(message: str = "", **data) -> Response:
    return Response(ok=True, message=message, data=dict(data))


def err(message: str, **data) -> Response:
    return Response(ok=False, message=message, data=dict(data))


__all__ = [
    "DeviceError",
    "FakeLink",
    "FakeSerial",
    "Response",
    "err",
    "flat",
    "make_device",
    "make_port",
    "ok",
]
