#!/usr/bin/env python3

# Electronic Cats
# test_cli_emvy.py — `bombercat emvy info` and the identity gate shared by the
# group (modules/emvy/cli.py). Same pattern as test_cli_magspoof.py: CliRunner +
# FakeLink, driving the command's real logic against a scripted `info` reply.
# The group gates on CAP_EMVY (auto-flashable now that build-firmware.yml ships
# EMVyBomberCat.uf2 — see test_autoflash_wiring.py) and then re-checks firmware
# identity over the handshake (fw_name == emvybombercat); these tests cover that
# second gate, with `use_link` standing in for the auto-flash step.
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
    probe_card_cmd,
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
        self.stream_kwargs: Dict[str, object] = {}
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

    def stream_until(
        self, sentinel, on_line=None, stop_cmd=None, timeout=None, **kwargs
    ):
        self.stream_kwargs = {"sentinel": sentinel, "stop_cmd": stop_cmd, **kwargs}
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
    # The hint points at the prebuilt image (and --auto-flash), not at a
    # build-from-source workaround.
    assert "bombercat flash EMVyBomberCat" in out
    assert "--auto-flash" in out


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


def test_read_decodes_the_scanned_track2_equivalent(runner, use_link, use_emvy_link):
    # A chip read hands track2 back as the EMV tag-57 equivalent (PAN 'D' YYMM
    # SC disc 'F'…); `read` should decode its standard + Service Code verdict
    # via core. SC 201 = chip required (1st digit 2), no PIN.
    _gated(
        FakeEmvyLink(
            stream_lines=[
                "JSON_START",
                '{"pan": "4111111111111111", '
                '"track2": "4111111111111111D2512201123456789F"}',
                "JSON_END",
            ]
        ),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(read_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "ISO 7813 financial" in out
    assert "chip required" in out


def test_read_json_stays_a_verbatim_passthrough(runner, use_link, use_emvy_link):
    # Even with a decodable track2, --json emits exactly the firmware object —
    # the analysis is a human-view convenience, not part of the machine output.
    _gated(
        FakeEmvyLink(
            stream_lines=[
                "JSON_START",
                '{"pan": "4111111111111111", '
                '"track2": "4111111111111111D2512201123456789F"}',
                "JSON_END",
            ]
        ),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(read_cmd, ["--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "pan": "4111111111111111",
        "track2": "4111111111111111D2512201123456789F",
    }


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


def test_emu_stream_passes_the_trigger_as_restart_cmd(runner, use_link, use_emvy_link):
    """A mid-emulation MCU reset (USB re-enumeration) must not kill the
    session: the CLI hands stream_until the trigger line as restart_cmd so the
    link reopens the port and re-sends the emulation."""
    fake = _gated(
        FakeEmvyLink(
            stream_lines=["EMU:START mode=emv", "EMU:DONE sent=0 reason=stop"]
        ),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(emu_card_cmd, [])

    assert result.exit_code == 0
    assert fake.stream_kwargs.get("restart_cmd") == fake.sent[0]


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
    # Bare emulation with the default CID (80=ARQC): the firmware receives
    # the CID as the 5th field — CID-aware firmware honors it, old firmware
    # ignores it.
    assert fake.sent[0] == "EMUEMV:||||80"


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
    assert fake.sent[0] == "EMUEMV:RAM|80"


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
    assert fake.sent[0] == "EMUEMV:A0|4111|||80"


def test_emu_card_cid_is_sent_on_every_trigger_shape(runner, use_link, use_emvy_link):
    """--cid reaches the firmware as the 5th EMUEMV field (or after RAM|),
    whatever the card source — that field is what CID-aware firmware reads."""
    cases = [
        (["--cid", "40"], "EMUEMV:||||40"),
        (["--cid", "00", "--from-ram"], "EMUEMV:RAM|00"),
        (
            ["--cid", "40", "--aid", "A0", "--pan", "4111", "--exp", "2512"],
            "EMUEMV:A0|4111|2512||40",
        ),
    ]
    for args, expected in cases:
        fake = _gated(
            FakeEmvyLink(
                stream_lines=["EMU:START mode=emv", "EMU:DONE sent=0 reason=stop"]
            ),
            use_link,
            use_emvy_link,
        )
        result = runner.invoke(emu_card_cmd, args)
        assert result.exit_code == 0, flat(result.output)
        assert fake.sent[0] == expected


def test_emu_card_rejects_an_invalid_cid(runner):
    # click.Choice validates before any port is touched.
    result = runner.invoke(emu_card_cmd, ["--cid", "41"])

    assert result.exit_code == 2
    assert "not one of" in flat(result.output)


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


# ── probe-card ───────────────────────────────────────────────────────────────
#
# Scripted card answers for the minimal terminal flow. The PPSE FCI carries
# the Visa AID, the SELECT-AID FCI announces a PDOL with only deterministic
# tags (no 9A date / 9F37 random), and the GPO answers a 77 template whose
# AFL sends probe-card to READ RECORD sfi=1 rec=1.


def _ppse_resp() -> str:
    # 6F { 84 0E "2PAY.SYS.DDF01", A5 { BF0C { 61 { 4F <aid> } } } }
    return (
        "6F20"
        "840E325041592E5359532E4444463031"
        "A50E"
        "BF0C0B"
        "6109"
        "4F07A0000000031010"
        "9000"
    )


_PPSE_CMD = "00A404000E325041592E5359532E444446303100"
# SELECT AID A0000000031010 (Le=00)
_SELECT_AID_CMD = "00A4040007A000000003101000"
# PDOL: 9F02/6 9F1A/2 95/5 5F2A/2 9C/1 9F35/1 — all deterministic values.
_PDOL = "9F02069F1A0295055F2A029C019F3501"
# 6F { 84 07 <aid>, A5 { 9F38 10 <pdol> } }
_FCI_RESP = "6F1E" "8407A0000000031010" "A513" "9F3810" + _PDOL + "9000"
# GPO body for amount=0, country=currency="0484": the tags above in PDOL order.
_GPO_BODY = "000000000000" "0484" "0000000000" "0484" "00" "22"
# Lc = 2 (83 <len>) + 17-byte body = 0x13; 83 len = 0x11.
_GPO_CMD = "80A80000138311" + _GPO_BODY + "00"
# GPO answer: AIP 1C00, AFL sfi=1 records 1..1; then the record itself.
_GPO_RESP = "770A" "82021C00" "940408010100" "9000"
_RECORD_CMD = "00B2010C00"
_RECORD_RESP = "700A" "5A0841111111111111" "9000"


def _probe_script(**overrides):
    script = {
        "WAIT 60000": ["READY:"],
        f"APDU:{_PPSE_CMD}": [f"RESP:{_ppse_resp()}"],
        f"APDU:{_SELECT_AID_CMD}": [f"RESP:{_FCI_RESP}"],
        f"APDU:{_GPO_CMD}": [f"RESP:{_GPO_RESP}"],
        f"APDU:{_RECORD_CMD}": [f"RESP:{_RECORD_RESP}"],
    }
    script.update(overrides)
    return script


def test_probe_card_runs_the_terminal_flow_and_prints_each_step(
    runner, use_link, use_emvy_link
):
    fake = _gated(FakeEmvyLink(script=_probe_script()), use_link, use_emvy_link)
    result = runner.invoke(probe_card_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0, out
    # WAIT → PPSE → SELECT AID → GPO → READ RECORD, and RELEASE on exit.
    assert fake.sent == [
        "WAIT 60000",
        f"APDU:{_PPSE_CMD}",
        f"APDU:{_SELECT_AID_CMD}",
        f"APDU:{_GPO_CMD}",
        f"APDU:{_RECORD_CMD}",
        "RELEASE",
    ]
    # Every APDU and its raw response are printed.
    for blob in (
        _PPSE_CMD,
        _SELECT_AID_CMD,
        _GPO_CMD,
        _RECORD_CMD,
        _ppse_resp(),
        _FCI_RESP,
        _GPO_RESP,
        _RECORD_RESP,
    ):
        assert blob in out
    # Summary: AID from the PPSE, PDOL, and the parsed AFL.
    assert "A0000000031010" in out
    assert _PDOL in out
    assert "08010100" in out


def test_probe_card_gpo_lc_counts_the_wrapper_not_the_le(
    runner, use_link, use_emvy_link
):
    """Regression guard for a real-hardware bug: Lc must be 2+len(body) (the
    83 <len> wrapper plus the values, not the trailing Le) — an off-by-one
    makes cards answer 6985. The scripted GPO above only answers when the
    exact command arrives, so a wrong Lc would time out here."""
    fake = _gated(FakeEmvyLink(script=_probe_script()), use_link, use_emvy_link)
    result = runner.invoke(probe_card_cmd, [])

    assert result.exit_code == 0, flat(result.output)
    assert f"APDU:{_GPO_CMD}" in fake.sent
    # body = 17 bytes → Lc 0x13, 83-length 0x11.
    assert _GPO_CMD.startswith("80A80000138311")


def test_probe_card_feeds_the_amount_country_and_currency(
    runner, use_link, use_emvy_link
):
    # amount 1234 cents → 9F02 "000000001234"; country 0124 → 9F1A "0124";
    # currency 0978 overrides 5F2A while 9F1A keeps the country.
    body = "000000001234" "0124" "0000000000" "0978" "00" "22"
    gpo = "80A80000138311" + body + "00"
    fake = _gated(
        FakeEmvyLink(script=_probe_script(**{f"APDU:{gpo}": [f"RESP:{_GPO_RESP}"]})),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(
        probe_card_cmd, ["--amount", "1234", "--country", "0124", "--currency", "0978"]
    )

    assert result.exit_code == 0, flat(result.output)
    assert f"APDU:{gpo}" in fake.sent


def test_probe_card_gpo_decline_is_reported_not_fatal(runner, use_link, use_emvy_link):
    # 6985: the card declines this PDOL/context. probe-card reports the SW,
    # skips the records, and still exits 0 (the probe itself worked).
    fake = _gated(
        FakeEmvyLink(script=_probe_script(**{f"APDU:{_GPO_CMD}": ["RESP:6985"]})),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(probe_card_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "6985" in out
    assert f"APDU:{_RECORD_CMD}" not in fake.sent
    assert "RELEASE" in fake.sent


def test_probe_card_reports_nocard(runner, use_link, use_emvy_link):
    _gated(
        FakeEmvyLink(script={"WAIT 60000": ["ERR:NOCARD"]}),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(probe_card_cmd, [])

    assert result.exit_code == 1
    assert "NOCARD" in flat(result.output)


def test_probe_card_reports_a_declined_select_aid(runner, use_link, use_emvy_link):
    fake = _gated(
        FakeEmvyLink(
            script=_probe_script(**{f"APDU:{_SELECT_AID_CMD}": ["RESP:6A82"]})
        ),
        use_link,
        use_emvy_link,
    )
    result = runner.invoke(probe_card_cmd, [])

    assert result.exit_code == 1
    out = flat(result.output)
    assert "6A82" in out
    assert f"APDU:{_GPO_CMD}" not in fake.sent


def test_probe_card_rejects_a_negative_amount(runner):
    result = runner.invoke(probe_card_cmd, ["--amount", "-5"])

    assert result.exit_code == 1
    assert "negative" in flat(result.output)


def test_probe_card_rejects_bad_country_hex(runner):
    result = runner.invoke(probe_card_cmd, ["--country", "ZZ"])

    assert result.exit_code == 1
    assert "not valid hex" in flat(result.output)
