#!/usr/bin/env python3

# Electronic Cats
# test_cli_status.py — `bombercat status` (modules/core/cli.py), the new
# firmware-reporting status, plus the hidden deprecation aliases that forward
# the old root spellings to `relay …`. docs/GENERALIZE_CLI_PLAN.md §2.3–2.4, §5.2.

import pytest

from conftest import DeviceError, FakeLink, flat, ok
from modules.core import cli as root
from modules.core import firmwares as fw
from modules.core.cli import firmware_status_cmd


@pytest.fixture
def detect(monkeypatch):
    """Make `bombercat status` see a chosen DetectionResult on a known port."""

    def _use(result: fw.DetectionResult, target: str = "/dev/ttyACM0", tagged=True):
        monkeypatch.setattr(
            root, "resolve_status_port", lambda *a, **k: (target, tagged)
        )
        monkeypatch.setattr(root, "detect_firmware", lambda *a, **k: result)
        return result

    return _use


def _result(fw_id, confidence, version=None, usb=True):
    return fw.DetectionResult(
        firmware=fw.by_id(fw_id),
        confidence=confidence,
        port="/dev/ttyACM0",
        version=version,
        usb_present=usb,
    )


# ── firmware status ──────────────────────────────────────────────────────────


def test_status_reports_nfcgate_by_handshake(runner, detect):
    detect(_result("nfcgate", fw.HANDSHAKE, version="0.9.7"))
    result = runner.invoke(firmware_status_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "NFCGate" in out and "0.9.7" in out
    assert "handshake" in out
    assert "relay" in out  # capability + suggested next command
    assert "bombercat relay run" in out


def test_status_marks_an_inferred_name_as_not_certain(runner, detect):
    """A pre-`fw_name` board: named, but the table must not claim certainty."""
    detect(_result("nfcgate", fw.INFERRED, version="0.9.7"))
    result = runner.invoke(firmware_status_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "NFCGate" in out and "0.9.7" in out
    assert "inferred" in out
    assert "handshake (certain)" not in out
    assert "does not report a firmware name" in out
    assert "bombercat relay run" in out  # still a usable NFCGate board


def test_status_reports_a_banner_detected_firmware(runner, detect):
    detect(_result("detecttags", fw.BANNER))
    result = runner.invoke(firmware_status_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "DetectTags" in out
    assert "banner" in out
    # honest: a REPL-less board is never pointed at the relay controls
    assert "bombercat relay run" not in out


def test_status_suggests_tags_commands_for_detecttags(runner, detect):
    detect(_result("detecttags", fw.HANDSHAKE, version="1.0.0"))
    result = runner.invoke(firmware_status_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "bombercat tags read" in out
    assert "bombercat tags watch" in out
    # honest: a REPL-less-for-relay board is never pointed at the relay controls
    assert "bombercat relay run" not in out


def test_status_suggests_readers_commands_for_detectreaders(runner, detect):
    detect(_result("detectreaders", fw.HANDSHAKE, version="1.0.0"))
    result = runner.invoke(firmware_status_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "bombercat readers read" in out
    assert "bombercat readers watch" in out
    # honest: a REPL-less-for-relay board is never pointed at the relay controls
    assert "bombercat relay run" not in out


def test_status_suggests_magspoof_commands_for_magspoof(runner, detect):
    detect(_result("magspoof", fw.HANDSHAKE, version="1.1.1.0"))
    result = runner.invoke(firmware_status_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "bombercat magspoof play" in out
    assert "bombercat magspoof card list" in out
    # honest: a REPL-less-for-relay board is never pointed at the relay controls
    assert "bombercat relay run" not in out


def test_status_reports_an_unidentified_but_present_board(runner, detect):
    detect(_result("unknown", fw.USB))
    result = runner.invoke(firmware_status_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "could not be identified" in out
    assert "bombercat flash --list" in out  # steer at flashing, not a guess


def test_status_fails_cleanly_when_nothing_answers(runner, detect):
    detect(_result("unknown", fw.NONE, usb=False))
    result = runner.invoke(firmware_status_cmd, [])

    assert result.exit_code == 1
    assert "nothing responded" in flat(result.output)


def test_status_reports_an_unresolvable_target(runner, monkeypatch):
    monkeypatch.setattr(
        root,
        "resolve_status_port",
        lambda *a, **k: (_ for _ in ()).throw(DeviceError("no BomberCat found")),
    )
    result = runner.invoke(firmware_status_cmd, [])

    assert result.exit_code == 1
    assert "no BomberCat found" in flat(result.output)


def test_status_passes_no_sniff_through_to_detection(runner, monkeypatch):
    seen = {}

    def _detect(port, sniff=True, usb_present=True):
        seen["sniff"] = sniff
        return _result("nfcgate", fw.HANDSHAKE, version="1")

    monkeypatch.setattr(root, "resolve_status_port", lambda *a, **k: ("/dev/x", True))
    monkeypatch.setattr(root, "detect_firmware", _detect)
    runner.invoke(firmware_status_cmd, ["--no-sniff"])

    assert seen["sniff"] is False


# ── deprecation aliases ──────────────────────────────────────────────────────


def test_run_alias_forwards_and_warns(runner, use_link, monkeypatch):
    # The alias reuses the real `run` callback, which reaches hardware through
    # the nfcgate module; patch there (and don't wait on the status poll).
    from modules.nfcgate import cli as nfc

    use_link(nfc, FakeLink({"run": ok("accepted"), "status": ok(state="relaying")}))
    monkeypatch.setattr(nfc.time, "sleep", lambda _s: None)

    alias = root._relay_alias(root._run, "relay run")
    result = runner.invoke(alias, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "deprecated" in out and "bombercat relay run" in out
    assert "relay started" in out


def test_config_alias_group_warns_and_keeps_subcommands(runner, use_link):
    from modules.nfcgate import cli as nfc

    use_link(nfc, FakeLink())
    alias = root._config_alias()
    assert set(alias.commands) == {"wifi", "nfcgate", "show"}

    result = runner.invoke(alias, ["wifi", "--ssid", "HomeNet"])
    out = flat(result.output)
    assert result.exit_code == 0
    assert "deprecated" in out and "relay config" in out
    assert "saved to flash" in out
