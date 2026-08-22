#!/usr/bin/env python3

# Electronic Cats
# test_detect_firmware.py — modules/core/firmwares.detect_firmware and
# resolve_status_port: the levelled, handshake-optional detection that powers
# `bombercat status`. Every case runs against a FakeLink / monkeypatched USB
# layer, no hardware. docs/GENERALIZE_CLI_PLAN.md §2.2, §5.1.

import pytest

from conftest import DeviceError, FakeLink, make_device, ok
from modules.core import firmwares as fw


@pytest.fixture
def fake_link(monkeypatch):
    """Point firmwares.DeviceLink at a supplied FakeLink."""

    def _use(link: FakeLink):
        monkeypatch.setattr(fw, "DeviceLink", lambda *a, **k: link)
        return link

    return _use


# ── detection levels ─────────────────────────────────────────────────────────


def test_handshake_identifies_nfcgate_with_its_version(fake_link):
    fake_link(FakeLink({"info": ok(fw="0.9.7")}, ping_ok=True))
    r = fw.detect_firmware("/dev/ttyACM0")

    assert r.confidence == fw.HANDSHAKE
    assert r.firmware.id == "nfcgate"
    assert r.version == "0.9.7"
    assert r.usb_present and r.identified


def test_handshake_trusts_an_explicit_fw_name(fake_link):
    """When a future firmware reports :fw_name we use it verbatim (§2.5)."""
    fake_link(FakeLink({"info": ok(fw="1.0.0", fw_name="nfcgate")}, ping_ok=True))
    r = fw.detect_firmware("/dev/ttyACM0")

    assert r.firmware.id == "nfcgate"
    assert r.version == "1.0.0"


def test_handshake_ignores_an_unknown_fw_name_and_falls_back(fake_link):
    fake_link(FakeLink({"info": ok(fw="9", fw_name="martian")}, ping_ok=True))
    r = fw.detect_firmware("/dev/ttyACM0")

    assert r.confidence == fw.HANDSHAKE
    assert r.firmware.id == "nfcgate"  # only REPL firmware -> the fallback


def test_banner_match_identifies_a_repl_less_firmware(fake_link):
    fake_link(
        FakeLink(
            ping_ok=False,
            stream_lines=["booting", "Detect NFC tags with PN7150/60", "ready"],
        )
    )
    r = fw.detect_firmware("/dev/ttyACM0")

    assert r.confidence == fw.BANNER
    assert r.firmware.id == "detecttags"
    assert r.identified


def test_sniff_can_be_disabled(fake_link):
    fake_link(FakeLink(ping_ok=False, stream_lines=["Detect NFC tags with PN7150/60"]))
    r = fw.detect_firmware("/dev/ttyACM0", sniff=False)

    assert r.confidence == fw.USB  # present, but we didn't look at the banner
    assert r.firmware is fw.UNKNOWN


def test_usb_present_but_silent_is_unknown(fake_link):
    fake_link(FakeLink(ping_ok=False, stream_lines=[]))
    r = fw.detect_firmware("/dev/ttyACM0", usb_present=True)

    assert r.confidence == fw.USB
    assert r.firmware is fw.UNKNOWN
    assert r.usb_present


def test_no_board_yields_none(fake_link):
    fake_link(FakeLink(ping_ok=False, stream_lines=[]))
    r = fw.detect_firmware("/dev/ttyACM0", usb_present=False)

    assert r.confidence == fw.NONE
    assert not r.usb_present


def test_detection_swallows_a_serial_error(monkeypatch):
    def _boom(*a, **k):
        raise OSError("could not open port")

    monkeypatch.setattr(fw, "DeviceLink", _boom)
    r = fw.detect_firmware("/dev/ttyACM0", usb_present=True)

    assert r.confidence == fw.USB  # no traceback, honest USB verdict


# ── resolve_status_port (no handshake) ───────────────────────────────────────


def test_preferred_port_wins_and_reports_usb_tagging(monkeypatch):
    monkeypatch.setattr(fw, "bombercat_ports", lambda: [])
    port, tagged = fw.resolve_status_port(preferred="/dev/ttyUSB9")
    assert port == "/dev/ttyUSB9" and tagged is False


def test_single_attached_board_is_auto_selected(monkeypatch):
    monkeypatch.setattr(
        fw, "find_devices", lambda: [make_device(1, "/dev/ttyACM0", usb_tagged=True)]
    )
    port, tagged = fw.resolve_status_port()
    assert port == "/dev/ttyACM0" and tagged is True


def test_multiple_boards_demand_a_device_flag(monkeypatch):
    monkeypatch.setattr(
        fw,
        "find_devices",
        lambda: [make_device(1, "/dev/ttyACM0"), make_device(2, "/dev/ttyACM1")],
    )
    monkeypatch.setattr(fw, "describe_devices", lambda d: "#1, #2")
    with pytest.raises(DeviceError, match="multiple BomberCats"):
        fw.resolve_status_port()


def test_no_board_raises(monkeypatch):
    monkeypatch.setattr(fw, "find_devices", lambda: [])
    with pytest.raises(DeviceError, match="no BomberCat found"):
        fw.resolve_status_port()


def test_port_and_device_are_mutually_exclusive():
    with pytest.raises(DeviceError, match="mutually exclusive"):
        fw.resolve_status_port(preferred="/dev/ttyACM0", device_id=1)
