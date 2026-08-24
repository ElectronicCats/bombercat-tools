#!/usr/bin/env python3

# Electronic Cats
# test_cli_tags.py — `bombercat tags read|watch` (modules/tags/cli.py).
# Same pattern as test_cli_relay.py: CliRunner + FakeLink, driving each
# command's real logic against a scripted `stream()`.
# docs/CLI_IMPROVEMENTS_DetectTags.md §3.1-3.2, §7 (Phase 2).

import json

import pytest

from conftest import FakeLink, flat
from modules.tags import cli as tagscli
from modules.tags.cli import read_cmd, tags, watch_cmd

STRUCTURED_LINE = ":tag 1234 NFC-A T2T 041A2B3C"

LEGACY_NFC_A_LINES = [
    "Remote activated tag type: 2",
    "\tTechnology: NFC-A",
    "\tNFC ID = 0x04 0x1a 0x2b",
]


# ── read ─────────────────────────────────────────────────────────────────────


def test_read_reports_a_structured_detection(runner, use_link):
    use_link(tagscli, FakeLink(stream_lines=[STRUCTURED_LINE]))
    result = runner.invoke(read_cmd, [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "Tag detected" in out
    assert "04:1A:2B:3C" in out
    assert "NFC-A" in out and "T2T" in out


def test_read_reports_a_legacy_detection(runner, use_link):
    use_link(tagscli, FakeLink(stream_lines=LEGACY_NFC_A_LINES))
    result = runner.invoke(read_cmd, [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "04:1A:2B" in out
    assert "NFC-A" in out


def test_read_times_out_with_no_tag(runner, use_link):
    use_link(tagscli, FakeLink(stream_lines=[]))
    result = runner.invoke(read_cmd, ["-t", "0.01"])

    assert result.exit_code == 1
    assert "no tag detected" in flat(result.stdout)


def test_read_json_emits_one_clean_object_on_stdout(runner, use_link):
    use_link(tagscli, FakeLink(stream_lines=[STRUCTURED_LINE]))
    result = runner.invoke(read_cmd, ["--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["uid"] == "041A2B3C"
    assert payload["tech"] == "NFC-A"
    assert payload["protocol"] == "T2T"
    assert payload["ts_ms"] == 1234


def test_read_reports_a_board_that_will_not_handshake(runner, use_link):
    link = use_link(tagscli, FakeLink(ping_ok=False))
    result = runner.invoke(read_cmd, [])

    assert result.exit_code == 1
    assert "did not answer the handshake" in flat(result.stdout)
    assert link.closed


def test_read_verbose_traces_to_stderr_and_keeps_stdout_clean_for_json(
    runner, use_link
):
    use_link(tagscli, FakeLink(stream_lines=[STRUCTURED_LINE]))
    result = runner.invoke(read_cmd, ["--json", "-v"])

    assert result.exit_code == 0
    json.loads(result.stdout)  # still valid JSON: -v never leaked into stdout
    assert f"< {STRUCTURED_LINE}" in result.stderr


def test_root_verbose_before_the_verb_also_traces_tags_read(
    runner, use_link, monkeypatch
):
    """`bombercat -v tags read` must trace just like `bombercat tags read -v`
    (docs/CLI_IMPROVEMENTS_DetectTags.md §4.2) — the root `-v` reaches this
    command's `ctx.obj["verbose"]` set by `modules.core.cli.cli()`."""
    import sys

    from modules.core import cli as root_cli

    use_link(tagscli, FakeLink(stream_lines=[STRUCTURED_LINE]))
    monkeypatch.setattr(sys, "argv", ["bombercat", "--help"])
    with pytest.raises(SystemExit):
        root_cli.main_cli()  # registers `tags` under the root `cli` group

    result = runner.invoke(root_cli.cli, ["-v", "tags", "read", "--json"])

    assert result.exit_code == 0
    json.loads(result.stdout)  # -v still didn't leak into stdout
    assert f"< {STRUCTURED_LINE}" in result.stderr


# ── watch ────────────────────────────────────────────────────────────────────


def test_watch_prints_each_detection_and_a_ctrl_c_summary(runner, use_link):
    def _lines():
        yield STRUCTURED_LINE
        yield ":tag 2000 NFC-A MIFARE A3912200"
        raise KeyboardInterrupt

    use_link(tagscli, FakeLink(stream_lines=_lines()))
    result = runner.invoke(watch_cmd, [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "04:1A:2B:3C" in out and "A3:91:22:00" in out
    assert "2 detections, 2 unique UIDs" in out


def test_watch_dedupe_collapses_repeats(runner, use_link):
    def _lines():
        yield STRUCTURED_LINE
        yield STRUCTURED_LINE
        yield STRUCTURED_LINE
        raise KeyboardInterrupt

    use_link(tagscli, FakeLink(stream_lines=_lines()))
    result = runner.invoke(watch_cmd, ["--dedupe"])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert out.count("04:1A:2B:3C") >= 1
    assert "seen again" in out
    assert "3 detections, 1 unique UIDs" in out


def test_watch_hides_noise_by_default(runner, use_link):
    def _lines():
        yield "Restarting..."
        yield "Waiting for a Card..."
        raise KeyboardInterrupt

    use_link(tagscli, FakeLink(stream_lines=_lines()))
    result = runner.invoke(watch_cmd, [])

    assert "Restarting" not in result.stdout


def test_watch_shows_noise_with_no_quiet_noise(runner, use_link):
    def _lines():
        yield "Restarting..."
        raise KeyboardInterrupt

    use_link(tagscli, FakeLink(stream_lines=_lines()))
    result = runner.invoke(watch_cmd, ["--no-quiet-noise"])

    assert "Restarting..." in result.stdout


def test_watch_json_emits_newline_delimited_objects(runner, use_link):
    def _lines():
        yield STRUCTURED_LINE
        raise KeyboardInterrupt

    use_link(tagscli, FakeLink(stream_lines=_lines()))
    result = runner.invoke(watch_cmd, ["--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout.strip().splitlines()[0])
    assert payload["uid"] == "041A2B3C"


# ── group wiring ─────────────────────────────────────────────────────────────


def test_tags_group_exposes_both_subcommands():
    assert set(tags.commands) == {"read", "watch"}
