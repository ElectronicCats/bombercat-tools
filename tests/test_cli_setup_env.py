#!/usr/bin/env python3

# Electronic Cats
# test_cli_setup_env.py — `bombercat setup-env` (modules/utils/system_cli.py):
# the root gate, the udev rules it writes, the groups it joins, and the udev
# reload. Also pins the embedded rules text to the one the packages install, so
# a new USB id added in packaging/ cannot silently stop reaching this command.

import platform
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import flat
from modules.core.cli import cli, main_cli
from modules.utils import system_cli
from modules.utils.system_cli import UDEV_RULES, setup_env

PACKAGED_RULES = (
    Path(__file__).resolve().parents[1]
    / "packaging/debian/lib/udev/rules.d/99-bombercat.rules"
)


@pytest.fixture
def linux_root(monkeypatch, tmp_path):
    """A Linux box where we are root and every sub-process succeeds."""
    monkeypatch.setattr(system_cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(system_cli.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(system_cli, "RULES_PATH", tmp_path / "99-bombercat.rules")
    monkeypatch.setenv("SUDO_USER", "tester")
    monkeypatch.setattr(system_cli.grp, "getgrnam", lambda name: object())

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(system_cli.subprocess, "run", fake_run)
    return calls


# ── wiring ───────────────────────────────────────────────────────────────────


def test_setup_env_is_registered_on_linux(monkeypatch):
    monkeypatch.setattr("modules.core.cli.platform.system", lambda: "Linux")
    monkeypatch.setattr(sys, "argv", ["bombercat", "--help"])
    with pytest.raises(SystemExit):
        main_cli()

    assert "setup-env" in cli.commands


def test_the_embedded_rules_match_the_packaged_ones():
    """One source of truth: the file the .deb/Arch package ships."""
    assert UDEV_RULES == PACKAGED_RULES.read_text(encoding="utf-8")


# ── gates ────────────────────────────────────────────────────────────────────


def test_setup_env_refuses_to_run_off_linux(runner, monkeypatch):
    monkeypatch.setattr(system_cli.platform, "system", lambda: "Darwin")
    result = runner.invoke(setup_env)

    assert result.exit_code == 1
    assert "Linux-only" in flat(result.output)


@pytest.mark.skipif(platform.system() == "Windows", reason="POSIX euid only")
def test_setup_env_requires_root(runner, monkeypatch):
    monkeypatch.setattr(system_cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(system_cli.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(sys, "argv", ["/usr/bin/bombercat"])
    result = runner.invoke(setup_env)

    assert result.exit_code == 1
    assert "Root privileges required" in flat(result.output)
    assert "sudo /usr/bin/bombercat setup-env" in flat(result.output)


@pytest.mark.skipif(platform.system() == "Windows", reason="POSIX euid only")
def test_the_sudo_hint_from_a_checkout_is_runnable(runner, monkeypatch):
    """`sudo bombercat.py setup-env` would not run — name the interpreter."""
    monkeypatch.setattr(system_cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(system_cli.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(sys, "argv", ["bombercat.py"])
    result = runner.invoke(setup_env)

    assert f"sudo {sys.executable} " in flat(result.output)
    assert "bombercat.py setup-env" in flat(result.output)


# ── the happy path ───────────────────────────────────────────────────────────


def test_setup_env_writes_the_rules_and_joins_the_groups(runner, linux_root):
    result = runner.invoke(setup_env)

    assert result.exit_code == 0
    assert system_cli.RULES_PATH.read_text(encoding="utf-8") == UDEV_RULES
    assert ["usermod", "-aG", "dialout", "tester"] in linux_root
    assert ["usermod", "-aG", "plugdev", "tester"] in linux_root
    assert ["udevadm", "control", "--reload-rules"] in linux_root
    assert "Environment setup complete" in flat(result.output)


def test_setup_env_targets_the_user_behind_sudo(runner, linux_root, monkeypatch):
    monkeypatch.setenv("SUDO_USER", "darcko")
    result = runner.invoke(setup_env)

    assert ["usermod", "-aG", "dialout", "darcko"] in linux_root
    assert "'darcko' added to group 'dialout'" in flat(result.output)


def test_setup_env_creates_a_group_the_distro_does_not_ship(
    runner, linux_root, monkeypatch
):
    """plugdev is absent on Arch and Fedora — usermod would fail without it."""

    def only_dialout_exists(name):
        if name == "plugdev":
            raise KeyError(name)
        return object()

    monkeypatch.setattr(system_cli.grp, "getgrnam", only_dialout_exists)
    result = runner.invoke(setup_env)

    assert result.exit_code == 0
    assert ["groupadd", "plugdev"] in linux_root
    assert ["groupadd", "dialout"] not in linux_root


# ── failures ─────────────────────────────────────────────────────────────────


def test_setup_env_reports_rules_it_could_not_write(runner, linux_root, monkeypatch):
    def boom(*a, **k):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(system_cli.Path, "write_text", boom)
    result = runner.invoke(setup_env)

    assert result.exit_code == 1
    assert "Failed to install udev rules" in flat(result.output)
    assert "finished with errors" in flat(result.output)


def test_setup_env_reports_a_group_it_could_not_join(runner, linux_root, monkeypatch):
    def fake_run(argv, **kwargs):
        linux_root.append(argv)
        if argv[0] == "usermod":
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(system_cli.subprocess, "run", fake_run)
    result = runner.invoke(setup_env)

    assert result.exit_code == 1
    assert "Could not add user 'tester' to group 'dialout'" in flat(result.output)


def test_setup_env_survives_a_machine_without_udevadm(runner, linux_root, monkeypatch):
    """A container has no udevadm; the rules and groups still landed."""

    def fake_run(argv, **kwargs):
        linux_root.append(argv)
        if argv[0] == "udevadm":
            raise FileNotFoundError("udevadm")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(system_cli.subprocess, "run", fake_run)
    result = runner.invoke(setup_env)

    assert result.exit_code == 0
    assert "Could not reload udev rules" in flat(result.output)
