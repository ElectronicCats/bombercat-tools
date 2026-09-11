#!/usr/bin/env python3

# Electronic Cats
# test_detection_cli_autoflash.py — the F4a plumbing from
# docs/AUTOFLASH_PLAN.md: `device_session(..., requires=...)`, the root
# `--auto-flash`/`--no-auto-flash` flag landing in `ctx.obj`, and
# `_policy_flag()` reading it back. `requires=None` (today's only caller
# shape, `tags`/`readers`/`mifare`/`magspoof`/`relay` included) must stay
# byte-for-byte identical to the pre-F4 behavior — the existing suite passing
# unmodified is the real proof of that; these tests cover the new branch.
# Wiring a real capability into those five call sites is F4b.

import click
import pytest

import modules.utils.detection_cli as detection_cli
from modules.core.ensure_firmware import EnsureOutcome
from modules.core.requirements import Requirement
from modules.utils.detection_cli import DetectionSpec, _policy_flag, device_session


class _StubLink:
    def __init__(self, port, trace=None):
        self.port = port
        self.opened = False
        self.closed = False

    def open(self):
        self.opened = True
        return self

    def close(self):
        self.closed = True

    def ping(self):
        return True


def _boom(*_a, **_k):
    raise AssertionError("must not be called")


# ── requires=None stays untouched ───────────────────────────────────────────


def test_device_session_requires_none_never_touches_ensure_firmware(monkeypatch):
    monkeypatch.setattr(detection_cli, "resolve_status_port", _boom)
    monkeypatch.setattr(detection_cli, "requirement_for", _boom)
    monkeypatch.setattr(detection_cli, "ensure_firmware", _boom)

    with device_session(
        lambda port, device_id: "/dev/fake0", _StubLink, "tags read", "DetectTags", None
    ) as (target, link):
        assert target == "/dev/fake0"
        assert link.opened
    assert link.closed


def test_detection_spec_requires_defaults_to_none():
    assert DetectionSpec.__dataclass_fields__["requires"].default is None


# ── requires=<capability> routes through ensure_firmware ───────────────────


def test_device_session_with_requires_routes_through_ensure_firmware(monkeypatch):
    seen = {}

    def _resolve_status_port(port, device_id):
        seen["resolve_status_port"] = (port, device_id)
        return "/dev/ttyACM0", True

    def _requirement_for(capability, command):
        seen["requirement_for"] = (capability, command)
        return Requirement(
            capability=capability, command=command, provider_id="detecttags"
        )

    def _ensure_firmware(port, usb_tagged, req, *, policy):
        seen["ensure_firmware"] = (port, usb_tagged, req, policy)
        return EnsureOutcome(port="/dev/ttyACM1", detection=None, flashed=True)

    monkeypatch.setattr(detection_cli, "resolve_status_port", _resolve_status_port)
    monkeypatch.setattr(detection_cli, "requirement_for", _requirement_for)
    monkeypatch.setattr(detection_cli, "ensure_firmware", _ensure_firmware)
    monkeypatch.setattr(
        detection_cli, "resolve_policy", lambda flag: f"policy-for-{flag}"
    )

    with click.Context(click.Command("root"), obj={"auto_flash": False}):
        with device_session(
            _boom, _StubLink, "tags read", "DetectTags", "/dev/x", 3, requires="tags"
        ) as (target, link):
            # ensure_firmware's outcome.port is what gets opened, not the
            # port resolve_status_port started with (D-3.5).
            assert target == "/dev/ttyACM1"
            assert link.opened

    assert seen["resolve_status_port"] == ("/dev/x", 3)
    assert seen["requirement_for"] == ("tags", "tags read")
    port, usb_tagged, req, policy = seen["ensure_firmware"]
    assert (port, usb_tagged) == ("/dev/ttyACM0", True)
    assert req.provider_id == "detecttags"
    assert policy == "policy-for-False"  # resolve_policy(_policy_flag())


# ── --auto-flash / --no-auto-flash / _policy_flag ───────────────────────────


def test_policy_flag_is_none_outside_a_click_context():
    assert _policy_flag() is None


@pytest.mark.parametrize(
    "args,expected",
    [([], None), (["--auto-flash"], True), (["--no-auto-flash"], False)],
)
def test_root_auto_flash_flag_lands_in_ctx_obj(runner, args, expected):
    from modules.core.cli import cli

    seen = {}

    @click.command("probe")
    def probe():
        seen["auto_flash"] = _policy_flag()

    cli.add_command(probe)
    try:
        result = runner.invoke(cli, [*args, "probe"])
    finally:
        del cli.commands["probe"]

    assert result.exit_code == 0
    assert seen["auto_flash"] is expected
