#!/usr/bin/env python3

# Electronic Cats
# test_cli_device.py — `bombercat device list|info` (modules/device/cli.py): the
# table that maps a device ID to a physical board, and the per-board handshake.
# The USB and handshake layers are stubbed, so the table is checked for what it
# tells the user, not for what happens to be plugged in.

import pytest

from conftest import FakeLink, err, flat, make_device, make_port, ok
from modules.device import cli as dev
from modules.device.cli import device


@pytest.fixture
def usb_view(monkeypatch):
    """Script `device list`'s three inputs: ports, numbered boards, responders."""

    def _set(ports=(), devices=(), responders=()):
        monkeypatch.setattr(
            dev, "list_ports_info", lambda include_all=False: list(ports)
        )
        monkeypatch.setattr(dev, "find_devices", lambda *a, **k: list(devices))
        monkeypatch.setattr(dev, "discover_devices", lambda *a, **k: list(responders))

    return _set


# ── device list ──────────────────────────────────────────────────────────────


def test_list_says_so_when_there_are_no_serial_ports(runner, usb_view):
    usb_view()
    result = runner.invoke(device, ["list"])

    assert result.exit_code == 0
    assert "No serial ports found" in flat(result.output)


def test_list_marks_the_board_that_answers_and_shows_its_id(runner, usb_view):
    port = make_port("/dev/ttyACM0", serial_number="ABC123")
    usb_view(
        ports=[port],
        devices=[make_device(1, "/dev/ttyACM0", serial_number="ABC123")],
        responders=[make_device(1, "/dev/ttyACM0")],
    )
    result = runner.invoke(device, ["list"])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "#1" in out and "/dev/ttyACM0" in out and "✓" in out
    assert "ABC123" in out
    assert "bombercat <command> -d <ID>" in out


def test_list_flags_a_board_present_by_usb_id_that_stays_silent(runner, usb_view):
    usb_view(
        ports=[make_port("/dev/ttyACM0")],
        devices=[make_device(1, "/dev/ttyACM0")],
        responders=[],
    )
    out = flat(runner.invoke(device, ["list"]).output)

    assert "USB id" in out
    assert "did not answer the handshake" in out
    assert "NFCGate relay" in out


def test_list_asks_for_a_board_when_nothing_is_detected(runner, usb_view):
    usb_view(ports=[make_port("/dev/ttyUSB0", vid=0x0403, pid=0x6001)])
    out = flat(runner.invoke(device, ["list"]).output)

    assert "No BomberCat answered the handshake" in out


def test_list_warns_when_numbering_fell_back_to_every_port(runner, usb_view):
    """No port carries a BomberCat USB id, so the IDs are a best guess."""
    usb_view(
        ports=[make_port("/dev/ttyUSB0", vid=0x0403, pid=0x6001)],
        devices=[make_device(1, "/dev/ttyUSB0", usb_tagged=False)],
    )
    out = flat(runner.invoke(device, ["list"]).output)

    assert "No port carries a BomberCat USB id" in out
    assert "check the IDs above before using -d" in out


def test_list_all_includes_the_noise_ports(runner, monkeypatch):
    seen = {}

    def _ports(include_all=False):
        seen["include_all"] = include_all
        return [make_port("/dev/ttyS0", vid=None, pid=None, description="n/a")]

    monkeypatch.setattr(dev, "list_ports_info", _ports)
    monkeypatch.setattr(dev, "find_devices", lambda *a, **k: [])
    monkeypatch.setattr(dev, "discover_devices", lambda *a, **k: [])
    runner.invoke(device, ["list", "--all"])

    assert seen["include_all"] is True


# ── device info ──────────────────────────────────────────────────────────────


def test_info_renders_the_firmware_and_configuration(runner, use_link):
    use_link(
        dev,
        FakeLink(
            {
                "info": ok(
                    fw="0.8.0",
                    role="card",
                    ssid="HomeNet",
                    server="192.168.1.5",
                    port="5566",
                    session="42",
                    state="idle",
                )
            }
        ),
    )
    result = runner.invoke(device, ["info"])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "BomberCat @ /dev/fake0" in out
    assert "0.8.0" in out and "card" in out and "192.168.1.5" in out


def test_info_shows_a_dash_for_fields_the_firmware_omits(runner, use_link):
    use_link(dev, FakeLink({"info": ok(fw="0.8.0")}))
    assert "—" in flat(runner.invoke(device, ["info"]).output)


def test_info_reports_a_board_that_will_not_handshake(runner, use_link):
    use_link(dev, FakeLink(ping_ok=False))
    result = runner.invoke(device, ["info"])

    assert result.exit_code == 1
    assert "did not answer the handshake" in flat(result.output)


def test_info_reports_a_failed_info(runner, use_link):
    use_link(dev, FakeLink({"info": err("busy")}))
    result = runner.invoke(device, ["info"])

    assert result.exit_code == 1
    assert "info failed: busy" in flat(result.output)


def test_info_reports_an_unresolvable_target(runner, monkeypatch):
    from modules.core.bombercat import DeviceError

    def _boom(*a, **k):
        raise DeviceError("no BomberCat found; pass --port")

    monkeypatch.setattr(dev, "resolve_port", _boom)
    result = runner.invoke(device, ["info"])

    assert result.exit_code == 1
    assert "no BomberCat found" in flat(result.output)
    assert "Traceback" not in result.output


def test_info_reports_an_unexpected_error_without_a_traceback(runner, monkeypatch):
    def _boom(*a, **k):
        raise PermissionError("[Errno 13] /dev/ttyACM0")

    monkeypatch.setattr(dev, "resolve_port", lambda *a, **k: "/dev/ttyACM0")
    monkeypatch.setattr(dev, "DeviceLink", _boom)
    result = runner.invoke(device, ["info"])

    assert result.exit_code == 1
    assert "PermissionError:" in flat(result.output)
