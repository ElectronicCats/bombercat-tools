#!/usr/bin/env python3

# Electronic Cats
# test_usb_connection.py — port enumeration and the stable device numbering that
# `--device/-d` addresses (modules/core/usb_connection.py). No port is ever
# opened here: this layer is USB-only by design, since opening a port can reset
# the MCU.

import pytest

from conftest import make_port
from modules.core import usb_connection as usb
from modules.core.usb_connection import (
    BomberCatDevice,
    describe_devices,
    find_device,
    find_devices,
    list_ports_info,
    open_serial,
)


class _ComPort:
    """What `serial.tools.list_ports.comports()` hands back."""

    def __init__(
        self,
        device,
        description="",
        hwid="",
        vid=None,
        pid=None,
        serial_number=None,
        location=None,
    ):
        self.device = device
        self.description = description
        self.hwid = hwid
        self.vid = vid
        self.pid = pid
        self.serial_number = serial_number
        self.location = location


@pytest.fixture
def comports(monkeypatch):
    """Script what the OS reports as attached serial ports."""

    def _set(*ports):
        monkeypatch.setattr(usb.list_ports, "comports", lambda: list(ports))

    return _set


# ── PortInfo ─────────────────────────────────────────────────────────────────


def test_bombercat_usb_id_is_recognised():
    assert make_port(vid=0x1209, pid=0x005E).matches_bombercat


def test_stock_mbed_profile_usb_id_is_recognised():
    """Sketches built against Arduino's Nano RP2040 profile keep its identity."""
    assert make_port(vid=0x2341, pid=0x005E).matches_bombercat


def test_foreign_usb_id_does_not_match():
    assert not make_port(vid=0x0403, pid=0x6001).matches_bombercat


def test_port_without_usb_ids_does_not_match():
    assert not make_port(vid=None, pid=None).matches_bombercat


@pytest.mark.parametrize(
    "device, description",
    [
        ("/dev/ttyS0", "n/a"),
        ("/dev/cu.Bluetooth-Incoming", "Bluetooth"),
        ("/dev/cu.debug-console", "debug-console"),
    ],
)
def test_noise_ports_are_not_candidates(device, description):
    assert not make_port(device=device, description=description).is_candidate


def test_usb_cdc_port_is_a_candidate():
    assert make_port(device="/dev/ttyACM0").is_candidate


# ── list_ports_info ──────────────────────────────────────────────────────────


def test_list_hides_noise_ports_by_default(comports):
    comports(
        _ComPort("/dev/ttyS0", "n/a", "n/a"),
        _ComPort("/dev/ttyACM0", "BomberCat", "USB", vid=0x1209, pid=0x005E),
    )
    assert [p.device for p in list_ports_info()] == ["/dev/ttyACM0"]


def test_list_includes_noise_ports_with_include_all(comports):
    comports(
        _ComPort("/dev/ttyS0", "n/a", "n/a"),
        _ComPort("/dev/ttyACM0", "BomberCat", "USB", vid=0x1209, pid=0x005E),
    )
    assert len(list_ports_info(include_all=True)) == 2


def test_bombercat_tagged_ports_sort_first(comports):
    comports(
        _ComPort("/dev/ttyACM0", "Generic CDC", "USB", vid=0x0403, pid=0x6001),
        _ComPort("/dev/ttyACM1", "BomberCat", "USB", vid=0x1209, pid=0x005E),
    )
    assert [p.device for p in list_ports_info()] == ["/dev/ttyACM1", "/dev/ttyACM0"]


def test_bombercat_ports_filters_to_tagged_only(comports):
    comports(
        _ComPort("/dev/ttyACM0", "Generic CDC", "USB", vid=0x0403, pid=0x6001),
        _ComPort("/dev/ttyACM1", "BomberCat", "USB", vid=0x1209, pid=0x005E),
    )
    assert [p.device for p in usb.bombercat_ports()] == ["/dev/ttyACM1"]


def test_missing_os_fields_do_not_crash_the_listing(comports):
    comports(_ComPort("/dev/ttyACM0", None, None))
    (port,) = list_ports_info()
    assert port.description == "" and port.hwid == ""


# ── find_devices: numbering ──────────────────────────────────────────────────


def test_only_tagged_ports_are_numbered_when_any_is_tagged():
    devices = find_devices(
        [
            make_port("/dev/ttyACM0", vid=0x0403, pid=0x6001, serial_number="AAA"),
            make_port("/dev/ttyACM1", serial_number="BBB"),
        ]
    )
    assert [(d.device_id, d.port) for d in devices] == [(1, "/dev/ttyACM1")]
    assert devices[0].usb_tagged


def test_every_candidate_is_numbered_when_none_is_tagged():
    """A board re-flashed to a generic identity must still be addressable."""
    devices = find_devices(
        [
            make_port("/dev/ttyACM0", vid=0x0403, pid=0x6001),
            make_port("/dev/ttyACM1", vid=0x0403, pid=0x6001),
        ]
    )
    assert [d.device_id for d in devices] == [1, 2]
    assert not any(d.usb_tagged for d in devices)


def test_ids_follow_the_usb_serial_not_the_port_order():
    """The tty number is assigned by the host in a non-deterministic order, so
    IDs must key off the USB serial to survive a replug."""
    ports = [
        make_port("/dev/ttyACM1", serial_number="BBB"),
        make_port("/dev/ttyACM0", serial_number="AAA"),
    ]
    first = {d.serial_number: d.device_id for d in find_devices(ports)}
    # Same two boards, ports swapped by the kernel after a replug.
    replugged = [
        make_port("/dev/ttyACM0", serial_number="BBB"),
        make_port("/dev/ttyACM1", serial_number="AAA"),
    ]
    assert first == {d.serial_number: d.device_id for d in find_devices(replugged)}


def test_usb_topology_orders_boards_without_a_serial_number():
    devices = find_devices(
        [
            make_port("/dev/ttyACM1", location="1-2.4:1.0"),
            make_port("/dev/ttyACM0", location="1-1.2:1.0"),
        ]
    )
    assert [d.port for d in devices] == ["/dev/ttyACM0", "/dev/ttyACM1"]


def test_port_path_is_the_last_resort_ordering():
    devices = find_devices([make_port("/dev/ttyACM1"), make_port("/dev/ttyACM0")])
    assert [d.port for d in devices] == ["/dev/ttyACM0", "/dev/ttyACM1"]


def test_no_ports_means_no_devices():
    assert find_devices([]) == []


# ── find_device / describe_devices ───────────────────────────────────────────


@pytest.fixture
def two_devices(monkeypatch):
    devices = [
        BomberCatDevice(1, "/dev/ttyACM0", serial_number="AAA"),
        BomberCatDevice(2, "/dev/ttyACM1", serial_number="BBB"),
    ]
    monkeypatch.setattr(usb, "find_devices", lambda *a, **k: list(devices))
    return devices


def test_find_device_selects_by_id(two_devices):
    assert find_device(2).port == "/dev/ttyACM1"


def test_find_device_defaults_to_the_first_board(two_devices):
    assert find_device().device_id == 1


def test_find_device_returns_none_for_an_unknown_id(two_devices):
    assert find_device(9) is None


def test_find_device_returns_none_with_nothing_attached(monkeypatch):
    monkeypatch.setattr(usb, "find_devices", lambda *a, **k: [])
    assert find_device(1) is None


def test_describe_devices_is_a_one_line_summary(two_devices):
    assert describe_devices() == "#1 /dev/ttyACM0, #2 /dev/ttyACM1"


def test_device_str_is_the_user_facing_label():
    assert str(BomberCatDevice(3, "/dev/ttyACM2")) == "BomberCat #3"


# ── open_serial ──────────────────────────────────────────────────────────────


def test_open_serial_bounds_writes_so_a_wedged_board_cannot_hang_the_cli(
    monkeypatch,
):
    captured = {}
    monkeypatch.setattr(usb.serial, "Serial", lambda **kw: captured.update(kw))
    open_serial("/dev/ttyACM0")

    assert captured["port"] == "/dev/ttyACM0"
    assert captured["baudrate"] == usb.DEFAULT_BAUDRATE
    assert captured["timeout"] == usb.DEFAULT_TIMEOUT
    assert captured["write_timeout"] == usb.DEFAULT_WRITE_TIMEOUT


# ── env-var USB id override ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [("0x1209", 0x1209), ("4617", 4617), ("nonsense", None), ("", None)],
)
def test_env_usb_id_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("BOMBERCAT_VID", raw)
    assert usb._env_id("BOMBERCAT_VID") == expected


def test_env_usb_id_unset(monkeypatch):
    monkeypatch.delenv("BOMBERCAT_VID", raising=False)
    assert usb._env_id("BOMBERCAT_VID") is None
