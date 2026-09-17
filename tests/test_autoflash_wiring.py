#!/usr/bin/env python3

# Electronic Cats
# test_autoflash_wiring.py — docs/AUTOFLASH_PLAN.md F4b: the real `requires=`
# capability wired into the five call points (tags/readers DetectionSpec,
# tags/mifare, magspoof, nfcgate — with capture inheriting the last one for
# free). `use_link` (tests/conftest.py) already stubs
# `detection_cli.resolve_status_port`/`ensure_firmware` so these run without
# touching hardware; here we additionally wrap `detection_cli.requirement_for`
# to record which capability each group actually asked for.

import modules.utils.detection_cli as detection_cli
from conftest import FakeLink
from modules.core.firmwares import (
    CAP_MAGSPOOF,
    CAP_MIFARE,
    CAP_READERS,
    CAP_RELAY,
    CAP_TAGS,
)
from modules.core.requirements import requirement_for as _real_requirement_for


def _spy_requirement_for(monkeypatch):
    """Wrap the real `requirement_for` so the session still resolves a valid
    Requirement, while recording every capability it was asked to resolve."""
    seen = []

    def _wrapped(capability, command):
        seen.append(capability)
        return _real_requirement_for(capability, command)

    monkeypatch.setattr(detection_cli, "requirement_for", _wrapped)
    return seen


def test_tags_requires_cap_tags(runner, use_link, monkeypatch):
    from modules.tags import cli as tagscli
    from modules.tags.cli import read_cmd

    seen = _spy_requirement_for(monkeypatch)
    use_link(tagscli, FakeLink(stream_lines=[]))

    runner.invoke(read_cmd, ["-t", "0.01"])

    assert seen == [CAP_TAGS]


def test_readers_requires_cap_readers(runner, use_link, monkeypatch):
    from modules.readers import cli as readerscli
    from modules.readers.cli import read_cmd

    seen = _spy_requirement_for(monkeypatch)
    use_link(readerscli, FakeLink(stream_lines=[]))

    runner.invoke(read_cmd, ["-t", "0.01"])

    assert seen == [CAP_READERS]


def test_mifare_requires_cap_mifare(runner, use_link, monkeypatch):
    from modules.tags.mifare import session as mifare_session
    from modules.tags.mifare.cli import mifare_auth_cmd

    seen = _spy_requirement_for(monkeypatch)
    use_link(mifare_session, FakeLink())

    runner.invoke(
        mifare_auth_cmd, ["--block", "4", "--key-type", "A", "--key", "FFFFFFFFFFFF"]
    )

    assert seen == [CAP_MIFARE]


def test_magspoof_requires_cap_magspoof(runner, use_link, monkeypatch):
    from modules.magspoof import cli as magspoofcli
    from modules.magspoof.cli import play_cmd

    seen = _spy_requirement_for(monkeypatch)
    use_link(magspoofcli, FakeLink())

    runner.invoke(play_cmd, [])

    assert seen == [CAP_MAGSPOOF]


def test_relay_requires_cap_relay(runner, use_link, monkeypatch):
    from modules.nfcgate import cli as nfc
    from modules.nfcgate.cli import config

    seen = _spy_requirement_for(monkeypatch)
    use_link(nfc, FakeLink())

    runner.invoke(config, ["show"])

    assert seen == [CAP_RELAY]


def test_capture_inherits_cap_relay_from_nfcgate(runner, use_link, monkeypatch):
    """`modules/capture/cli.py` imports `_device_session` straight from
    `nfcgate.cli` rather than building its own — so it inherits CAP_RELAY
    with no separate wiring (docs/AUTOFLASH_PLAN.md F4b table, note under it).
    The imported function still runs against `nfcgate.cli`'s own
    `resolve_port`/`DeviceLink`, so that's the module `use_link` must patch
    — not `modules.capture.cli` (see `fake_session` in tests/conftest.py).
    """
    from modules.capture.cli import capture
    from modules.nfcgate import cli as nfc

    seen = _spy_requirement_for(monkeypatch)
    use_link(nfc, FakeLink())

    runner.invoke(capture, ["stop"])

    assert seen == [CAP_RELAY]
