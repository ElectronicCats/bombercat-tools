#!/usr/bin/env python3

# Electronic Cats
# test_cli_root.py — the CLI's entry point (modules/core/cli.py): the header,
# command wiring, `bombercat identify` and `bombercat completion install`.
# Keeps the top-level surface honest: a command that stops being registered, or
# a completion script that stops being rewritten to an absolute path, fails here.

import platform
import subprocess
import sys

import pytest

from conftest import DeviceError, FakeLink, err, flat
from modules.core import cli as root
from modules.core.cli import cli, completion, identify_cmd, main_cli, print_header
from modules.utils._version import __version__


@pytest.fixture
def home(monkeypatch, tmp_path):
    """Redirect ``Path.home()`` so completion installs land in a temp dir."""
    monkeypatch.setattr(root.Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def click_completion_script(monkeypatch):
    """Stub the sub-process Click uses to emit a completion script."""

    def _set(script):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: type("R", (), {"stdout": script, "returncode": 0})(),
        )

    return _set


# ── header ───────────────────────────────────────────────────────────────────


def test_header_shows_the_version(capsys):
    print_header()
    out = flat(capsys.readouterr().out)

    assert f"v{__version__}" in out
    assert "Electronic Cats" in out


def test_header_names_the_module_being_run(capsys):
    print_header("capture")
    assert "bombercat capture" in flat(capsys.readouterr().out)


@pytest.mark.skipif(platform.system() == "Windows", reason="POSIX euid only")
def test_header_warns_when_running_as_root(capsys, monkeypatch):
    monkeypatch.setattr(root.os, "geteuid", lambda: 0, raising=False)
    print_header()

    assert "(root)" in flat(capsys.readouterr().out)


# ── group wiring ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "device",
        "identify",
        "status",  # now the FIRMWARE status (relay state moved under `relay`)
        "relay",
        "capture",
        "proto",
        "testserver",
        # deprecated root aliases, still registered (hidden) for one cycle
        "config",
        "run",
        "stop",
        "monitor",
    ],
)
def test_every_command_is_registered(monkeypatch, name):
    monkeypatch.setattr(sys, "argv", ["bombercat", "--help"])
    with pytest.raises(SystemExit) as e:
        main_cli()

    assert e.value.code == 0
    assert name in cli.commands


def test_relay_group_holds_the_nfcgate_subcommands(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bombercat", "--help"])
    with pytest.raises(SystemExit):
        main_cli()

    relay = cli.commands["relay"]
    assert set(relay.commands) == {"config", "run", "stop", "status", "monitor"}


@pytest.mark.parametrize("name", ["config", "run", "stop", "monitor"])
def test_deprecated_root_aliases_are_hidden(monkeypatch, name):
    monkeypatch.setattr(sys, "argv", ["bombercat", "--help"])
    with pytest.raises(SystemExit):
        main_cli()

    assert cli.commands[name].hidden is True


def test_status_is_the_firmware_status_command(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bombercat", "--help"])
    with pytest.raises(SystemExit):
        main_cli()

    assert cli.commands["status"].hidden is False
    assert "firmware is flashed" in (cli.commands["status"].help or "")


@pytest.mark.skipif(platform.system() == "Windows", reason="POSIX shells only")
def test_completion_is_offered_on_posix(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bombercat", "--help"])
    with pytest.raises(SystemExit):
        main_cli()

    assert "completion" in cli.commands


def test_help_is_available_as_both_h_and_help(runner):
    for flag in ("-h", "--help"):
        result = runner.invoke(cli, [flag])
        assert result.exit_code == 0
        assert "All in one bombercat tools environment" in flat(result.output)


def test_verbose_flag_raises_the_log_level(runner, monkeypatch):
    import logging

    monkeypatch.setattr(root.logger, "level", logging.WARNING)
    result = runner.invoke(cli, ["-v", "device", "--help"])

    assert result.exit_code == 0
    assert root.logger.level == logging.INFO


def test_header_is_skipped_while_click_generates_completions(monkeypatch):
    """Printing anything here would corrupt the completion script."""
    printed = []
    monkeypatch.setattr(
        root, "print_header", lambda module=None: printed.append(module)
    )
    monkeypatch.setenv("_BOMBERCAT_COMPLETE", "zsh_source")
    monkeypatch.setattr(sys, "argv", ["bombercat", "--help"])
    with pytest.raises(SystemExit):
        main_cli()

    assert printed == []


# ── identify ─────────────────────────────────────────────────────────────────


def test_identify_blinks_the_board(runner, use_link):
    link = use_link(root, FakeLink())
    result = runner.invoke(identify_cmd, [])

    assert result.exit_code == 0
    assert link.sent == ["identify"]
    assert "blinking its LED" in flat(result.output)


def test_identify_reports_a_board_that_will_not_handshake(runner, use_link):
    use_link(root, FakeLink(ping_ok=False))
    result = runner.invoke(identify_cmd, [])

    assert result.exit_code == 1
    assert "did not answer the handshake" in flat(result.output)


def test_identify_points_at_a_firmware_that_lacks_the_command(runner, use_link):
    use_link(root, FakeLink({"identify": err("unknown command")}))
    result = runner.invoke(identify_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 1
    assert "identify failed" in out
    assert "predates `identify`" in out and "reflash" in out


def test_identify_reports_an_unresolvable_target(runner, monkeypatch):
    def _boom(*a, **k):
        raise DeviceError("multiple BomberCats found")

    monkeypatch.setattr(root, "resolve_port", _boom)
    result = runner.invoke(identify_cmd, [])

    assert result.exit_code == 1
    assert "multiple BomberCats found" in flat(result.output)


def test_identify_reports_an_unexpected_error_without_a_traceback(runner, monkeypatch):
    monkeypatch.setattr(root, "resolve_port", lambda *a, **k: "/dev/ttyACM0")
    monkeypatch.setattr(
        root,
        "DeviceLink",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError("[Errno 13]")),
    )
    result = runner.invoke(identify_cmd, [])

    assert result.exit_code == 1
    assert "PermissionError:" in flat(result.output)
    assert "Traceback" not in result.output


# ── completion install ───────────────────────────────────────────────────────


@pytest.mark.skipif(platform.system() == "Windows", reason="POSIX shells only")
def test_completion_install_writes_a_bash_script(runner, home, click_completion_script):
    click_completion_script(
        "_BOMBERCAT_COMPLETE=bash_complete $1\n"
        "complete -o nosort -F _bombercat_completion bombercat\n"
    )
    result = runner.invoke(completion, ["install", "--shell", "bash"])
    target = home / ".local/share/bash-completion/completions/bombercat"

    assert result.exit_code == 0
    assert target.exists()
    script = target.read_text()
    # The bare program name is rewritten to an absolute "python …/bombercat.py"
    # invocation, so completion works whether or not bombercat is on PATH.
    assert "_BOMBERCAT_COMPLETE=bash_complete bombercat\n" not in script
    assert str(root.Path(sys.executable).resolve()) in script
    assert (
        "complete -o nosort -F _bombercat_completion bombercat bombercat.py" in script
    )
    assert "Completion script written to" in flat(result.output)


@pytest.mark.skipif(platform.system() == "Windows", reason="POSIX shells only")
def test_completion_install_adds_the_zsh_fpath_entry_once(
    runner, home, click_completion_script
):
    click_completion_script(
        "#compdef bombercat\ncompdef _bombercat_completion bombercat\n"
    )
    runner.invoke(completion, ["install", "--shell", "zsh"])
    zshrc = home / ".zshrc"

    assert (home / ".zfunc/_bombercat").exists()
    assert "fpath=(~/.zfunc $fpath)" in zshrc.read_text()

    # A second install must not append the same block again.
    result = runner.invoke(completion, ["install", "--shell", "zsh"])
    assert zshrc.read_text().count("fpath=(~/.zfunc $fpath)") == 1
    assert "already in fpath" in flat(result.output)


@pytest.mark.skipif(platform.system() == "Windows", reason="POSIX shells only")
def test_completion_install_writes_a_fish_script(runner, home, click_completion_script):
    click_completion_script("complete --command bombercat\n")
    result = runner.invoke(completion, ["install", "--shell", "fish"])

    assert result.exit_code == 0
    assert (home / ".config/fish/completions/bombercat.fish").exists()


@pytest.mark.skipif(platform.system() == "Windows", reason="POSIX shells only")
def test_completion_install_detects_the_shell_from_the_environment(
    runner, home, click_completion_script, monkeypatch
):
    monkeypatch.setenv("SHELL", "/usr/bin/fish")
    click_completion_script("complete --command bombercat\n")
    result = runner.invoke(completion, ["install"])

    assert result.exit_code == 0
    assert "Detected shell: fish" in flat(result.output)


def test_completion_install_asks_for_a_shell_it_cannot_detect(
    runner, home, monkeypatch
):
    monkeypatch.setenv("SHELL", "/bin/somethingelse")
    monkeypatch.setattr(root.platform, "system", lambda: "Linux")
    result = runner.invoke(completion, ["install"])

    assert result.exit_code == 1
    assert "Could not detect shell" in flat(result.output)


def test_completion_install_rejects_an_unknown_shell(runner):
    assert runner.invoke(completion, ["install", "--shell", "csh"]).exit_code == 2


def test_completion_install_reports_an_empty_script(
    runner, home, click_completion_script, monkeypatch
):
    monkeypatch.setattr(root.platform, "system", lambda: "Linux")
    click_completion_script("   \n")
    result = runner.invoke(completion, ["install", "--shell", "bash"])

    assert result.exit_code == 1
    assert "Empty completion script" in flat(result.output)


def test_completion_install_is_not_supported_on_windows(runner, monkeypatch):
    monkeypatch.setattr(root.platform, "system", lambda: "Windows")
    result = runner.invoke(completion, ["install", "--shell", "bash"])

    assert result.exit_code == 1
    assert "not supported on Windows" in flat(result.output)
