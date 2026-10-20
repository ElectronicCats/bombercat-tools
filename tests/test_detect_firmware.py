#!/usr/bin/env python3

# Electronic Cats
# test_detect_firmware.py — modules/core/firmwares.detect_firmware and
# resolve_status_port: the levelled, handshake-optional detection that powers
# `bombercat status`. Every case runs against a FakeLink / monkeypatched USB
# layer, no hardware. docs/GENERALIZE_CLI_PLAN.md §2.2, §5.1.

import pytest

from conftest import DeviceError, FakeLink, err, make_device, ok
from modules.core import firmwares as fw


@pytest.fixture
def fake_link(monkeypatch):
    """Point firmwares.DeviceLink at a supplied FakeLink."""

    def _use(link: FakeLink):
        monkeypatch.setattr(fw, "DeviceLink", lambda *a, **k: link)
        return link

    return _use


@pytest.fixture(autouse=True)
def no_busy_wait(monkeypatch):
    """The busy-retry window is a real wait (12 s); the suite must not pay it.

    Tests that care about the retry pass their own `busy_timeout`.
    """
    monkeypatch.setattr(fw, "BUSY_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(fw, "BUSY_RETRY_DELAY_SECONDS", 0.0)


# ── detection levels ─────────────────────────────────────────────────────────


def test_handshake_identifies_nfcgate_with_its_version(fake_link):
    fake_link(FakeLink({"info": ok(fw="0.9.8", fw_name="nfcgate")}, ping_ok=True))
    r = fw.detect_firmware("/dev/ttyACM0")

    assert r.confidence == fw.HANDSHAKE
    assert r.firmware.id == "nfcgate"
    assert r.version == "0.9.8"
    assert r.usb_present and r.confidence in (fw.HANDSHAKE, fw.INFERRED, fw.BANNER)


def test_handshake_trusts_an_explicit_fw_name(fake_link):
    """When a future firmware reports :fw_name we use it verbatim (§2.5)."""
    fake_link(FakeLink({"info": ok(fw="1.0.0", fw_name="nfcgate")}, ping_ok=True))
    r = fw.detect_firmware("/dev/ttyACM0")

    assert r.firmware.id == "nfcgate"
    assert r.version == "1.0.0"


def test_handshake_names_a_firmware_other_than_nfcgate(fake_link):
    """Six sketches answer the REPL now; each one names itself (§2.5)."""
    fake_link(FakeLink({"info": ok(fw="0.9.8", fw_name="detecttags")}, ping_ok=True))
    r = fw.detect_firmware("/dev/ttyACM0")

    assert r.confidence == fw.HANDSHAKE
    assert r.firmware.id == "detecttags"
    assert r.version == "0.9.8"


def test_handshake_identifies_detectreaders(fake_link):
    fake_link(FakeLink({"info": ok(fw="1.0.0", fw_name="detectreaders")}, ping_ok=True))
    r = fw.detect_firmware("/dev/ttyACM0")

    assert r.confidence == fw.HANDSHAKE
    assert r.firmware.id == "detectreaders"
    assert r.version == "1.0.0"


def test_handshake_identifies_magspoof(fake_link):
    fake_link(FakeLink({"info": ok(fw="1.1.1.0", fw_name="magspoof")}, ping_ok=True))
    r = fw.detect_firmware("/dev/ttyACM0")

    assert r.confidence == fw.HANDSHAKE
    assert r.firmware.id == "magspoof"
    assert r.version == "1.1.1.0"


def test_an_unnamed_repl_board_is_inferred_not_asserted(fake_link):
    """A pre-`fw_name` build — every NFCGate up to 0.9.7, i.e. what is flashed
    on the boards in the wild today.

    It still gets a name: NFCGate is the only firmware whose builds could
    answer the REPL before the field existed. What it must NOT get is
    handshake-level certainty, now that six firmwares answer the same ping.
    """
    fake_link(FakeLink({"info": ok(fw="0.9.7")}, ping_ok=True))
    r = fw.detect_firmware("/dev/ttyACM0")

    assert r.firmware.id == "nfcgate"
    assert r.confidence == fw.INFERRED
    assert r.confidence != fw.HANDSHAKE
    assert r.confidence in (
        fw.HANDSHAKE,
        fw.INFERRED,
        fw.BANNER,
    )  # named, just not certain
    assert r.version == "0.9.7"


def test_a_firmware_that_names_itself_something_unknown_is_not_inferred(fake_link):
    """Naming an unknown firmware is not the same as naming none.

    A board that says "martian" is a newer or custom image, not an old
    NFCGate, so the pre-`fw_name` inference must not fire for it.
    """
    fake_link(FakeLink({"info": ok(fw="9", fw_name="martian")}, ping_ok=True))
    r = fw.detect_firmware("/dev/ttyACM0")

    assert r.firmware is fw.UNKNOWN
    assert r.confidence == fw.USB
    assert r.confidence not in (fw.HANDSHAKE, fw.INFERRED, fw.BANNER)
    assert r.version == "9"  # what little we did learn survives


def test_banner_identifies_a_board_whose_repl_stays_silent(fake_link):
    """A board running an image built before its firmware grew the REPL.

    Every published .uf2 today is such an image, so this level must consider
    firmwares that `has_repl` marks as REPL-capable — filtering them out made
    the whole banner level unreachable.
    """
    fake_link(
        FakeLink(
            ping_ok=False,
            stream_lines=["booting", "Detect NFC tags with PN7150/60", "ready"],
        )
    )
    r = fw.detect_firmware("/dev/ttyACM0")

    assert r.confidence == fw.BANNER
    assert r.firmware.id == "detecttags"


def test_banner_identifies_the_repl_less_relay_pair(fake_link):
    """The legacy relay sketches have no REPL at all: banner or nothing."""
    fake_link(
        FakeLink(
            ping_ok=False,
            stream_lines=["BomberCat, yes Sir!", "Host Relay NFC", "Type help"],
        )
    )
    r = fw.detect_firmware("/dev/ttyACM0")

    assert r.firmware.id == "host_relay_nfc"
    assert r.confidence == fw.BANNER


def test_an_ambiguous_banner_names_nobody(fake_link):
    """Two firmwares matching is missing evidence, not a tie to break."""
    fake_link(
        FakeLink(
            ping_ok=False,
            stream_lines=["Host Relay NFC", "Client Relay NFC"],
        )
    )
    r = fw.detect_firmware("/dev/ttyACM0")

    assert r.firmware is fw.UNKNOWN
    assert r.confidence == fw.USB


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


def test_a_board_that_answers_ping_but_not_info_is_busy_not_nfcgate(fake_link):
    """A busy board is NOT an old firmware.

    The MifareClassic firmware blocks its whole control REPL while it sweeps
    a card (11 s measured on a 1K card), so a probe that lands in that window
    sees a board that answers `ping` but never `info`. Inferring "old NFCGate"
    from that made the CLI offer to flash MifareClassic over a perfectly
    healthy board — and the flash resets it, which is what the user saw as
    "the device disconnected".
    """
    fake_link(FakeLink({"info": err("timed out")}, ping_ok=True))
    r = fw.detect_firmware("/dev/ttyACM0", busy_timeout=0)

    assert r.busy is True
    assert r.firmware is fw.UNKNOWN
    assert r.confidence == fw.USB
    assert r.confidence != fw.INFERRED


def test_a_silent_but_present_board_is_not_inferred_either(fake_link):
    """Same story one level down: the board does not even answer `ping`.

    It may be mid-sweep; it must not be named after some other firmware. It
    stays UNKNOWN, but flagged as busy so the caller does not flash it.
    """
    fake_link(FakeLink(ping_ok=False, stream_lines=[]))
    r = fw.detect_firmware("/dev/ttyACM0", busy_timeout=0)

    assert r.busy is True
    assert r.firmware is fw.UNKNOWN
    assert r.confidence == fw.USB
    assert r.confidence != fw.INFERRED


def test_a_board_that_comes_back_is_identified_normally(fake_link):
    """The retry exists for a board that was busy, not for one that is away:
    a board that answers on a later probe is identified as usual."""

    class LateLink(FakeLink):
        def __init__(self):
            super().__init__(
                {"info": ok(fw="1.2.0.0", fw_name="mifareclassic")}, ping_ok=True
            )
            self.pings = 0

        def ping(self):
            self.pings += 1
            return self.pings > 1  # silent the first time (mid-sweep)

    link = LateLink()
    fake_link(link)
    r = fw.detect_firmware("/dev/ttyACM0", busy_timeout=5.0)

    assert r.confidence == fw.HANDSHAKE
    assert r.firmware.id == "mifareclassic"
    assert r.busy is False
    assert link.pings > 1  # it was asked again


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
