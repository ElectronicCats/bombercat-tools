#!/usr/bin/env python3

# Electronic Cats
# test_verbose.py — the `-v`/`-vv` trace plumbing: `make_tracer`
# (modules/utils/output.py), the `_tx`/`_rx` hooks it feeds on `DeviceLink`
# (modules/core/bombercat.py), the shared `device_options` decorator
# (modules/utils/cli_options.py), and the root `cli()` group storing the flag
# in `ctx.obj["verbose"]` (modules/core/cli.py). No command wires a tracer in
# yet — Phase 2 (`tags read`/`watch`) is the first consumer — so this file
# only proves the foundation works in isolation.

import click
import pytest

from conftest import FakeSerial
from modules.core import bombercat as core
from modules.core import cli as root
from modules.core.bombercat import DeviceLink
from modules.core.cli import cli
from modules.utils import output as out
from modules.utils.cli_options import device_options, target_options


# ── make_tracer ──────────────────────────────────────────────────────────────


def test_level_zero_returns_no_tracer():
    assert out.make_tracer(0) is None


def test_level_one_prints_arrows_with_tx_rx_styling(capsys):
    trace = out.make_tracer(1)
    trace("tx", "ping")
    trace("rx", "+OK bombercat")

    err = capsys.readouterr().err
    assert "> ping" in err
    assert "< +OK bombercat" in err


def test_level_two_adds_elapsed_time_and_byte_count(capsys):
    trace = out.make_tracer(2, t0=0.0)
    trace("tx", "ping")

    err = capsys.readouterr().err
    assert "> " in err
    assert "s]" in err  # elapsed-time stamp
    assert "B)" in err  # byte count
    assert "ping" in err


def test_level_one_has_no_timestamp_or_byte_count(capsys):
    trace = out.make_tracer(1, t0=0.0)
    trace("tx", "ping")

    err = capsys.readouterr().err
    assert "s]" not in err
    assert "B)" not in err


@pytest.mark.parametrize(
    "line",
    [
        "set pass hunter2",
        "SET PASS hunter2",
    ],
)
def test_set_pass_lines_are_redacted(capsys, line):
    trace = out.make_tracer(1)
    trace("tx", line)

    err = capsys.readouterr().err
    assert "hunter2" not in err
    assert "(redacted)" in err


def test_redaction_preserves_trailing_args(capsys):
    trace = out.make_tracer(1)
    trace("tx", "set pass hunter2 extra")

    err = capsys.readouterr().err
    assert "hunter2" not in err
    assert "extra" in err  # only the secret token is dropped


def test_non_secret_set_commands_are_not_redacted(capsys):
    trace = out.make_tracer(1)
    trace("tx", "set ssid home-network")

    err = capsys.readouterr().err
    assert "home-network" in err
    assert "(redacted)" not in err


# ── DeviceLink tracing ───────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def no_settle_sleep(monkeypatch):
    monkeypatch.setattr(core.time, "sleep", lambda _s: None)


@pytest.fixture
def linked(monkeypatch):
    """Open a DeviceLink wired to a tracer, backed by a scripted FakeSerial."""

    def _open(script=None, trace=None, timeout=0.2):
        ser = FakeSerial(script)
        monkeypatch.setattr(core, "open_serial", lambda *a, **k: ser)
        return DeviceLink("/dev/fake0", timeout=timeout, trace=trace).open(), ser

    return _open


def test_command_traces_the_outgoing_line(linked):
    calls = []
    link, ser = linked(
        {"ping": ["+OK bombercat"]}, trace=lambda d, t: calls.append((d, t))
    )

    link.command("ping")

    assert ("tx", "ping") in calls


def test_command_traces_every_incoming_line(linked):
    calls = []
    link, ser = linked(
        {"info": [":name detecttags", "+OK"]},
        trace=lambda d, t: calls.append((d, t)),
    )

    link.command("info")

    assert ("rx", ":name detecttags") in calls
    assert ("rx", "+OK") in calls


def test_no_trace_by_default(linked):
    """A DeviceLink built without `trace=` must not touch stderr at all."""
    link, ser = linked({"ping": ["+OK bombercat"]})

    link.command("ping")  # would raise if _tx/_rx assumed a tracer exists


def test_read_lines_traces_each_line(linked):
    calls = []
    link, ser = linked(trace=lambda d, t: calls.append((d, t)))
    ser.feed("Waiting for a Card...", "Card removed!")

    link.read_lines(duration=0.05)

    assert ("rx", "Waiting for a Card...") in calls
    assert ("rx", "Card removed!") in calls


# ── device_options decorator ─────────────────────────────────────────────────


def test_device_options_adds_verbose_alongside_target_options(runner):
    @click.command()
    @device_options
    def cmd(port, device_id, verbose):
        click.echo(f"port={port} device_id={device_id} verbose={verbose}")

    result = runner.invoke(cmd, ["-p", "/dev/ttyACM0", "-d", "3", "-vv"])

    assert result.exit_code == 0
    assert "port=/dev/ttyACM0" in result.output
    assert "device_id=3" in result.output
    assert "verbose=2" in result.output


def test_device_options_verbose_defaults_to_zero(runner):
    @click.command()
    @device_options
    def cmd(port, device_id, verbose):
        click.echo(f"verbose={verbose}")

    result = runner.invoke(cmd, [])

    assert result.exit_code == 0
    assert "verbose=0" in result.output


def test_device_options_is_target_options_plus_verbose(runner):
    """The decorator must not change `-p`/`-d` behavior, only add `-v` to it."""

    @click.command()
    @target_options
    def plain(port, device_id):
        click.echo(f"{port}/{device_id}")

    @click.command()
    @device_options
    def extended(port, device_id, verbose):
        click.echo(f"{port}/{device_id}")

    args = ["-p", "/dev/ttyACM0", "-d", "1"]
    assert runner.invoke(plain, args).output == runner.invoke(extended, args).output


# ── root cli() verbosity ─────────────────────────────────────────────────────


def test_root_verbose_count_lands_in_ctx_obj(runner):
    seen = {}

    @click.command()
    @click.pass_context
    def probe(ctx):
        seen["verbose"] = ctx.obj["verbose"]

    root.cli.add_command(probe)
    try:
        result = runner.invoke(cli, ["-vv", "probe"])
        assert result.exit_code == 0
        assert seen["verbose"] == 2
    finally:
        del root.cli.commands["probe"]


def test_root_verbose_defaults_to_zero_in_ctx_obj(runner):
    seen = {}

    @click.command()
    @click.pass_context
    def probe(ctx):
        seen["verbose"] = ctx.obj["verbose"]

    root.cli.add_command(probe)
    try:
        result = runner.invoke(cli, ["probe"])
        assert result.exit_code == 0
        assert seen["verbose"] == 0
    finally:
        del root.cli.commands["probe"]
