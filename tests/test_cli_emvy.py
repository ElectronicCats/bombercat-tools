#!/usr/bin/env python3

# Electronic Cats
# test_cli_emvy.py — `bombercat emvy info` and the identity gate shared by the
# group (modules/emvy/cli.py). Same pattern as test_cli_magspoof.py: CliRunner +
# FakeLink, driving the command's real logic against a scripted `info` reply.
# The gate is by firmware identity (fw_name == emvybombercat), NOT an
# auto-flashable capability, because EMVyBomberCat is built from source (D2/§5).
# Fase 2 of docs/IMPLEMENTATION_PLAN_EMVyBomberCat_CLI.md.

import json
import sys
from typing import Dict, Iterable, List, Optional

import pytest

from conftest import FakeLink, flat, ok
from modules.core.cli import cli, main_cli
from modules.emvy import cli as emvycli
from modules.emvy.cli import (
    apdu_cmd,
    cardscan_cmd,
    emu_card_cmd,
    emu_ndef_cmd,
    emvy,
    info_cmd,
    mag_cmd,
    nfcinfo_cmd,
    read_cmd,
    reboot_cmd,
    tag_cmd,
)
from modules.emvy.parser import EmvyError

# What a healthy EMVyBomberCat answers `info` with (see §2.2 of the plan).
_EMVY_INFO = ok("", fw_name="emvybombercat", fw="1.1.0.1", state="idle")


# ── FakeEmvyLink ─────────────────────────────────────────────────────────────
#
# An EmvyLink stand-in for the operative-verb command tests (Fase 3+), mirroring
# FakeLink's shape but speaking `exchange()`/`stream_until()` instead of
# `command()`. ``script`` maps a sent line (or its verb, split on the first
# ' ' or ':') to the raw lines `exchange()` would have collected — the same
# shape parser.py's functions expect, so they run unmodified against them.


class FakeEmvyLink:
    def __init__(
        self,
        script: Optional[Dict[str, List[str]]] = None,
        stream_lines: Iterable[str] = (),
        interrupt_after: Optional[int] = None,
    ):
        self.script = dict(script or {})
        self.stream_lines = list(stream_lines)
        # If set, raise KeyboardInterrupt after this many stream lines have
        # been delivered to on_line — simulating Ctrl-C mid-emulation
        # (Fase 4's `emu ndef`/`emu card`). Mirrors EmvyLink.stream_until's
        # own contract: send stop_cmd, then re-raise.
        self.interrupt_after = interrupt_after
        self.sent: List[str] = []
        self.opened = False
        self.closed = False

    def open(self) -> "FakeEmvyLink":
        self.opened = True
        return self

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "FakeEmvyLink":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def send(self, line: str) -> None:
        self.sent.append(line.strip())

    def exchange(self, line, terminators=None, timeout=None) -> List[str]:
        self.send(line)
        full = line.strip()
        verb = full.split(":")[0].split(" ")[0]
        for key in (full, verb):
            if key in self.script:
                return list(self.script[key])
        return ["OK"]

    def stream_until(self, sentinel, on_line=None, stop_cmd=None, timeout=None):
        lines: List[str] = []
        for i, text in enumerate(self.stream_lines):
            if self.interrupt_after is not None and i == self.interrupt_after:
                if stop_cmd is not None:
                    self.send(stop_cmd)
                raise KeyboardInterrupt()
            lines.append(text)
            if on_line is not None:
                on_line(text)
            if text.startswith(sentinel):
                return lines
        raise EmvyError(f"timed out waiting for {sentinel!r}")


@pytest.fixture
def use_emvy_link(monkeypatch):
    """Point `_emvy_operative_session`'s EmvyLink at a FakeEmvyLink, after the
    identity gate (already stood up by `use_link`) passes."""

    def _use(fake: FakeEmvyLink) -> FakeEmvyLink:
        monkeypatch.setattr(emvycli, "EmvyLink", lambda *a, **k: fake)
        return fake

    return _use


# ── group wiring ─────────────────────────────────────────────────────────────


def test_emvy_group_is_registered_on_the_root_cli(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bombercat", "--help"])
    with pytest.raises(SystemExit) as e:
        main_cli()

    assert e.value.code == 0
    assert "emvy" in cli.commands


def test_emvy_help_lists_the_info_command(runner):
    result = runner.invoke(emvy, ["--help"])

    assert result.exit_code == 0
    assert "info" in flat(result.output)


# ── info ─────────────────────────────────────────────────────────────────────


def test_info_reports_version_state_and_operations(runner, use_link):
    fake = use_link(emvycli, FakeLink(responses={"info": _EMVY_INFO}))
    result = runner.invoke(info_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert fake.sent == ["info"]
    assert "1.1.0.1" in out
    assert "idle" in out
    # The operative surface the Fase 3-4 subcommands will drive is listed.
    for name in ("read", "apdu", "tag", "mag", "emu"):
        assert name in out


def test_info_renders_empty_fields_as_a_dash(runner, use_link):
    # A board that names itself but reports neither version nor state.
    use_link(emvycli, FakeLink(responses={"info": ok("", fw_name="emvybombercat")}))
    result = runner.invoke(info_cmd, [])

    assert result.exit_code == 0
    assert "—" in flat(result.output)


# ── identity gate ────────────────────────────────────────────────────────────


def test_info_refutes_a_different_firmware(runner, use_link):
    # The board answers the handshake but is NOT EMVyBomberCat (e.g. NFCGate).
    use_link(emvycli, FakeLink(responses={"info": ok("", fw_name="nfcgate")}))
    result = runner.invoke(info_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 1
    assert "not running EMVyBomberCat" in out
    assert "fw_name=nfcgate" in out
    # Refutes with a build-and-flash hint, never an auto-flash (D2/§5).
    assert "arduino-cli" in out
    assert "bombercat-firmware/EMVyBomberCat" in out


def test_info_refutes_a_board_that_reports_no_firmware_name(runner, use_link):
    # A pre-`fw_name` REPL build: answers ping/info but names nothing.
    use_link(emvycli, FakeLink(responses={"info": ok("", fw="0.9.0", state="idle")}))
    result = runner.invoke(info_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 1
    assert "not running EMVyBomberCat" in out
    assert "fw_name=—" in out


def test_info_refutes_a_board_that_will_not_handshake(runner, use_link):
    link = use_link(emvycli, FakeLink(ping_ok=False))
    result = runner.invoke(info_cmd, [])

    assert result.exit_code == 1
    assert "did not answer the handshake" in flat(result.output)
    assert link.closed


def test_info_closes_the_link_after_refuting(runner, use_link):
    # The gate must not leak the port when it rejects a wrong firmware.
    link = use_link(emvycli, FakeLink(responses={"info": ok("", fw_name="nfcgate")}))
    result = runner.invoke(info_cmd, [])

    assert result.exit_code == 1
    assert link.closed


# ── read ─────────────────────────────────────────────────────────────────────


def _gated(fake_emvy, use_link, use_emvy_link):
    """Pass the identity gate with a healthy board, then hand the operative
    session `fake_emvy`."""
    use_link(emvycli, FakeLink(responses={"info": _EMVY_INFO}))
    return use_emvy_link(fake_emvy)


def test_read_prints_a_table_of_parsed_fields(runner, use_link, use_emvy_link):
    fake = _gated(
        FakeEmvyLink(
            stream_lines=[
                "# EMV flow",
                "JSON_START",
                '{"pan": "4111111111111111", "exp": "2512", '
                '"aid": "A0000000031010"}',
                "JSON_END",
            ]
        ),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(read_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert fake.sent == ["SCAN 500"]
    assert "4111111111111111" in out
    assert "2512" in out
    assert "A0000000031010" in out


def test_read_json_emits_the_parsed_object(runner, use_link, use_emvy_link):
    _gated(
        FakeEmvyLink(
            stream_lines=["JSON_START", '{"pan": "4111111111111111"}', "JSON_END"]
        ),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(read_cmd, ["--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == {"pan": "4111111111111111"}


def test_read_raw_prints_the_stream_verbatim_and_skips_the_table(
    runner, use_link, use_emvy_link
):
    _gated(
        FakeEmvyLink(
            stream_lines=["# EMV flow", "JSON_START", '{"pan": "1"}', "JSON_END"]
        ),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(read_cmd, ["--raw"])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "JSON_START" in out and "JSON_END" in out
    # No parsed-table field label ("PAN") is rendered in --raw mode.
    assert "PAN" not in out


def test_read_reports_a_scan_error_without_waiting_out_the_timeout(
    runner, use_link, use_emvy_link
):
    # No JSON_END is ever sent on failure — just a "# ERROR: ..." log
    # (§2.3) — so `read` must bail on that line, not time out.
    _gated(
        FakeEmvyLink(stream_lines=["# scanning", "# ERROR: Timeout"]),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(read_cmd, [])

    assert result.exit_code == 1
    assert "Timeout" in flat(result.output)


def test_read_uses_the_amount_option(runner, use_link, use_emvy_link):
    fake = _gated(
        FakeEmvyLink(stream_lines=["JSON_START", "{}", "JSON_END"]),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(read_cmd, ["--amount", "100"])

    assert result.exit_code == 0
    assert fake.sent == ["SCAN 100"]


# ── apdu ─────────────────────────────────────────────────────────────────────


def test_apdu_requires_exactly_one_of_argument_or_stdin(runner):
    result = runner.invoke(apdu_cmd, [])
    assert result.exit_code == 1
    assert "exactly one" in flat(result.output)

    result = runner.invoke(apdu_cmd, ["00A404", "--stdin"])
    assert result.exit_code == 1
    assert "exactly one" in flat(result.output)


def test_apdu_rejects_bad_hex_before_opening_a_session(runner):
    # No use_link/use_emvy_link: local validation must reject this before any
    # port is ever touched.
    result = runner.invoke(apdu_cmd, ["ZZZZ"])

    assert result.exit_code == 1
    assert "not valid hex" in flat(result.output)


def test_apdu_one_shot_prints_the_response_and_releases(
    runner, use_link, use_emvy_link
):
    fake = _gated(
        FakeEmvyLink(script={"WAIT": ["READY:"], "APDU:00A404": ["RESP:9000"]}),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(apdu_cmd, ["00A404"])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "9000" in out
    assert fake.sent == ["WAIT 30000", "APDU:00A404", "RELEASE"]


def test_apdu_json_flag_emits_the_response_hex(runner, use_link, use_emvy_link):
    _gated(
        FakeEmvyLink(script={"WAIT": ["READY:"], "APDU:00A404": ["RESP:9000"]}),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(apdu_cmd, ["00A404", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == {"resp": "9000"}


def test_apdu_reports_nocard_and_never_releases(runner, use_link, use_emvy_link):
    # WAIT itself failed — gPassthroughActive was never set, so RELEASE would
    # be pointless (and the firmware never armed a card to release).
    fake = _gated(
        FakeEmvyLink(script={"WAIT": ["ERR:NOCARD"]}), use_link, use_emvy_link
    )
    result = runner.invoke(apdu_cmd, ["00A404"])

    assert result.exit_code == 1
    assert "NOCARD" in flat(result.output)
    assert "RELEASE" not in fake.sent


def test_apdu_reports_a_bad_apdu_but_still_releases(runner, use_link, use_emvy_link):
    fake = _gated(
        FakeEmvyLink(script={"WAIT": ["READY:"], "APDU:00A404": ["ERR:BADAPDU"]}),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(apdu_cmd, ["00A404"])

    assert result.exit_code == 1
    assert "BADAPDU" in flat(result.output)
    assert fake.sent == ["WAIT 30000", "APDU:00A404", "RELEASE"]


def test_apdu_stdin_sends_each_line_in_one_session(runner, use_link, use_emvy_link):
    fake = _gated(
        FakeEmvyLink(
            script={
                "WAIT": ["READY:"],
                "APDU:AA": ["RESP:1111"],
                "APDU:BB": ["RESP:2222"],
            }
        ),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(apdu_cmd, ["--stdin"], input="AA\nBB\n")
    out = flat(result.output)

    assert result.exit_code == 0
    assert "1111" in out and "2222" in out
    assert fake.sent == ["WAIT 30000", "APDU:AA", "APDU:BB", "RELEASE"]


def test_apdu_stdin_continues_after_one_bad_apdu(runner, use_link, use_emvy_link):
    _gated(
        FakeEmvyLink(
            script={
                "WAIT": ["READY:"],
                "APDU:AA": ["ERR:TXFAIL"],
                "APDU:BB": ["RESP:9000"],
            }
        ),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(apdu_cmd, ["--stdin"], input="AA\nBB\n")
    out = flat(result.output)

    assert result.exit_code == 1
    assert "TXFAIL" in out
    assert "9000" in out


# ── tag ──────────────────────────────────────────────────────────────────────


def test_tag_prints_proto_tech_uid(runner, use_link, use_emvy_link):
    # Real wire format (modes_tags.ino): TAG:<proto> TECH:<tech> UID:<hex> —
    # the TECH: field the plan's §2.3 table omitted.
    fake = _gated(
        FakeEmvyLink(script={"TAG": ["TAG:4 TECH:2 UID:04AABBCC"]}),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(tag_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert fake.sent == ["TAG"]
    assert "04AABBCC" in out


def test_tag_json_includes_tech(runner, use_link, use_emvy_link):
    _gated(
        FakeEmvyLink(script={"TAG": ["TAG:4 TECH:2 UID:04AABBCC"]}),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(tag_cmd, ["--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == {"proto": "4", "tech": "2", "uid": "04AABBCC"}


def test_tag_reports_notag(runner, use_link, use_emvy_link):
    _gated(FakeEmvyLink(script={"TAG": ["ERR:NOTAG"]}), use_link, use_emvy_link)
    result = runner.invoke(tag_cmd, [])

    assert result.exit_code == 1
    assert "NOTAG" in flat(result.output)


# ── mag ──────────────────────────────────────────────────────────────────────


def test_mag_plays_both_tracks(runner, use_link, use_emvy_link):
    fake = _gated(FakeEmvyLink(script={"MAG": ["OK"]}), use_link, use_emvy_link)
    result = runner.invoke(mag_cmd, ["--t1", "%B123?", "--t2", ";456?"])

    assert result.exit_code == 0
    assert fake.sent == ["MAG:%B123?|;456?"]
    assert "2-track" in flat(result.output)
    assert "emulated" in flat(result.output)


def test_mag_single_track_leaves_the_other_empty(runner, use_link, use_emvy_link):
    fake = _gated(FakeEmvyLink(script={"MAG": ["OK"]}), use_link, use_emvy_link)
    result = runner.invoke(mag_cmd, ["--t2", ";456?"])

    assert result.exit_code == 0
    assert fake.sent == ["MAG:|;456?"]
    assert "1-track" in flat(result.output)


def test_mag_single_track1_leaves_track2_empty(runner, use_link, use_emvy_link):
    fake = _gated(FakeEmvyLink(script={"MAG": ["OK"]}), use_link, use_emvy_link)
    result = runner.invoke(mag_cmd, ["--t1", "%B123?"])

    assert result.exit_code == 0
    assert fake.sent == ["MAG:%B123?|"]
    assert "1-track" in flat(result.output)


def test_mag_requires_at_least_one_track(runner):
    # No flags, and no use_link/use_emvy_link: rejected before any port is touched.
    result = runner.invoke(mag_cmd, [])

    assert result.exit_code == 1
    assert "at least one" in flat(result.output)


def test_mag_treats_an_empty_track_as_absent(runner):
    # `--t1 ""` is "no track 1" (matches the firmware's own non-empty test), so
    # an otherwise trackless invocation is still rejected before touching a port.
    result = runner.invoke(mag_cmd, ["--t1", ""])

    assert result.exit_code == 1
    assert "at least one" in flat(result.output)


def test_mag_rejects_a_pipe_in_a_track(runner):
    result = runner.invoke(mag_cmd, ["--t1", "a|b"])

    assert result.exit_code == 1
    assert "'|'" in flat(result.output)


def test_mag_reports_a_firmware_error(runner, use_link, use_emvy_link):
    _gated(FakeEmvyLink(script={"MAG": ["ERR:BUSY"]}), use_link, use_emvy_link)
    result = runner.invoke(mag_cmd, ["--t1", "%B123?"])

    assert result.exit_code == 1
    assert "BUSY" in flat(result.output)


# ── cardscan ─────────────────────────────────────────────────────────────────


def test_cardscan_prints_the_scanned_fields(runner, use_link, use_emvy_link):
    fake = _gated(
        FakeEmvyLink(
            script={
                "CARDSCAN": [
                    "# CARDSCAN: acerca la tarjeta contactless a leer...",
                    "EMU:SCANNED aid=A0000000031010 pan=4111111111111111 "
                    "exp=2512 t2=4111111111111111D2512",
                ]
            }
        ),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(cardscan_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert fake.sent == ["CARDSCAN"]
    assert "A0000000031010" in out
    assert "4111111111111111" in out
    assert "from ram" in out.lower() or "--from-ram" in out


def test_cardscan_json(runner, use_link, use_emvy_link):
    _gated(
        FakeEmvyLink(
            script={"CARDSCAN": ["EMU:SCANNED aid=A0 pan=4111 exp=2512 t2=41"]}
        ),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(cardscan_cmd, ["--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "aid": "A0",
        "pan": "4111",
        "exp": "2512",
        "t2": "41",
    }


def test_cardscan_reports_nocard(runner, use_link, use_emvy_link):
    _gated(FakeEmvyLink(script={"CARDSCAN": ["ERR:NOCARD"]}), use_link, use_emvy_link)
    result = runner.invoke(cardscan_cmd, [])

    assert result.exit_code == 1
    assert "NOCARD" in flat(result.output)


# ── emu ndef ─────────────────────────────────────────────────────────────────


def test_emu_ndef_streams_formatted_events(runner, use_link, use_emvy_link):
    fake = _gated(
        FakeEmvyLink(
            stream_lines=[
                "EMU:START len=4",
                "EMU:RX SELECT-APP D2760000850101 00A4040007D2760000850101",
                "EMU:TX 9000",
                "EMU:DONE sent=0 reason=stop",
            ]
        ),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(emu_ndef_cmd, ["AABBCCDD"])
    out = flat(result.output)

    assert result.exit_code == 0
    assert fake.sent[0] == "EMU:AABBCCDD"
    assert "EMU:START" in out
    assert "EMU:RX" in out
    assert "EMU:DONE" in out


def test_emu_ndef_raw_prints_lines_verbatim(runner, use_link, use_emvy_link):
    _gated(
        FakeEmvyLink(stream_lines=["EMU:START len=0", "EMU:DONE sent=0 reason=stop"]),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(emu_ndef_cmd, ["", "--raw"])

    assert result.exit_code == 0
    assert "EMU:START len=0" in flat(result.output)


def test_emu_ndef_rejects_bad_hex_before_opening_a_session(runner):
    result = runner.invoke(emu_ndef_cmd, ["ZZ"])

    assert result.exit_code == 1
    assert "not valid hex" in flat(result.output)


def test_emu_ndef_stops_cleanly_on_ctrl_c(runner, use_link, use_emvy_link):
    fake = _gated(
        FakeEmvyLink(
            stream_lines=["EMU:START len=2", "EMU:RX APDU 00A4", "EMU:TX 9000"],
            interrupt_after=1,
        ),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(emu_ndef_cmd, ["AABB"])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "stopped" in out.lower()
    assert "STOP" in fake.sent


# ── emu card ─────────────────────────────────────────────────────────────────


def test_emu_card_bare_uses_the_canned_test_card(runner, use_link, use_emvy_link):
    fake = _gated(
        FakeEmvyLink(
            stream_lines=["EMU:START mode=emv", "EMU:DONE sent=0 reason=stop"]
        ),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(emu_card_cmd, [])

    assert result.exit_code == 0
    assert fake.sent[0] == "EMUEMV"


def test_emu_card_from_ram(runner, use_link, use_emvy_link):
    fake = _gated(
        FakeEmvyLink(
            stream_lines=["EMU:START mode=emv", "EMU:DONE sent=0 reason=stop"]
        ),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(emu_card_cmd, ["--from-ram"])

    assert result.exit_code == 0
    assert fake.sent[0] == "EMUEMV:RAM"


def test_emu_card_custom_fields(runner, use_link, use_emvy_link):
    fake = _gated(
        FakeEmvyLink(
            stream_lines=["EMU:START mode=emv", "EMU:DONE sent=0 reason=stop"]
        ),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(emu_card_cmd, ["--aid", "A0", "--pan", "4111"])

    assert result.exit_code == 0
    assert fake.sent[0] == "EMUEMV:A0|4111||"


def test_emu_card_from_ram_and_custom_fields_are_mutually_exclusive(runner):
    result = runner.invoke(emu_card_cmd, ["--from-ram", "--aid", "A0"])

    assert result.exit_code == 1
    assert "mutually exclusive" in flat(result.output)


def test_emu_card_rejects_bad_hex_before_opening_a_session(runner):
    result = runner.invoke(emu_card_cmd, ["--pan", "ZZ"])

    assert result.exit_code == 1
    assert "not valid hex" in flat(result.output)


# ── nfcinfo ──────────────────────────────────────────────────────────────────


def test_nfcinfo_reports_the_firmware_version(runner, use_link, use_emvy_link):
    fake = _gated(
        FakeEmvyLink(script={"NFCINFO": ["NFCINFO: fwver=32 (chip vivo)"]}),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(nfcinfo_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert fake.sent == ["NFCINFO"]
    assert "32" in out


def test_nfcinfo_reports_a_dead_chip(runner, use_link, use_emvy_link):
    _gated(
        FakeEmvyLink(
            script={"NFCINFO": ["NFCINFO: ERR connectNCI (chip no responde)"]}
        ),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(nfcinfo_cmd, [])

    assert result.exit_code == 1
    assert "connectNCI" in flat(result.output)


# ── reboot ───────────────────────────────────────────────────────────────────


def test_reboot_sends_reboot_and_warns_to_reconnect(runner, use_link, use_emvy_link):
    fake = _gated(FakeEmvyLink(), use_link, use_emvy_link)
    result = runner.invoke(reboot_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert fake.sent == ["REBOOT"]
    assert "re-enumerate" in out
    assert fake.closed
