#!/usr/bin/env python3

# Electronic Cats
# test_utils_output.py — the shared console helpers every command prints through
# (modules/utils/output.py) and the device selectors they all share
# (modules/utils/cli_options.py). Small surface, but the whole CLI's voice: the
# ✓/✗ prefixes, quiet mode, and the panels that carry a fix the user must act on.

import click
import pytest
from rich.console import Console

from conftest import flat
from modules.utils import output as out
from modules.utils.cli_options import target_options


@pytest.fixture(autouse=True)
def loud():
    """Quiet mode is process-global; never leak it into another test."""
    out.set_quiet_mode(False)
    yield
    out.set_quiet_mode(False)


def render(fn, *args, width: int = 80, **kwargs) -> str:
    """Run a print helper against a captured console and return the plain text.

    Patches both `out.console` and `out.console_err` to the same captured
    console: `print_error`/`print_warning` write to the latter (M23), and
    these tests only care about the rendered text, not which stream it went
    to (that split is covered at the CLI level via CliRunner's merged
    `result.output`).
    """
    console = Console(width=width, force_terminal=False)
    original, out.console = out.console, console
    original_err, out.console_err = out.console_err, console
    try:
        with console.capture() as cap:
            fn(*args, **kwargs)
    finally:
        out.console = original
        out.console_err = original_err
    return cap.get()


# ── one-line messages ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fn, mark",
    [
        (out.print_success, "✓"),
        (out.print_warning, "⚠"),
        (out.print_error, "✗"),
        (out.print_info, "ℹ"),
    ],
)
def test_status_messages_carry_their_marker(fn, mark):
    text = render(fn, "relay started")

    assert mark in text
    assert "relay started" in text


def test_dim_message_has_no_marker():
    assert flat(render(out.print_dim, "link ended")) == "link ended"


def test_step_shows_progress_out_of_a_total():
    assert "Step 2/5: connecting" in flat(render(out.print_step, 2, 5, "connecting"))


def test_example_pairs_a_command_with_its_description():
    text = flat(render(out.print_example, "bombercat run", "start the relay"))

    assert "bombercat run" in text and "start the relay" in text


def test_separator_has_the_requested_width():
    assert render(out.print_separator, "-", 20).strip() == "-" * 20


def test_raw_text_is_printed_untouched():
    assert flat(render(out.print_raw, "plain")) == "plain"


def test_detail_message_is_indented():
    assert render(out.print_detail_message, "detail", indent=4).startswith("    detail")


def test_fmt_command_marks_up_a_copy_pasteable_command():
    assert out.fmt_command("docker ps") == "[green]docker ps[/green]"


# ── quiet mode ───────────────────────────────────────────────────────────────


def test_quiet_mode_is_off_by_default():
    assert out.is_quiet_mode() is False


def test_quiet_mode_silences_the_test_helpers():
    out.set_quiet_mode(True)

    assert out.is_quiet_mode() is True
    assert render(out.print_test_header, "Fase 6") == ""
    assert render(out.print_test_step, "ping", "handshake") == ""
    assert render(out.print_test_pass, "+OK") == ""


def test_failures_are_printed_even_in_quiet_mode():
    out.set_quiet_mode(True)
    assert "FAIL" in render(out.print_test_fail, "-ERR")


def test_long_details_are_truncated():
    text = render(out.print_test_pass, "x" * 200, max_length=10)

    assert "x" * 10 + "..." in flat(text)
    assert "x" * 20 not in flat(text)


def test_summary_counts_are_reported():
    assert "7/8 protocol tests passed" in flat(
        render(out.print_test_summary, 7, 8, "protocol")
    )


# ── numbered steps ───────────────────────────────────────────────────────────


def test_numbered_steps_are_numbered_in_order():
    text = flat(
        render(lambda: out.console.print(out.numbered_steps(["do this", "then that"])))
    )

    assert "1. do this" in text and "2. then that" in text


def test_numbered_steps_keep_wrapped_lines_out_of_the_number_column():
    """A wrapped step used to read as a new one; continuations must be indented."""
    step = "a step whose text is long enough that it has to wrap across lines"
    lines = [
        line
        for line in render(
            lambda: out.console.print(out.numbered_steps([step])), width=40
        ).splitlines()
        if line.strip()
    ]

    assert lines[0].strip().startswith("1.")
    assert len(lines) > 1
    assert not lines[1].strip().startswith(("1.", "2."))


# ── panels ───────────────────────────────────────────────────────────────────


def test_error_panel_carries_problem_why_fix_and_notes():
    text = flat(
        render(
            out.print_error_panel,
            "Cannot start the test server",
            "Docker is not installed.",
            why="The test server runs in a container.",
            fix=[out.fmt_command("sudo apt install docker.io")],
            notes=["You can also run it without Docker."],
        )
    )

    assert "Cannot start the test server" in text
    assert "✗ Docker is not installed." in text
    assert "runs in a container" in text
    assert "How to fix it" in text and "1. sudo apt install docker.io" in text
    assert "without Docker" in text


def test_error_panel_can_retitle_the_fix_section():
    assert "What to check" in flat(
        render(
            out.print_error_panel,
            "Latency patch not verified",
            "connection refused",
            fix=["is the server running?"],
            fix_title="What to check",
        )
    )


def test_error_panel_drops_empty_notes():
    text = render(
        out.print_error_panel, "Title", "Problem", notes=["", None, "a real note"]
    )

    assert "a real note" in flat(text)


def test_success_panel_mirrors_the_error_panel():
    text = flat(
        render(
            out.print_success_panel,
            "Latency patch · 127.0.0.1:5566",
            "Latency patch is active",
            why="Every frame arrived whole.",
            notes=["Measured over 8 rounds."],
        )
    )

    assert "✓ Latency patch is active" in text
    assert "Every frame arrived whole." in text
    assert "Measured over 8 rounds." in text


# ── shared device selectors ──────────────────────────────────────────────────


@click.command()
@target_options
def _targeted(port, device_id):
    click.echo(f"port={port} device_id={device_id}")


def test_target_options_default_to_auto_detection(runner):
    assert "port=None device_id=None" in runner.invoke(_targeted, []).output


@pytest.mark.parametrize("flag", ["-p", "--port"])
def test_port_is_taken_as_a_raw_path(runner, flag):
    result = runner.invoke(_targeted, [flag, "/dev/ttyACM0"])
    assert "port=/dev/ttyACM0" in result.output


@pytest.mark.parametrize("flag", ["-d", "--device"])
def test_device_is_taken_as_an_integer_id(runner, flag):
    result = runner.invoke(_targeted, [flag, "2"])
    assert "device_id=2" in result.output


def test_device_id_must_be_a_number(runner):
    assert runner.invoke(_targeted, ["-d", "first"]).exit_code == 2


def test_both_selectors_are_accepted_by_click_and_resolved_later(runner):
    """`resolve_port` is what rejects the combination, with a clear message."""
    result = runner.invoke(_targeted, ["-p", "/dev/ttyACM0", "-d", "1"])
    assert result.exit_code == 0
