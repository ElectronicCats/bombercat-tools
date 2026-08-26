#!/usr/bin/env python3

# Electronic Cats
# test_cli_magspoof.py — `bombercat magspoof play|show|watch|info|card`
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
    show_cmd,
    watch_cmd,
)

TRACK1 = "%B4111111111111111^TEST/CARD^261200000000000?"
TRACK2 = ";4111111111111111=26120000000000?"


# ── play ─────────────────────────────────────────────────────────────────────


def test_play_sends_bare_magplay_for_the_active_card(runner, use_link):
    fake = use_link(magspoofcli, FakeLink(responses={"magplay": ok("played 1")}))
    result = runner.invoke(play_cmd, [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    # Bare `magplay` lets the firmware pick the track(s) the active card holds —
    # a full swipe for a two-track card, the lone track for a single-track one.
    assert fake.sent == ["magplay"]
    assert "played track 1" in out


def test_play_reports_the_played_track_for_a_single_track_card(runner, use_link):
    use_link(magspoofcli, FakeLink(responses={"magplay": ok("played 2")}))
    result = runner.invoke(play_cmd, [])

    assert result.exit_code == 0
    assert "played track 2" in flat(result.stdout)


def test_play_takes_no_track_argument(runner, use_link):
    use_link(magspoofcli, FakeLink(responses={"magplay 1": ok("played 1")}))
    for track in ("1", "2", "3"):
        assert runner.invoke(play_cmd, [track]).exit_code != 0


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


# ── show ─────────────────────────────────────────────────────────────────────


def test_show_prints_both_tracks_and_the_button_mode(runner, use_link):
    use_link(
        magspoofcli,
        FakeLink(responses={"magget": ok("", t1=TRACK1, t2=TRACK2, btn="2")}),
    )
    result = runner.invoke(show_cmd, [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert TRACK1 in out
    assert TRACK2 in out
    assert "track 2" in out.split("button")[-1]


def test_show_leaves_the_button_row_blank_on_firmware_without_magbtn(runner, use_link):
    use_link(
        magspoofcli,
        FakeLink(responses={"magget": ok("", t1=TRACK1, t2=TRACK2)}),
    )
    result = runner.invoke(show_cmd, [])

    assert result.exit_code == 0
    assert "—" in flat(result.stdout).split("button")[-1]


def test_show_json_emits_a_clean_object(runner, use_link):
    use_link(
        magspoofcli,
        FakeLink(responses={"magget": ok("", t1=TRACK1, t2=TRACK2, btn="alt")}),
    )
    result = runner.invoke(show_cmd, ["--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {"t1": TRACK1, "t2": TRACK2, "btn": "alt"}


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
    assert set(magspoof.commands) == {
        "play",
        "show",
        "watch",
        "info",
        "card",
    }


def test_card_group_exposes_all_subcommands():
    assert set(magspoof.commands["card"].commands) == {
        "list",
        "add",
        "del",
        "set",
        "select",
        "get",
        "info",
    }


# ── card ─────────────────────────────────────────────────────────────────────


def _card_cmd(name):
    """Reach a `card` subcommand by name for CliRunner.invoke."""
    return magspoof.commands["card"].commands[name]


NAME2 = "AMEX"
TRACK1B = "%B378282246310005^TEST/AMEX^261200000000000?"
TRACK2B = ";378282246310005=26120000000000?"


def test_card_list_renders_cards_and_marks_the_active_one(runner, use_link):
    use_link(
        magspoofcli,
        FakeLink(
            responses={
                "magcard list": ok(
                    "2 cards",
                    count="2",
                    active="BBVA",
                    card0=f"BBVA\t{TRACK1}\t{TRACK2}",
                    card1=f"{NAME2}\t{TRACK1B}\t{TRACK2B}",
                )
            }
        ),
    )
    result = runner.invoke(_card_cmd("list"), [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "BBVA" in out and NAME2 in out
    # The table truncates long tracks to fit the terminal (full data is in
    # --json); a distinctive prefix of each still survives.
    assert "%B41111111" in out and "%B37828224" in out
    # The active card carries a marker the inactive one does not.
    assert "●" in result.stdout


def test_card_list_json_emits_one_object_per_card(runner, use_link):
    use_link(
        magspoofcli,
        FakeLink(
            responses={
                "magcard list": ok(
                    "",
                    count="2",
                    active="BBVA",
                    card0=f"BBVA\t{TRACK1}\t{TRACK2}",
                    card1=f"{NAME2}\t{TRACK1B}\t{TRACK2B}",
                )
            }
        ),
    )
    result = runner.invoke(_card_cmd("list"), ["--json"])
    rows = [json.loads(ln) for ln in result.stdout.strip().splitlines()]

    assert result.exit_code == 0
    assert rows[0] == {"name": "BBVA", "t1": TRACK1, "t2": TRACK2, "active": True}
    assert rows[1]["active"] is False


def test_card_list_reports_an_empty_store(runner, use_link):
    use_link(
        magspoofcli,
        FakeLink(responses={"magcard list": ok("0 cards", count="0", active="")}),
    )
    result = runner.invoke(_card_cmd("list"), [])

    assert result.exit_code == 0
    assert "no cards" in flat(result.stdout)


def test_card_add_validates_the_name_locally_without_a_round_trip(runner, use_link):
    fake = use_link(magspoofcli, FakeLink())
    result = runner.invoke(
        _card_cmd("add"), ["bad name", "--t1", TRACK1, "--t2", TRACK2]
    )

    assert result.exit_code == 1
    assert "card name cannot contain spaces" in flat(result.output)
    assert fake.sent == []


def test_card_add_validates_tracks_locally(runner, use_link):
    fake = use_link(magspoofcli, FakeLink())
    result = runner.invoke(_card_cmd("add"), ["BBVA", "--t1", "garbage"])

    assert result.exit_code == 1
    assert "bad track 1 format" in flat(result.output)
    assert fake.sent == []


def test_card_add_requires_at_least_one_track(runner, use_link):
    fake = use_link(magspoofcli, FakeLink())
    result = runner.invoke(_card_cmd("add"), ["BBVA"])

    assert result.exit_code == 1
    assert "at least one track" in flat(result.output)
    assert fake.sent == []


def test_card_add_creates_the_card_then_writes_both_tracks(runner, use_link):
    fake = use_link(magspoofcli, FakeLink(responses={"magcard": ok("")}))
    result = runner.invoke(_card_cmd("add"), ["BBVA", "--t1", TRACK1, "--t2", TRACK2])

    assert result.exit_code == 0
    assert fake.sent == [
        "magcard add BBVA",
        f"magcard set BBVA 1 {TRACK1}",
        f"magcard set BBVA 2 {TRACK2}",
    ]
    assert "added 2-track card BBVA" in flat(result.stdout)


def test_card_add_stores_a_single_track_1_membership_card(runner, use_link):
    fake = use_link(magspoofcli, FakeLink(responses={"magcard": ok("")}))
    result = runner.invoke(_card_cmd("add"), ["CINEMA", "--t1", TRACK1])

    assert result.exit_code == 0
    # Only track 1 is written — no empty track 2 command.
    assert fake.sent == ["magcard add CINEMA", f"magcard set CINEMA 1 {TRACK1}"]
    assert "added 1-track card CINEMA" in flat(result.stdout)


def test_card_add_stores_a_single_track_2_card(runner, use_link):
    fake = use_link(magspoofcli, FakeLink(responses={"magcard": ok("")}))
    result = runner.invoke(_card_cmd("add"), ["LOYAL", "--t2", TRACK2])

    assert result.exit_code == 0
    assert fake.sent == ["magcard add LOYAL", f"magcard set LOYAL 2 {TRACK2}"]
    assert "added 1-track card LOYAL" in flat(result.stdout)


def test_card_add_rolls_back_the_empty_card_when_a_track_write_fails(runner, use_link):
    fake = use_link(
        magspoofcli,
        FakeLink(
            responses={
                "magcard add BBVA": ok(""),
                f"magcard set BBVA 1 {TRACK1}": err("flash error"),
                "magcard": ok(""),  # del rollback (matched by first word)
            }
        ),
    )
    result = runner.invoke(_card_cmd("add"), ["BBVA", "--t1", TRACK1, "--t2", TRACK2])

    assert result.exit_code == 1
    assert "magcard del BBVA" in fake.sent  # the empty card was cleaned up
    assert "card add failed" in flat(result.output)


def test_card_del_sends_the_delete(runner, use_link):
    fake = use_link(magspoofcli, FakeLink(responses={"magcard del BBVA": ok("")}))
    result = runner.invoke(_card_cmd("del"), ["BBVA"])

    assert result.exit_code == 0
    assert fake.sent == ["magcard del BBVA"]
    assert "deleted card BBVA" in flat(result.stdout)


def test_card_del_reports_a_missing_card(runner, use_link):
    use_link(magspoofcli, FakeLink(responses={"magcard del NOPE": err("not found")}))
    result = runner.invoke(_card_cmd("del"), ["NOPE"])

    assert result.exit_code == 1
    assert "card del failed" in flat(result.output)


def test_card_set_requires_at_least_one_track(runner, use_link):
    fake = use_link(magspoofcli, FakeLink())
    result = runner.invoke(_card_cmd("set"), ["BBVA"])

    assert result.exit_code == 1
    assert "nothing to set" in flat(result.output)
    assert fake.sent == []


def test_card_set_updates_only_the_given_tracks(runner, use_link):
    fake = use_link(magspoofcli, FakeLink(responses={"magcard": ok("")}))
    result = runner.invoke(_card_cmd("set"), ["BBVA", "--t2", TRACK2])

    assert result.exit_code == 0
    assert fake.sent == [f"magcard set BBVA 2 {TRACK2}"]
    assert "track 2" in flat(result.stdout)


def test_card_set_validates_before_sending(runner, use_link):
    fake = use_link(magspoofcli, FakeLink())
    result = runner.invoke(_card_cmd("set"), ["BBVA", "--t1", "nope"])

    assert result.exit_code == 1
    assert "bad track 1 format" in flat(result.output)
    assert fake.sent == []


def test_card_select_sends_select_and_reports_active(runner, use_link):
    fake = use_link(
        magspoofcli,
        FakeLink(responses={"magcard select BBVA": ok("", active="BBVA")}),
    )
    result = runner.invoke(_card_cmd("select"), ["BBVA"])

    assert result.exit_code == 0
    assert fake.sent == ["magcard select BBVA"]
    assert "active card is now BBVA" in flat(result.stdout)


def test_card_get_active_sends_bare_get(runner, use_link):
    fake = use_link(
        magspoofcli,
        FakeLink(
            responses={
                "magcard get": ok("", name="BBVA", t1=TRACK1, t2=TRACK2, active="1")
            }
        ),
    )
    result = runner.invoke(_card_cmd("get"), [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert fake.sent == ["magcard get"]
    assert "BBVA" in out and TRACK1 in out
    assert "yes" in out  # active


def test_card_get_by_name_passes_the_name(runner, use_link):
    fake = use_link(
        magspoofcli,
        FakeLink(
            responses={
                f"magcard get {NAME2}": ok(
                    "", name=NAME2, t1=TRACK1B, t2=TRACK2B, active="0"
                )
            }
        ),
    )
    result = runner.invoke(_card_cmd("get"), [NAME2])

    assert result.exit_code == 0
    assert fake.sent == [f"magcard get {NAME2}"]
    assert "no" in flat(result.stdout).split("active")[-1]


def test_card_get_json(runner, use_link):
    use_link(
        magspoofcli,
        FakeLink(
            responses={
                "magcard get": ok("", name="BBVA", t1=TRACK1, t2=TRACK2, active="1")
            }
        ),
    )
    result = runner.invoke(_card_cmd("get"), ["--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "name": "BBVA",
        "t1": TRACK1,
        "t2": TRACK2,
        "active": True,
    }


def test_card_info_shows_count_and_capacity(runner, use_link):
    use_link(
        magspoofcli,
        FakeLink(
            responses={
                "magcard info": ok("", count="3", capacity="50", active="BBVA", btn="1")
            }
        ),
    )
    result = runner.invoke(_card_cmd("info"), [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "3 / 50" in out
    assert "BBVA" in out


def test_card_hints_reflash_on_unknown_command(runner, use_link):
    use_link(magspoofcli, FakeLink(responses={"magcard": err("unknown command")}))
    result = runner.invoke(_card_cmd("list"), [])

    assert result.exit_code == 1
    assert "bombercat flash magspoof" in flat(result.output)
