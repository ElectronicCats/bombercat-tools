#!/usr/bin/env python3

# Electronic Cats
# test_cli_devtools.py — the dev-tooling wrappers: `bombercat proto`
# (modules/proto/cli.py) and `bombercat testserver` (modules/testserver/cli.py).
# Both shell out — to gen_proto.sh, to Docker, to the verifier — so what is
# tested here is the wrapping: the checks made before spawning anything, the
# arguments handed over, the exit code passed back, and how the verifier's JSON
# report is rendered. No container is ever started.

import json
import subprocess
import sys

import pytest

from conftest import flat
from modules.proto import cli as proto_cli
from modules.proto.cli import proto
from modules.testserver import cli as ts

# Imported under an alias: a module-level name starting with "test" would be
# picked up by pytest's collector.
from modules.testserver.cli import testserver as ts_group


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


# ── testserver: the protobuf interpreter ─────────────────────────────────────


def test_smoketest_python_prefers_the_running_interpreter(monkeypatch):
    monkeypatch.setattr(ts, "_has_protobuf", lambda python: True)
    assert ts._smoketest_python() == sys.executable


def test_smoketest_python_falls_back_to_the_bootstrapped_venv(monkeypatch, tmp_path):
    venv_python = tmp_path / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.touch()
    monkeypatch.setattr(ts, "SMOKE_VENV", tmp_path)
    monkeypatch.setattr(ts, "_has_protobuf", lambda python: python != sys.executable)

    assert ts._smoketest_python() == str(venv_python)


def test_smoketest_python_explains_a_failed_bootstrap(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ts, "SMOKE_VENV", tmp_path / "venv")
    monkeypatch.setattr(ts, "_has_protobuf", lambda python: False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no venv module")),
    )
    with pytest.raises(SystemExit) as e:
        ts._smoketest_python()

    assert e.value.code == 1
    assert "could not prepare the protobuf runtime" in flat(capsys.readouterr().err)


# ── testserver verify / smoke ────────────────────────────────────────────────


@pytest.fixture
def no_preflight(monkeypatch):
    """Skip the git-clone / Docker checks: they are not what these tests cover."""
    monkeypatch.setattr(ts.preflight, "check_server_sources", lambda *a, **k: None)
    monkeypatch.setattr(ts, "_smoketest_python", lambda: sys.executable)


def test_verify_reports_a_missing_verifier(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "VERIFY", tmp_path / "nope.py")
    result = runner.invoke(ts_group, ["verify"])

    assert result.exit_code == 1
    assert "Verifier not found" in flat(result.output)


def test_verify_defaults_to_the_local_server(runner, no_preflight, monkeypatch):
    seen = {}

    def _render(python, host, port, rounds):
        seen.update(host=host, port=port, rounds=rounds)
        return 0

    monkeypatch.setattr(ts, "_render_verify", _render)
    result = runner.invoke(ts_group, ["verify"])

    assert result.exit_code == 0
    assert seen == {"host": "127.0.0.1", "port": 5566, "rounds": 8}


def test_verify_passes_host_port_and_rounds_through(runner, no_preflight, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        ts,
        "_render_verify",
        lambda python, host, port, rounds: seen.update(
            host=host, port=port, rounds=rounds
        )
        or 0,
    )
    runner.invoke(ts_group, ["verify", "10.0.0.9", "6000", "-n", "3"])

    assert seen == {"host": "10.0.0.9", "port": 6000, "rounds": 3}


def test_smoke_reports_a_missing_test(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "SMOKETEST", tmp_path / "nope.py")
    result = runner.invoke(ts_group, ["smoke"])

    assert result.exit_code == 1
    assert "Smoke test not found" in flat(result.output)


def test_smoke_runs_the_relay_test_against_the_given_server(
    runner, no_preflight, fake_run
):
    calls = fake_run(0)
    result = runner.invoke(ts_group, ["smoke", "10.0.0.9", "6000"])

    assert result.exit_code == 0
    assert calls[-1] == [sys.executable, str(ts.SMOKETEST), "10.0.0.9", "6000"]


def test_smoke_passes_the_tests_exit_code_through(runner, no_preflight, fake_run):
    fake_run(2)
    assert runner.invoke(ts_group, ["smoke"]).exit_code == 2


# ── testserver verify: rendering the verifier's report ───────────────────────


def _verifier(*events, returncode=0):
    """A `subprocess.Popen` stand-in that replays JSON-line events."""

    class _Proc:
        def __init__(self, *a, **k):
            self.stdout = iter(
                [e if isinstance(e, str) else json.dumps(e) for e in events]
            )

        def wait(self):
            return returncode

    return _Proc


ROUND = {
    "event": "round",
    "i": 1,
    "first_len": 5,
    "whole": True,
    "gap_ms": 0.12,
    "total_ms": 3.4,
}


def test_render_verify_prints_the_passing_verdict(monkeypatch, capsys):
    monkeypatch.setattr(
        subprocess,
        "Popen",
        _verifier(
            {"event": "start", "session": 0x2A, "rounds": 1},
            ROUND,
            {
                "event": "result",
                "verdict": "present",
                "headline": "Latency patch is active",
                "notes": ["frames arrive whole"],
                "split": 0,
                "rounds": 1,
                "median_gap_ms": 0.12,
                "median_total_ms": 3.4,
            },
        ),
    )
    rc = ts._render_verify(sys.executable, "127.0.0.1", 5566, 1)
    out = flat(capsys.readouterr().out)

    assert rc == 0
    assert "session 0x2A · 1 rounds" in out
    assert "Latency patch is active" in out
    assert "median relay round trip" in out


def test_render_verify_reports_a_missing_patch(monkeypatch, capsys):
    monkeypatch.setattr(
        subprocess,
        "Popen",
        _verifier(
            {
                "event": "result",
                "verdict": "missing",
                "headline": "Server splits frames",
                "notes": ["the header arrives on its own", "rebuild the image"],
                "split": 8,
                "rounds": 8,
                "median_gap_ms": 40.0,
                "median_total_ms": 44.0,
                "fix": ["docker compose build"],
            },
            returncode=2,
        ),
    )
    rc = ts._render_verify(sys.executable, "127.0.0.1", 5566, 8)
    out = flat(capsys.readouterr().out)

    assert rc == 2
    assert "Server splits frames" in out
    assert "docker compose build" in out


def test_render_verify_surfaces_a_verifier_error(monkeypatch, capsys):
    monkeypatch.setattr(
        subprocess,
        "Popen",
        _verifier(
            {
                "event": "error",
                "message": "connection refused",
                "fix": ["bombercat testserver run"],
            },
            returncode=0,
        ),
    )
    rc = ts._render_verify(sys.executable, "127.0.0.1", 5566, 8)
    out = flat(capsys.readouterr().out)

    assert rc == 2  # an error without a non-zero child code still fails
    assert "connection refused" in out
    assert "Latency patch not verified" in out


def test_render_verify_reports_a_verifier_that_gave_no_verdict(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "Popen", _verifier(returncode=0))
    rc = ts._render_verify(sys.executable, "127.0.0.1", 5566, 8)

    assert rc == 2
    assert "without a verdict" in flat(capsys.readouterr().err)


def test_render_verify_passes_unexpected_child_output_through(monkeypatch, capsys):
    monkeypatch.setattr(
        subprocess,
        "Popen",
        _verifier("DeprecationWarning: protobuf", "", returncode=1),
    )
    ts._render_verify(sys.executable, "127.0.0.1", 5566, 8)

    assert "DeprecationWarning: protobuf" in flat(capsys.readouterr().out)


# ── testserver: millisecond formatting ───────────────────────────────────────


@pytest.mark.parametrize(
    "value, warn, expected",
    [
        (0.42, False, "[dim]0.42 ms[/dim]"),  # noise: dimmed
        (3.40, False, "3.40 ms"),  # normal
        (40.0, True, "[red]40.00 ms[/red]"),  # a stall on a split frame
        (40.0, False, "40.00 ms"),  # same figure, not a split frame
    ],
)
def test_ms_marks_noise_and_stalls(value, warn, expected):
    assert ts._ms(value, warn=warn) == expected


def test_summary_grid_flags_a_majority_of_split_frames():
    from rich.console import Console

    console = Console(width=80, no_color=False, force_terminal=False)
    with console.capture() as cap:
        console.print(
            ts._summary_grid(
                {
                    "split": 8,
                    "rounds": 8,
                    "median_gap_ms": 40.0,
                    "median_total_ms": 44.0,
                }
            )
        )

    assert "8 / 8" in cap.get()
    assert "median gap after header" in cap.get()


# ── testserver run ───────────────────────────────────────────────────────────


@pytest.fixture
def no_run_preflight(monkeypatch):
    """Docker, the clone and the port are all fine; only `run` itself is tested."""
    monkeypatch.setattr(ts.preflight, "check_server_sources", lambda *a, **k: None)
    monkeypatch.setattr(ts.preflight, "check_docker", lambda *a, **k: None)
    monkeypatch.setattr(ts.preflight, "check_port", lambda *a, **k: None)


def test_run_publishes_the_requested_host_port(runner, no_run_preflight, monkeypatch):
    seen = {}

    def _run(cmd, *a, **k):
        seen["cmd"], seen["port"] = list(cmd), k["env"]["PORT"]
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", _run)
    result = runner.invoke(ts_group, ["run", "-p", "6000"])

    assert result.exit_code == 0
    assert seen["cmd"] == ["bash", str(ts.RUN_SH)]
    assert seen["port"] == "6000"  # run.sh reads the port from the environment
    assert "host port 6000" in flat(result.output)


def test_run_defaults_to_port_5566(runner, no_run_preflight, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, *a, **k: seen.update(port=k["env"]["PORT"])
        or subprocess.CompletedProcess(cmd, 0),
    )
    runner.invoke(ts_group, ["run"])

    assert seen["port"] == "5566"


def test_run_reports_a_clean_stop_on_ctrl_c(runner, no_run_preflight, monkeypatch):
    """run.sh traps SIGINT and stops the container itself; the CLI just reports."""
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt)
    )
    result = runner.invoke(ts_group, ["run"])

    assert result.exit_code == 0
    assert "server stopped" in flat(result.output)


def test_run_explains_a_missing_bash(runner, no_run_preflight, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("bash")),
    )
    result = runner.invoke(ts_group, ["run"])
    out = flat(result.output)

    assert result.exit_code == 1
    assert "bash is not installed" in out
    assert "sudo apt install bash" in out


def test_run_passes_the_servers_exit_code_through(
    runner, no_run_preflight, monkeypatch
):
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, *a, **k: subprocess.CompletedProcess(cmd, 4)
    )
    assert runner.invoke(ts_group, ["run"]).exit_code == 4


def test_run_reports_a_missing_launcher(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "RUN_SH", tmp_path / "nope.sh")
    result = runner.invoke(ts_group, ["run"])

    assert result.exit_code == 1
    assert "Server launcher not found" in flat(result.output)


def test_has_protobuf_asks_the_interpreter_itself(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, *a, **k: seen.update(cmd=list(cmd))
        or subprocess.CompletedProcess(cmd, 0),
    )

    assert ts._has_protobuf(sys.executable) is True
    assert seen["cmd"] == [sys.executable, "-c", "import google.protobuf"]


def test_has_protobuf_is_false_when_the_import_fails(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, *a, **k: subprocess.CompletedProcess(cmd, 1)
    )
    assert ts._has_protobuf(sys.executable) is False
