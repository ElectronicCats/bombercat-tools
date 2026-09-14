#!/usr/bin/env python3

# Electronic Cats
# test_ensure_firmware.py — the auto-flash orchestrator
# (modules/core/ensure_firmware.py, docs/AUTOFLASH_PLAN.md F3).
# Every case is a double: no hardware, no network, no serial. Coverage
# follows the confidence table in D-3.6 and the consent policy in D-3.4/D-3.5.

import pytest

from modules.core import ensure_firmware as ef
from modules.core.bombercat import DeviceError
from modules.core.exceptions import EXIT_FIRMWARE, FirmwareMismatch
from modules.core.firmwares import (
    BANNER,
    CAP_TAGS,
    HANDSHAKE,
    INFERRED,
    NONE,
    REGISTRY,
    UNKNOWN,
    USB,
    DetectionResult,
)
from modules.core.requirements import requirement_for
from modules.firmware.flasher import FlashOutcome
from modules.firmware.releases import FirmwareError
from modules.firmware.uf2 import BootloaderTimeout

PORT = "/dev/ttyACM0"
NEW_PORT = "/dev/ttyACM1"

REQ = requirement_for(CAP_TAGS, command="tags read")  # provider: detecttags
DETECTTAGS = REGISTRY["detecttags"]  # satisfies CAP_TAGS
NFCGATE = REGISTRY["nfcgate"]  # does not satisfy CAP_TAGS


def detection(firmware, confidence, port=PORT):
    return DetectionResult(
        firmware=firmware, confidence=confidence, port=port, usb_present=True
    )


class FakeDetect:
    """Returns each queued detection once, then repeats the last forever —
    enough to drive both the first check and the post-flash retry loop."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, port, sniff=True, usb_present=True):
        self.calls += 1
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]


class FakeFlash:
    def __init__(self, outcome=None):
        self.calls = []
        self.outcome = outcome or FlashOutcome(
            image=None, drive=None, touched=True, port=NEW_PORT
        )

    def __call__(self, req, port):
        self.calls.append((req, port))
        return self.outcome


# ── D-3.6 confidence table ───────────────────────────────────────────────────


def test_none_confidence_is_a_device_error_not_a_firmware_one():
    flash = FakeFlash()
    with pytest.raises(DeviceError):
        ef.ensure_firmware(
            PORT,
            True,
            REQ,
            policy=ef.AutoFlashPolicy.ALWAYS,
            detect_fn=lambda *a, **k: detection(UNKNOWN, NONE),
            flash_fn=flash,
        )
    assert flash.calls == []


@pytest.mark.parametrize("confidence", [HANDSHAKE, INFERRED, BANNER])
def test_satisfied_capability_never_flashes(confidence, monkeypatch):
    warnings = []
    monkeypatch.setattr(ef, "print_warning", warnings.append)
    flash = FakeFlash()

    outcome = ef.ensure_firmware(
        PORT,
        True,
        REQ,
        policy=ef.AutoFlashPolicy.NEVER,
        detect_fn=lambda *a, **k: detection(DETECTTAGS, confidence),
        flash_fn=flash,
    )

    assert outcome == ef.EnsureOutcome(
        port=PORT, detection=detection(DETECTTAGS, confidence), flashed=False
    )
    assert flash.calls == []
    # BANNER is the only satisfied row that warns instead of staying silent.
    assert bool(warnings) == (confidence == BANNER)


@pytest.mark.parametrize("confidence", [HANDSHAKE, INFERRED, BANNER])
def test_unsatisfied_capability_under_never_raises_without_flashing(confidence):
    flash = FakeFlash()
    with pytest.raises(FirmwareMismatch) as exc:
        ef.ensure_firmware(
            PORT,
            True,
            REQ,
            policy=ef.AutoFlashPolicy.NEVER,
            detect_fn=lambda *a, **k: detection(NFCGATE, confidence),
            flash_fn=flash,
        )
    assert flash.calls == []
    assert exc.value.hint == ["bombercat flash DetectTags"]
    assert ("inferred" in str(exc.value).lower()) == (confidence == INFERRED)


def test_mismatch_off_nfcgate_warns_about_losing_its_config():
    # F6/R3: flashing over NFCGate specifically loses its saved WiFi/relay
    # config, unlike swapping between the other single-purpose firmwares.
    flash = FakeFlash()
    with pytest.raises(FirmwareMismatch) as exc:
        ef.ensure_firmware(
            PORT,
            True,
            REQ,
            policy=ef.AutoFlashPolicy.NEVER,
            detect_fn=lambda *a, **k: detection(NFCGATE, HANDSHAKE),
            flash_fn=flash,
        )
    assert "bombercat config show" in str(exc.value)


def test_mismatch_off_a_non_nfcgate_firmware_has_no_config_warning():
    flash = FakeFlash()
    with pytest.raises(FirmwareMismatch) as exc:
        ef.ensure_firmware(
            PORT,
            True,
            REQ,
            policy=ef.AutoFlashPolicy.NEVER,
            detect_fn=lambda *a, **k: detection(REGISTRY["magspoof"], HANDSHAKE),
            flash_fn=flash,
        )
    assert "config show" not in str(exc.value)


def test_usb_confidence_requires_confirmation_even_under_always_policy():
    detect = FakeDetect(detection(UNKNOWN, USB), detection(DETECTTAGS, HANDSHAKE))
    flash = FakeFlash()
    asked = []

    outcome = ef.ensure_firmware(
        PORT,
        True,
        REQ,
        policy=ef.AutoFlashPolicy.ALWAYS,
        detect_fn=detect,
        flash_fn=flash,
        confirm_fn=lambda q: asked.append(q) or True,
    )

    assert asked, "ALWAYS alone must not be enough for a USB-only identification"
    assert flash.calls == [(REQ, PORT)]
    assert outcome.flashed is True
    assert outcome.port == NEW_PORT


def test_usb_confidence_declining_confirmation_does_not_flash():
    flash = FakeFlash()
    with pytest.raises(FirmwareMismatch):
        ef.ensure_firmware(
            PORT,
            True,
            REQ,
            policy=ef.AutoFlashPolicy.ALWAYS,
            detect_fn=lambda *a, **k: detection(UNKNOWN, USB),
            flash_fn=flash,
            confirm_fn=lambda q: False,
        )
    assert flash.calls == []


# ── Consent policy (D-3.4/D-3.5) ─────────────────────────────────────────────


def test_ask_policy_flashes_only_after_confirmation():
    detect = FakeDetect(detection(NFCGATE, HANDSHAKE), detection(DETECTTAGS, HANDSHAKE))
    flash = FakeFlash()

    outcome = ef.ensure_firmware(
        PORT,
        True,
        REQ,
        policy=ef.AutoFlashPolicy.ASK,
        detect_fn=detect,
        flash_fn=flash,
        confirm_fn=lambda q: True,
    )

    assert flash.calls == [(REQ, PORT)]
    assert outcome.flashed is True


def test_ask_policy_declining_confirmation_raises_without_flashing():
    flash = FakeFlash()
    with pytest.raises(FirmwareMismatch):
        ef.ensure_firmware(
            PORT,
            True,
            REQ,
            policy=ef.AutoFlashPolicy.ASK,
            detect_fn=lambda *a, **k: detection(NFCGATE, HANDSHAKE),
            flash_fn=flash,
            confirm_fn=lambda q: False,
        )
    assert flash.calls == []


def test_always_policy_with_non_usb_confidence_flashes_without_asking():
    detect = FakeDetect(detection(NFCGATE, HANDSHAKE), detection(DETECTTAGS, HANDSHAKE))
    flash = FakeFlash()
    asked = []

    outcome = ef.ensure_firmware(
        PORT,
        True,
        REQ,
        policy=ef.AutoFlashPolicy.ALWAYS,
        detect_fn=detect,
        flash_fn=flash,
        confirm_fn=lambda q: asked.append(q) or True,
    )

    assert asked == []
    assert flash.calls == [(REQ, PORT)]
    assert outcome.flashed is True


def test_bootloader_timeout_shows_the_help_panel_instead_of_a_bare_message(
    monkeypatch,
):
    # docs/AUTOFLASH_PLAN.md F5: a board that never reaches BOOTSEL (no
    # udisks2 to auto-mount RPI-RP2, or a firmware without the 1200-bps
    # touch) must show the same actionable panel `bombercat flash` shows,
    # not the bare BootloaderTimeout message main_cli()'s generic
    # `except BomberCatError` would otherwise print.
    def raising_flash(req, port):
        raise BootloaderTimeout("no RPI-RP2 drive appeared within 15 s.")

    seen = []
    monkeypatch.setattr(
        ef, "bootloader_help", lambda image_name: seen.append(image_name)
    )

    with pytest.raises(SystemExit) as excinfo:
        ef.ensure_firmware(
            PORT,
            True,
            REQ,
            policy=ef.AutoFlashPolicy.ALWAYS,
            detect_fn=lambda *a, **k: detection(NFCGATE, HANDSHAKE),
            flash_fn=raising_flash,
            confirm_fn=lambda q: True,
        )

    assert seen == [REQ.image_name]
    assert excinfo.value.code == EXIT_FIRMWARE


# ── Post-flash port and re-verification (D-3.5/D-3.7) ────────────────────────


def test_flash_outcome_with_no_port_raises_instead_of_continuing():
    flash = FakeFlash(
        outcome=FlashOutcome(image=None, drive=None, touched=True, port=None)
    )
    with pytest.raises(FirmwareError):
        ef.ensure_firmware(
            PORT,
            True,
            REQ,
            policy=ef.AutoFlashPolicy.ALWAYS,
            detect_fn=lambda *a, **k: detection(NFCGATE, HANDSHAKE),
            flash_fn=flash,
            confirm_fn=lambda q: True,
        )


def test_successful_reverification_returns_the_new_port():
    detect = FakeDetect(
        detection(NFCGATE, HANDSHAKE), detection(DETECTTAGS, HANDSHAKE, port=NEW_PORT)
    )
    flash = FakeFlash()

    outcome = ef.ensure_firmware(
        PORT,
        True,
        REQ,
        policy=ef.AutoFlashPolicy.ALWAYS,
        detect_fn=detect,
        flash_fn=flash,
        confirm_fn=lambda q: True,
        verify_delay=0,
    )

    assert outcome.port == NEW_PORT
    assert outcome.detection.firmware is DETECTTAGS


def test_failed_reverification_warns_but_does_not_raise(monkeypatch):
    warnings = []
    monkeypatch.setattr(ef, "print_warning", warnings.append)
    # Only the pre-flash detection is queued; every post-flash check (the
    # first one plus every retry) keeps returning it — never satisfied.
    detect = FakeDetect(detection(NFCGATE, HANDSHAKE))
    flash = FakeFlash()

    outcome = ef.ensure_firmware(
        PORT,
        True,
        REQ,
        policy=ef.AutoFlashPolicy.ALWAYS,
        detect_fn=detect,
        flash_fn=flash,
        confirm_fn=lambda q: True,
        verify_retries=1,
        verify_delay=0,
    )

    assert outcome.flashed is True
    assert any("could not confirm" in w for w in warnings)


# ── resolve_policy (D-3.4) ────────────────────────────────────────────────────


class _FakeStdin:
    def __init__(self, is_tty):
        self._is_tty = is_tty

    def isatty(self):
        return self._is_tty


def test_resolve_policy_flag_wins_over_everything(monkeypatch):
    monkeypatch.setenv(ef.AUTO_FLASH_ENV, "never")
    assert ef.resolve_policy(True) == ef.AutoFlashPolicy.ALWAYS
    assert ef.resolve_policy(False) == ef.AutoFlashPolicy.NEVER


def test_resolve_policy_env_wins_over_tty_state(monkeypatch):
    monkeypatch.setenv(ef.AUTO_FLASH_ENV, "always")
    monkeypatch.setattr(ef.sys, "stdin", _FakeStdin(False))
    assert ef.resolve_policy(None) == ef.AutoFlashPolicy.ALWAYS


def test_resolve_policy_defaults_to_ask_on_a_tty(monkeypatch):
    monkeypatch.delenv(ef.AUTO_FLASH_ENV, raising=False)
    monkeypatch.setattr(ef.sys, "stdin", _FakeStdin(True))
    assert ef.resolve_policy(None) == ef.AutoFlashPolicy.ASK


def test_resolve_policy_defaults_to_never_off_a_tty(monkeypatch):
    monkeypatch.delenv(ef.AUTO_FLASH_ENV, raising=False)
    monkeypatch.setattr(ef.sys, "stdin", _FakeStdin(False))
    assert ef.resolve_policy(None) == ef.AutoFlashPolicy.NEVER
