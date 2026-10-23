#!/usr/bin/env python3

# Electronic Cats
# test_cli_devtools.py — the dev-tooling wrapper `bombercat proto`
# (modules/proto/cli.py). It shells out to gen_proto.sh, so what is tested here
# is the wrapping: the checks made before spawning anything, the arguments
# handed over and the exit code passed back.

import subprocess

import pytest

from conftest import flat
from modules.proto import cli as proto_cli
from modules.proto.cli import proto


@pytest.fixture
def fake_run(monkeypatch):
    """Capture `subprocess.run` calls and script their return code."""
    calls = []

    def _set(returncode=0, raises=None):
        def _run(cmd, *a, **k):
            calls.append(list(cmd))
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(cmd, returncode)

        monkeypatch.setattr(subprocess, "run", _run)
        return calls

    return _set


# ── bombercat proto gen ──────────────────────────────────────────────────────


def test_proto_gen_runs_the_generator_script(runner, fake_run, tmp_path, monkeypatch):
    script = tmp_path / "gen_proto.sh"
    script.write_text("#!/bin/bash\n")
    monkeypatch.setattr(proto_cli, "GEN_PROTO", script)
    calls = fake_run(0)
    result = runner.invoke(proto, ["gen"])

    assert result.exit_code == 0
    assert calls == [["bash", str(script)]]
    assert "Protobuf sources regenerated" in flat(result.output)


def test_proto_gen_passes_the_generators_failure_through(
    runner, fake_run, tmp_path, monkeypatch
):
    script = tmp_path / "gen_proto.sh"
    script.write_text("#!/bin/bash\n")
    monkeypatch.setattr(proto_cli, "GEN_PROTO", script)
    fake_run(3)
    result = runner.invoke(proto, ["gen"])

    assert result.exit_code == 3
    assert "regenerated" not in result.output


def test_proto_gen_reports_a_missing_generator(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(proto_cli, "GEN_PROTO", tmp_path / "nope.sh")
    result = runner.invoke(proto, ["gen"])

    assert result.exit_code == 1
    assert "Generator script not found" in flat(result.output)


def test_gen_proto_script_ships_with_the_repo():
    """The default path is relative to the package; a move would break `gen`."""
    assert proto_cli.GEN_PROTO.name == "gen_proto.sh"
    assert proto_cli.GEN_PROTO.exists()


def test_proto_gen_explains_a_missing_bash(runner, fake_run, tmp_path, monkeypatch):
    script = tmp_path / "gen_proto.sh"
    script.write_text("#!/bin/bash\n")
    monkeypatch.setattr(proto_cli, "GEN_PROTO", script)
    fake_run(raises=FileNotFoundError("bash"))
    result = runner.invoke(proto, ["gen"])
    out = flat(result.output)

    assert result.exit_code == 1
    assert "bash is not installed" in out
    assert "sudo apt install bash" in out


def test_proto_gen_reports_a_clean_interruption_on_ctrl_c(
    runner, fake_run, tmp_path, monkeypatch
):
    script = tmp_path / "gen_proto.sh"
    script.write_text("#!/bin/bash\n")
    monkeypatch.setattr(proto_cli, "GEN_PROTO", script)
    fake_run(raises=KeyboardInterrupt)
    result = runner.invoke(proto, ["gen"])

    assert result.exit_code == 130
    assert "generation interrupted" in flat(result.output)
