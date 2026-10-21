#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

# test_mifare_device_dump.py — Fase 4 of the "Plan de Implementación Firmware
# MIFARE" (task 4.4): the DEVICE reads the card and dumps it (ONE round-trip
# for the whole card), and the key recovery it does on the way is the same
# logic the host runs — the trailer-read pass.
#
# Proves:
#   - the device's dump wire format is parsed exactly (per-sector blocks, the
#     key type that opened the sector, the reason for a hole, the counters and
#     the block 0), and a malformed payload is a hole, never a guess;
#   - `mifare dump` in device mode issues ONE command and no per-sector read;
#   - the mode policy: no --keys-file -> the device dumps (it has the keys its
#     own sweep recovered); with --keys-file -> the host dumps, because the
#     user's keys may open sectors the device's sweep never opened;
#   - an older firmware (no `mifare dump`) falls back to the host-driven dump;
#   - in device mode the host does NOT repeat the trailer-read pass when the
#     device already did it (the `trailer=` counter), and DOES repeat it with
#     an older firmware that has no such counter.
#
# No PN7150: the device is a fake that answers like the firmware does.

import json

import pytest

from modules.core.bombercat import Response
from modules.tags.mifare import session as mifare_session
from modules.tags.mifare.check import mifare_check_cmd
from modules.tags.mifare.cli import mifare_standalone_cmd
from modules.tags.mifare.device_sweep import (
    DeviceDumpNoSweep,
    DeviceDumpUnsupported,
    parse_device_dump,
    run_device_dump,
)
from modules.tags.mifare.dump import mifare_dump_cmd

from conftest import FakeLink, err, flat, ok

import mifare_card as mc


# ── a fake that speaks the device's dump protocol ────────────────────────────


class DumpLink(FakeLink):
    """A fake device that answers `mifare dump` the way the firmware does:
    one `:mifare_dump_sN <hex128|-> <A|B|-> [reason]` line per sector, the
    counters line, the block 0, then `+OK`."""

    def __init__(self, sectors: dict, stat: str = "", card0: str = "", **kw):
        super().__init__(**kw)
        self.sectors = sectors  # {sector: (hex128|None, "A"/"B"/None, reason)}
        self.stat = stat
        self.card0 = card0
        self.dumps = []  # the `mifare dump ...` lines it was asked to run

    def command(self, line: str, read_timeout=None) -> Response:
        stripped = line.strip()
        if stripped.startswith("mifare dump"):
            self.sent.append(stripped)
            self.dumps.append(stripped)
            data = {}
            for sector, (data_hex, key_type, reason) in self.sectors.items():
                parts = [data_hex or "-", key_type or "-"]
                if reason:
                    parts.append(reason)
                data[f"mifare_dump_s{sector}"] = " ".join(parts)
            if self.stat:
                data["mifare_dump_stat"] = self.stat
            if self.card0:
                data["mifare_card0"] = self.card0
            return ok(**data)
        return super().command(stripped, read_timeout)


def _sector_hex(sector, blocks=("00", "11", "22")):
    """A sector's 4 blocks as 128 hex chars (3 data blocks + a trailer)."""
    data = "".join(b * 16 for b in blocks)
    return data + ("00" * 6) + mc.AC_TRANSPORT + ("FFFFFFFFFFFF")


def _dump_lines(link):
    return [line for line in link.sent if line.startswith("mifare dump")]


def _sector_reads(link):
    return [line for line in link.sent if line.startswith("mifare sector")]


# ── the wire format ──────────────────────────────────────────────────────────


def test_parse_reads_the_sectors_the_counters_and_the_block_0():
    hex128 = _sector_hex(0)
    result = parse_device_dump(
        {
            "mifare_dump_s0": f"{hex128} A",
            "mifare_dump_s1": "- - nokey",
            "mifare_dump_stat": "read=1 failed=1 total=2",
            "mifare_card0": (
                "DECAFBAD07"
                + "080400"
                + "C2000000B4030103"
                + " uid=DECAFBAD sak=08 atqa=0400 bcc=07 bcc_ok=1"
            ),
        }
    )
    assert result.sectors[0].data_hex == hex128
    assert result.sectors[0].key_type == "A"
    assert result.sectors[0].reason is None
    assert result.sectors[1].data_hex is None
    assert result.sectors[1].key_type is None
    assert result.sectors[1].reason == "nokey"
    assert result.read == 1
    assert result.failed == 1
    assert result.total == 2
    assert result.card0 == "DECAFBAD07080400C2000000B4030103"
    assert result.card0_fields["uid"] == "DECAFBAD"
    assert result.card0_fields["bcc_ok"] == "1"
    assert result.blocks_of(0) == [
        "00" * 16,
        "11" * 16,
        "22" * 16,
        "00" * 6 + mc.AC_TRANSPORT + "FFFFFFFFFFFF",
    ]
    assert result.blocks_of(1) is None


def test_parse_never_guesses_a_malformed_payload():
    # A payload that isn't a full sector is a hole, never a guess.
    result = parse_device_dump({"mifare_dump_s0": "DEADBEEF A"})
    assert result.sectors[0].data_hex is None
    assert result.blocks_of(0) is None


def test_parse_ignores_unrelated_lines():
    result = parse_device_dump(
        {
            "mifare": "1234 DECAFBAD 4 - ok",
            "mifare_check_s0": "FFFFFFFFFFFF FFFFFFFFFFFF",
            "mifare_dump_s0": _sector_hex(0) + " A",
        }
    )
    assert set(result.sectors) == {0}


# ── the device dump, end to end ──────────────────────────────────────────────


def test_the_device_dump_asks_once_and_reads_no_sector_by_hand():
    link = DumpLink(
        {0: (_sector_hex(0), "A", None), 1: (None, None, "nokey")},
        stat="read=1 failed=1 total=2",
        card0="DECAFBAD07080400C2000000B4030103",
    )
    result = run_device_dump(link, 2)
    assert result.read == 1
    assert result.total == 2
    assert _dump_lines(link) == ["mifare dump --sectors 2"]
    assert _sector_reads(link) == []  # the device read the card, not the host


def test_a_device_without_sweep_results_says_so():
    link = FakeLink(
        responses={
            "mifare dump --sectors 1": err(
                "no sweep results yet - run 'mifare check' first"
            )
        }
    )
    with pytest.raises(DeviceDumpNoSweep):
        run_device_dump(link, 1)


def test_an_older_firmware_has_no_device_dump():
    link = FakeLink(
        responses={"mifare dump --sectors 1": err("unknown mifare subcommand")}
    )
    with pytest.raises(DeviceDumpUnsupported):
        run_device_dump(link, 1)


# ── the CLI ──────────────────────────────────────────────────────────────────


def test_dump_in_device_mode_is_one_round_trip(runner, use_link):
    link = use_link(
        mifare_session,
        DumpLink(
            {0: (_sector_hex(0), "A", None), 1: (None, None, "nokey")},
            stat="read=1 failed=1 total=2",
            card0="DECAFBAD07080400C2000000B4030103",
        ),
    )
    result = runner.invoke(
        mifare_dump_cmd, ["--mode", "device", "--sectors", "2", "--json"]
    )
    assert result.exit_code == 1  # sector 1 is a gap
    doc = json.loads(result.stdout)
    assert doc["sectors_read"] == 1
    assert doc["uid"] == "DECAFBAD"  # the block 0 the device read
    assert doc["failed_sectors"] == [{"sector": 1, "reason": "no key available"}]
    assert doc["sectors"][0]["opened_with"] == "A"
    assert _dump_lines(link) == ["mifare dump --sectors 2"]
    assert _sector_reads(link) == []


def test_dump_without_a_keys_file_uses_the_device(runner, use_link):
    # No keyfile: the device's own sweep is the only source of keys, and it
    # reads the card itself.
    link = use_link(
        mifare_session,
        DumpLink({0: (_sector_hex(0), "A", None)}, stat="read=1 failed=0 total=1"),
    )
    result = runner.invoke(mifare_dump_cmd, ["--sectors", "1", "--json"])
    assert result.exit_code == 0
    assert _dump_lines(link) == ["mifare dump --sectors 1"]


def test_dump_with_a_keys_file_keeps_the_host_reading(runner, use_link, tmp_path):
    # The user's keys may open sectors the device's own sweep never opened,
    # so an explicit keyfile keeps the host-driven dump (and says so).
    keyfile = tmp_path / "sector.keys"
    keyfile.write_text("0:FFFFFFFFFFFF:\n")
    link = use_link(
        mifare_session,
        FakeLink(
            responses={
                "mifare sector 0 A FFFFFFFFFFFF": ok(mifare_sector=_sector_hex(0))
            }
        ),
    )
    result = runner.invoke(
        mifare_dump_cmd, ["--keys-file", str(keyfile), "--sectors", "1", "--json"]
    )
    assert result.exit_code == 0
    assert _dump_lines(link) == []
    assert _sector_reads(link) == ["mifare sector 0 A FFFFFFFFFFFF"]


def test_host_mode_without_a_keys_file_is_refused(runner, use_link):
    use_link(mifare_session, FakeLink())
    result = runner.invoke(mifare_dump_cmd, ["--mode", "host", "--sectors", "1"])
    assert result.exit_code == 1
    assert "--keys-file" in flat(result.output)


def test_an_older_firmware_falls_back_to_the_host_dump(runner, use_link, tmp_path):
    keyfile = tmp_path / "sector.keys"
    keyfile.write_text("0:FFFFFFFFFFFF:\n")
    link = use_link(
        mifare_session,
        FakeLink(
            responses={
                "mifare dump --sectors 1": err("unknown mifare subcommand"),
                "mifare sector 0 A FFFFFFFFFFFF": ok(mifare_sector=_sector_hex(0)),
            }
        ),
    )
    result = runner.invoke(
        mifare_dump_cmd,
        ["--keys-file", str(keyfile), "--mode", "auto", "--sectors", "1", "--json"],
    )
    assert result.exit_code == 0
    assert _sector_reads(link) == ["mifare sector 0 A FFFFFFFFFFFF"]


def test_device_mode_without_sweep_results_fails_loudly(runner, use_link):
    use_link(
        mifare_session,
        FakeLink(
            responses={
                "mifare dump --sectors 1": err(
                    "no sweep results yet - run 'mifare check' first"
                )
            }
        ),
    )
    result = runner.invoke(mifare_dump_cmd, ["--mode", "device", "--sectors", "1"])
    assert result.exit_code == 1
    assert "sweep" in flat(result.output)


# ── the standalone mode (the field trigger) ──────────────────────────────────


def test_the_standalone_mode_is_read_and_set_through_the_cli(runner, use_link):
    # The board runs the whole workflow by itself when a card is detected:
    # the CLI just reads and sets the flag (it survives a reset).
    link = use_link(
        mifare_session,
        FakeLink(responses={"mifare standalone": ok("standalone off")}),
    )
    result = runner.invoke(mifare_standalone_cmd, ["--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"standalone": "off"}
    assert "mifare standalone" in link.sent

    link.responses["mifare standalone on"] = ok("standalone on")
    result = runner.invoke(mifare_standalone_cmd, ["--on", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"standalone": "on"}
    assert "mifare standalone on" in link.sent


def test_the_standalone_mode_reports_a_refusal(runner, use_link):
    use_link(
        mifare_session,
        FakeLink(responses={"mifare standalone": err("unknown mifare subcommand")}),
    )
    result = runner.invoke(mifare_standalone_cmd, [])
    assert result.exit_code == 1
    assert "standalone failed" in flat(result.output)


# ── the trailer-read pass: done once, on one side ───────────────────────────


class SweepLink(FakeLink):
    """A fake device that answers `mifare check` the way the firmware does."""

    def __init__(self, sectors: dict, stat: str = "", **kw):
        super().__init__(**kw)
        self.sectors = sectors
        self.stat = stat

    def command(self, line: str, read_timeout=None) -> Response:
        stripped = line.strip()
        if stripped.startswith("mifare check"):
            self.sent.append(stripped)
            data = {}
            for sector, (key_a, key_b) in self.sectors.items():
                data[f"mifare_check_s{sector}"] = f"{key_a or '-'} {key_b or '-'}"
            if self.stat:
                data["mifare_check_stat"] = self.stat
            return ok(**data)
        return super().command(stripped, read_timeout)


def _keyfile(tmp_path, *keys):
    path = tmp_path / "keys.keys"
    path.write_text("".join(k + "\n" for k in keys))
    return path


def test_the_host_does_not_repeat_the_trailer_read_the_device_already_did(
    runner, use_link, tmp_path
):
    # The device ran the trailer-read pass itself (it reports the `trailer=`
    # counter): it recovered the same Key B the host would have read over
    # USB, so the host must not spend a round-trip per sector on it.
    link = use_link(
        mifare_session,
        SweepLink(
            {0: ("FFFFFFFFFFFF", "AABBCCDDEEFF")},
            stat="attempts=2 naive=64 recovered=2/2 trailer=1",
        ),
    )
    keyfile = _keyfile(tmp_path, "FFFFFFFFFFFF")
    result = runner.invoke(
        mifare_check_cmd,
        ["--mode", "device", "--keys", str(keyfile), "--sectors", "1", "--json"],
    )
    doc = json.loads(result.stdout)
    assert doc["sectors"][0]["key_b"] == "AABBCCDDEEFF"
    assert _sector_reads(link) == []  # no host-side trailer read


def test_an_older_firmware_still_gets_the_host_side_trailer_read(
    runner, use_link, tmp_path
):
    # No `trailer=` counter: the device did not read the trailers, so the
    # host still does it (as it always did).
    link = use_link(
        mifare_session,
        SweepLink(
            {0: ("FFFFFFFFFFFF", None)},
            stat="attempts=2 naive=64 recovered=1/2",
            responses={
                "mifare sector 0 A FFFFFFFFFFFF": ok(
                    mifare_sector=(
                        "00" * 48 + "00" * 6 + mc.AC_TRANSPORT + "AABBCCDDEEFF"
                    )
                )
            },
        ),
    )
    keyfile = _keyfile(tmp_path, "FFFFFFFFFFFF")
    result = runner.invoke(
        mifare_check_cmd,
        ["--mode", "device", "--keys", str(keyfile), "--sectors", "1", "--json"],
    )
    doc = json.loads(result.stdout)
    assert doc["sectors"][0]["key_b"] == "AABBCCDDEEFF"
    assert _sector_reads(link) == ["mifare sector 0 A FFFFFFFFFFFF"]


# ── the board's own wall-clock split, and the cached sweep ───────────────────


def test_the_wall_clock_split_of_the_board_is_reported(runner, use_link, tmp_path):
    # The board measures where its own sweep spent the time (Fase 4, Sesión
    # 11): the same number of authentications, but the split between the
    # authentication itself and the re-select that follows a wrong key.
    use_link(
        mifare_session,
        SweepLink(
            {0: ("FFFFFFFFFFFF", "FFFFFFFFFFFF")},
            stat=(
                "attempts=2 naive=64 recovered=2/2 trailer=0 auth_ms=32 "
                "reselect_ms=234 reset_ms=130 detect_ms=26 trailer_ms=0"
            ),
        ),
    )
    keyfile = _keyfile(tmp_path, "FFFFFFFFFFFF")
    result = runner.invoke(
        mifare_check_cmd,
        ["--mode", "device", "--keys", str(keyfile), "--sectors", "1", "--json"],
    )
    doc = json.loads(result.stdout)
    assert doc["stats"]["auth_ms"] == 32
    assert doc["stats"]["reselect_ms"] == 234


def test_a_cached_sweep_is_reported_as_such(runner, use_link, tmp_path):
    # The device reused its own sweep (it had already swept the card on
    # detection): the host must say so instead of pretending it swept again.
    use_link(
        mifare_session,
        SweepLink(
            {0: ("FFFFFFFFFFFF", "FFFFFFFFFFFF")},
            stat="attempts=2 naive=64 recovered=2/2 trailer=0 cached=1",
        ),
    )
    keyfile = _keyfile(tmp_path, "FFFFFFFFFFFF")
    result = runner.invoke(
        mifare_check_cmd,
        ["--mode", "device", "--keys", str(keyfile), "--sectors", "1", "--json"],
    )
    doc = json.loads(result.stdout)
    assert doc["stats"]["cached"] is True
    # The keys are the same either way: the cache holds the sweep's results.
    assert doc["sectors"][0]["key_a"] == "FFFFFFFFFFFF"


def test_an_older_firmware_reports_no_cache(runner, use_link, tmp_path):
    # No `cached=` field: the host must not claim the device reused a sweep.
    use_link(
        mifare_session,
        SweepLink(
            {0: ("FFFFFFFFFFFF", "FFFFFFFFFFFF")},
            stat="attempts=2 naive=64 recovered=2/2",
        ),
    )
    keyfile = _keyfile(tmp_path, "FFFFFFFFFFFF")
    result = runner.invoke(
        mifare_check_cmd,
        ["--mode", "device", "--keys", str(keyfile), "--sectors", "1", "--json"],
    )
    doc = json.loads(result.stdout)
    assert "cached" not in doc["stats"]
