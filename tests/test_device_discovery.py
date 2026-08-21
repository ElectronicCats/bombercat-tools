#!/usr/bin/env python3

# Electronic Cats
# test_device_discovery.py — handshake discovery and target selection
# (`discover_devices` / `resolve_port` in modules/core/bombercat.py). These
# encode the rules behind `-p/--port` and `-d/--device`, and every "no
# BomberCat found" message the user ever sees, so each branch is pinned here.

import pytest
import serial

from conftest import FakeLink, make_device, make_port
from modules.core import bombercat as core
from modules.core.bombercat import DeviceError, discover_devices, resolve_port


@pytest.fixture
def usb_devices(monkeypatch):
    """Script the USB-only view: which boards `find_devices` reports."""

    def _set(*devices):
        devices = list(devices)
        monkeypatch.setattr(core, "find_devices", lambda *a, **k: list(devices))
        monkeypatch.setattr(
            core,
            "find_device",
            lambda device_id=None: next(
                (d for d in devices if d.device_id == device_id), None
            ),
        )
        return devices

    return _set


@pytest.fixture
def handshake(monkeypatch):
    """Script which ports answer the handshake when a link is opened."""

    def _set(*responding_ports, raises=None):
        def _link(port, *a, **k):
            if raises is not None and port in responding_ports:
                raise raises
            return FakeLink(ping_ok=port in responding_ports, port=port)

        monkeypatch.setattr(core, "DeviceLink", _link)

    return _set


# ── discover_devices ─────────────────────────────────────────────────────────


def test_discover_returns_only_the_boards_that_answer(usb_devices, handshake):
    usb_devices(make_device(1, "/dev/ttyACM0"), make_device(2, "/dev/ttyACM1"))
    handshake("/dev/ttyACM1")

    assert [d.port for d in discover_devices()] == ["/dev/ttyACM1"]


def test_discover_keeps_the_id_assigned_by_the_usb_layer(usb_devices, handshake):
    """A board keeps its number whether or not its neighbours answer."""
    usb_devices(make_device(1, "/dev/ttyACM0"), make_device(2, "/dev/ttyACM1"))
    handshake("/dev/ttyACM1")

    assert [d.device_id for d in discover_devices()] == [2]


def test_discover_skips_ports_that_cannot_be_opened(usb_devices, handshake):
    usb_devices(make_device(1, "/dev/ttyACM0"))
    handshake("/dev/ttyACM0", raises=serial.SerialException("permission denied"))

    assert discover_devices() == []


def test_discover_with_nothing_attached(usb_devices, handshake):
    usb_devices()
    handshake()
    assert discover_devices() == []


# ── resolve_port: explicit selection ─────────────────────────────────────────


def test_explicit_port_wins_without_enumerating(monkeypatch):
    def _boom(*a, **k):  # noqa: ANN001 - must never be reached
        raise AssertionError("--port must not trigger enumeration")

    monkeypatch.setattr(core, "find_devices", _boom)
    monkeypatch.setattr(core, "discover_devices", _boom)

    assert resolve_port("/dev/ttyUSB9") == "/dev/ttyUSB9"


def test_port_and_device_are_mutually_exclusive():
    with pytest.raises(DeviceError, match="mutually exclusive"):
        resolve_port("/dev/ttyACM0", 1)


def test_device_id_selects_its_port(usb_devices):
    usb_devices(make_device(1, "/dev/ttyACM0"), make_device(2, "/dev/ttyACM1"))
    assert resolve_port(None, 2) == "/dev/ttyACM1"


def test_unknown_device_id_lists_what_is_attached(usb_devices):
    usb_devices(make_device(1, "/dev/ttyACM0"))
    with pytest.raises(DeviceError) as e:
        resolve_port(None, 7)

    assert "no BomberCat with ID 7" in str(e.value)
    assert "#1 /dev/ttyACM0" in str(e.value)


def test_device_id_with_nothing_attached_says_so(usb_devices):
    usb_devices()
    with pytest.raises(DeviceError, match="none is attached"):
        resolve_port(None, 7)


# ── resolve_port: auto-detection ─────────────────────────────────────────────


def test_single_responding_board_is_auto_detected(monkeypatch):
    monkeypatch.setattr(
        core, "discover_devices", lambda *a, **k: [make_device(1, "/dev/ttyACM0")]
    )
    assert resolve_port() == "/dev/ttyACM0"


def test_several_boards_require_an_explicit_selector(monkeypatch):
    monkeypatch.setattr(
        core,
        "discover_devices",
        lambda *a, **k: [
            make_device(1, "/dev/ttyACM0"),
            make_device(2, "/dev/ttyACM1"),
        ],
    )
    with pytest.raises(DeviceError) as e:
        resolve_port()

    assert "multiple BomberCats" in str(e.value)
    assert "--device/-d" in str(e.value)


def test_silent_board_present_by_usb_id_points_at_the_firmware(monkeypatch):
    monkeypatch.setattr(core, "discover_devices", lambda *a, **k: [])
    monkeypatch.setattr(
        core,
        "bombercat_ports",
        lambda: [make_port("/dev/ttyACM0")],
    )
    with pytest.raises(DeviceError) as e:
        resolve_port()

    assert "did not answer the handshake" in str(e.value)
    assert "NFCGate relay" in str(e.value)


def test_several_silent_boards_ask_for_a_device_id(monkeypatch):
    monkeypatch.setattr(core, "discover_devices", lambda *a, **k: [])
    monkeypatch.setattr(
        core,
        "bombercat_ports",
        lambda: [make_port("/dev/ttyACM0"), make_port("/dev/ttyACM1")],
    )
    monkeypatch.setattr(core, "describe_devices", lambda *a, **k: "#1 /dev/ttyACM0")
    with pytest.raises(DeviceError, match="none answered the handshake"):
        resolve_port()


def test_nothing_attached_suggests_passing_a_port(monkeypatch):
    monkeypatch.setattr(core, "discover_devices", lambda *a, **k: [])
    monkeypatch.setattr(core, "bombercat_ports", lambda: [])
    with pytest.raises(DeviceError, match=r"no BomberCat found; pass --port"):
        resolve_port()
