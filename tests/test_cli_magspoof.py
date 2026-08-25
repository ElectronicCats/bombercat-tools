#!/usr/bin/env python3

# Electronic Cats
# test_cli_magspoof.py — `bombercat magspoof play|set|show|watch|info`
# (modules/magspoof/cli.py). Same pattern as test_cli_readers.py: CliRunner +
# FakeLink, driving each command's real logic against scripted `command()`
# responses and a scripted `stream()`.
# docs/IMPLEMENTATION_PLAN_MagSpoof_CLI.md §5 phase 3.

import json

import pytest

from conftest import FakeLink, err, flat, ok
from modules.magspoof import cli as magspoofcli
from modules.magspoof.cli import (
    info_cmd,
    magspoof,
    play_cmd,
    set_cmd,
    show_cmd,
    watch_cmd,
)

TRACK1 = "%B4111111111111111^TEST/CARD^261200000000000?"
TRACK2 = ";4111111111111111=26120000000000?"


# ── play ─────────────────────────────────────────────────────────────────────


def test_play_with_no_track_reports_alternated_track(runner, use_link):
    use_link(magspoofcli, FakeLink(responses={"magplay": ok("played 2")}))
    result = runner.invoke(play_cmd, [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "played track 2" in out


def test_play_with_explicit_track_sends_it(runner, use_link):
    fake = use_link(magspoofcli, FakeLink(responses={"magplay 1": ok("played 1")}))
    result = runner.invoke(play_cmd, ["1"])

    assert result.exit_code == 0
    assert "magplay 1" in fake.sent


def test_play_rejects_a_track_outside_1_or_2():
    from click.testing import CliRunner

    result = CliRunner().invoke(play_cmd, ["3"])
    assert result.exit_code != 0


def test_play_reports_a_firmware_error(runner, use_link):
    use_link(magspoofcli, FakeLink(responses={"magplay": err("bad track")}))
    result = runner.invoke(play_cmd, [])

    assert result.exit_code == 1
    assert "play failed" in flat(result.output)


def test_play_hints_reflash_on_unknown_command(runner, use_link):
    use_link(magspoofcli, FakeLink(responses={"magplay": err("unknown command")}))
    result = runner.invoke(play_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 1
    assert "bombercat flash magspoof" in out


def test_play_reports_a_board_that_will_not_handshake(runner, use_link):
    link = use_link(magspoofcli, FakeLink(ping_ok=False))
    result = runner.invoke(play_cmd, [])

    assert result.exit_code == 1
    assert "did not answer the handshake" in flat(result.output)
    assert link.closed


# ── set ──────────────────────────────────────────────────────────────────────


def test_set_sends_valid_track_1_and_reports_success(runner, use_link):
    fake = use_link(
        magspoofcli,
        FakeLink(responses={"magset": ok("track 1 set (44 chars)")}),
    )
    result = runner.invoke(set_cmd, ["1", TRACK1])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "track 1 set (44 chars)" in out
    assert fake.sent == [f"magset 1 {TRACK1}"]


def test_set_sends_valid_track_2(runner, use_link):
    fake = use_link(
        magspoofcli,
        FakeLink(responses={"magset": ok("track 2 set (34 chars)")}),
    )
    result = runner.invoke(set_cmd, ["2", TRACK2])

    assert result.exit_code == 0
    assert fake.sent == [f"magset 2 {TRACK2}"]


def test_set_rejects_bad_sentinel_locally_without_a_serial_round_trip(runner, use_link):
    fake = use_link(magspoofcli, FakeLink())
    result = runner.invoke(set_cmd, ["1", "basura"])

    assert result.exit_code == 1
    assert "bad track 1 format" in flat(result.output)
    assert fake.sent == []  # never left the CLI


def test_set_rejects_wrong_sentinel_for_track_2(runner, use_link):
    use_link(magspoofcli, FakeLink())
    result = runner.invoke(set_cmd, ["2", "%Bshouldbesemicolon?"])

    assert result.exit_code == 1
    assert "bad track 2 format" in flat(result.output)


def test_set_rejects_data_over_126_chars(runner, use_link):
    use_link(magspoofcli, FakeLink())
    data = "%" + "1" * 125 + "?"  # 127 chars
    result = runner.invoke(set_cmd, ["1", data])

    assert result.exit_code == 1
    assert "too long" in flat(result.output)


def test_set_rejects_data_containing_a_newline(runner, use_link):
    fake = use_link(magspoofcli, FakeLink())
    result = runner.invoke(set_cmd, ["1", "%B1234?\n?"])

    assert result.exit_code == 1
    assert "newline" in flat(result.output)
    assert fake.sent == []


def test_set_reports_a_firmware_error(runner, use_link):
    use_link(magspoofcli, FakeLink(responses={"magset": err("bad track")}))
    result = runner.invoke(set_cmd, ["1", TRACK1])

    assert result.exit_code == 1
    assert "set failed" in flat(result.output)


def test_set_hints_reflash_on_unknown_command(runner, use_link):
    use_link(magspoofcli, FakeLink(responses={"magset": err("unknown command")}))
    result = runner.invoke(set_cmd, ["1", TRACK1])

    assert result.exit_code == 1
    assert "bombercat flash magspoof" in flat(result.output)


# ── show ─────────────────────────────────────────────────────────────────────


def test_show_prints_both_tracks(runner, use_link):
    use_link(
        magspoofcli,
        FakeLink(responses={"magget": ok("", t1=TRACK1, t2=TRACK2)}),
    )
    result = runner.invoke(show_cmd, [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert TRACK1 in out
    assert TRACK2 in out


def test_show_json_emits_a_clean_object(runner, use_link):
    use_link(
        magspoofcli,
        FakeLink(responses={"magget": ok("", t1=TRACK1, t2=TRACK2)}),
    )
    result = runner.invoke(show_cmd, ["--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {"t1": TRACK1, "t2": TRACK2}


def test_show_hints_reflash_on_unknown_command(runner, use_link):
    use_link(magspoofcli, FakeLink(responses={"magget": err("unknown command")}))
    result = runner.invoke(show_cmd, [])

    assert result.exit_code == 1
    assert "bombercat flash magspoof" in flat(result.output)


# ── watch ────────────────────────────────────────────────────────────────────


def test_watch_prints_each_play_and_a_ctrl_c_summary(runner, use_link):
    def _lines():
        yield ":mag 1000 1"
        yield ":mag 2000 2"
        yield ":mag 3000 1"
        raise KeyboardInterrupt

    use_link(magspoofcli, FakeLink(stream_lines=_lines()))
    result = runner.invoke(watch_cmd, [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "track 1" in out and "track 2" in out
    assert "3 plays, track 1 ×2, track 2 ×1" in out


def test_watch_hides_noise_by_default(runner, use_link):
    def _lines():
        yield "Activating MagSpoof..."
        yield "Default tracks:"
        yield "Track 1: " + TRACK1
        raise KeyboardInterrupt

    use_link(magspoofcli, FakeLink(stream_lines=_lines()))
    result = runner.invoke(watch_cmd, [])

    assert "Activating MagSpoof" not in result.stdout


def test_watch_shows_noise_with_no_quiet_noise(runner, use_link):
    def _lines():
        yield "Press the MagSpoof button"
        raise KeyboardInterrupt

    use_link(magspoofcli, FakeLink(stream_lines=_lines()))
    result = runner.invoke(watch_cmd, ["--no-quiet-noise"])

    assert "Press the MagSpoof button" in result.stdout


def test_watch_json_emits_newline_delimited_objects(runner, use_link):
    def _lines():
        yield ":mag 1000 1"
        raise KeyboardInterrupt

    use_link(magspoofcli, FakeLink(stream_lines=_lines()))
    result = runner.invoke(watch_cmd, ["--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout.strip().splitlines()[0])
    assert payload == {"ts_ms": 1000, "track": 1}


def test_watch_reports_a_board_that_will_not_handshake(runner, use_link):
    link = use_link(magspoofcli, FakeLink(ping_ok=False))
    result = runner.invoke(watch_cmd, [])

    assert result.exit_code == 1
    assert "did not answer the handshake" in flat(result.output)
    assert link.closed


# ── info ─────────────────────────────────────────────────────────────────────


def test_info_reports_structured_events_seen(runner, use_link):
    use_link(
        magspoofcli,
        FakeLink(
            responses={"info": ok(fw="1.1.1.0", state="idle")},
            stream_lines=[":mag 10 1"],
        ),
    )
    result = runner.invoke(info_cmd, [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "1.1.1.0" in out
    assert "structured" in out
    assert "idle" in out


def test_info_reports_no_events_seen_yet(runner, use_link):
    use_link(
        magspoofcli,
        FakeLink(responses={"info": ok(fw="1.1.1.0", state="idle")}, stream_lines=[]),
    )
    result = runner.invoke(info_cmd, [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "no ':mag' events seen yet" in out


def test_info_reports_a_device_error_from_the_info_command(runner, use_link):
    use_link(magspoofcli, FakeLink(responses={"info": err("unknown command")}))
    result = runner.invoke(info_cmd, [])

    assert result.exit_code == 1
    assert "info failed" in flat(result.output)


def test_info_reports_a_board_that_will_not_handshake(runner, use_link):
    link = use_link(magspoofcli, FakeLink(ping_ok=False))
    result = runner.invoke(info_cmd, [])

    assert result.exit_code == 1
    assert "did not answer the handshake" in flat(result.output)
    assert link.closed


# ── group wiring ─────────────────────────────────────────────────────────────


def test_magspoof_group_exposes_all_subcommands():
    assert set(magspoof.commands) == {"play", "set", "show", "watch", "info"}
