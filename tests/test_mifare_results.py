#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

# test_mifare_results.py — Fase 4, task 4.6: the results the board stored
# while it worked on its own.
#
# The standalone workflow reads a card with NO PC attached, so the dump goes
# out over the serial port to nobody. The firmware keeps the last results in
# its own flash; the host collects them later. What matters:
#
#   - the listing is parsed exactly (uid, sectors read, gaps, the board's own
#     uptime), and a malformed token is never guessed;
#   - `mifare dump --last` rebuilds the CANONICAL dump from the stored
#     result, holes included, so `mifare analyze` can report on it - which is
#     what task 4.6 asks for (the honest gap report in standalone mode);
#   - an older firmware (no `mifare results`) says so instead of failing
#     obscurely.
#
# No PN7150: the device is a fake that answers like the firmware does.

import json

import pytest

from modules.core.bombercat import Response
from modules.tags.mifare import session as mifare_session
from modules.tags.mifare.analyze import analyze_dump
from modules.tags.mifare.device_results import (
    DeviceResultsError,
    DeviceResultsUnsupported,
    parse_device_results,
    run_device_results,
    run_device_results_clear,
    run_device_stored_result,
)
from modules.tags.mifare.dump import mifare_dump_cmd
from modules.tags.mifare.results import mifare_results_cmd

from conftest import FakeLink, err, flat, ok

import mifare_card as mc


# ── a fake that speaks the results protocol ──────────────────────────────────


class ResultsLink(FakeLink):
    """A fake board with a ring of stored results.

    `listing` is the dict the board answers `mifare results` with; `stored`
    is the dict it answers `mifare results show <n>` with (the very same
    lines a live dump emits)."""

    def __init__(self, listing=None, stored=None, **kw):
        super().__init__(**kw)
        self.listing = listing if listing is not None else {}
        self.stored = stored if stored is not None else {}

    def command(self, line: str, read_timeout=None) -> Response:
        stripped = line.strip()
        if stripped == "mifare results clear":
            self.sent.append(stripped)
            self.listing = {"mifare_results_stat": "slots=8 used=0 seq=0"}
            return ok(message="results cleared")
        if stripped.startswith("mifare results show"):
            self.sent.append(stripped)
            if not self.stored:
                return err("no such stored result")
            return ok(**self.stored)
        if stripped == "mifare results":
            self.sent.append(stripped)
            return ok(**self.listing)
        return super().command(stripped, read_timeout)


def _listing(results=((0, 12, 34567, "AABBCCDD", 16, 15, 1),)):
    """The `:mifare_resultN …` lines of a listing, plus the stat line."""
    data = {}
    for index, seq, ts, uid, sectors, read, failed in results:
        data["mifare_result%d" % index] = (
            "seq=%d ts=%d uid=%s sectors=%d read=%d failed=%d"
            % (seq, ts, uid, sectors, read, failed)
        )
    data["mifare_results_stat"] = "slots=8 used=%d seq=12" % len(results)
    return data


def _sector_hex(sector, blocks=("00", "11", "22")):
    """A sector's 4 blocks as 128 hex chars (3 data blocks + a trailer)."""
    data = "".join(b * 16 for b in blocks)
    return data + ("00" * 6) + mc.AC_TRANSPORT + ("FFFFFFFFFFFF")


def _stored_result(sectors=2, read=1, failed=1):
    """The lines the board emits when it re-emits a stored result."""
    data = {
        "mifare_result0": "seq=12 ts=34567 uid=AABBCCDD sectors=%d read=%d failed=%d"
        % (sectors, read, failed),
        "mifare_dump_s0": "%s A" % _sector_hex(0),
        "mifare_dump_s1": "- - nokey",
        "mifare_dump_stat": "read=%d failed=%d total=%d" % (read, failed, sectors),
        "mifare_card0": (
            "DECAFBAD07"
            + "080400"
            + "C2000000B4030103"
            + " uid=DECAFBAD sak=08 atqa=0400 bcc=07 bcc_ok=1"
        ),
    }
    return data


# ── the wire format ──────────────────────────────────────────────────────────


def test_the_listing_is_parsed_exactly():
    results = parse_device_results(_listing())
    assert results.slots == 8
    assert results.used == 1
    assert results.newest_seq == 12
    assert len(results.results) == 1
    entry = results.results[0]
    assert entry.index == 0
    assert entry.seq == 12
    assert entry.ts_ms == 34567
    assert entry.uid == "AABBCCDD"
    assert entry.sectors == 16
    assert entry.read == 15
    assert entry.failed == 1
    assert entry.gaps == 1
    assert entry.uptime_s == pytest.approx(34.567)


def test_the_listing_is_newest_first():
    results = parse_device_results(
        _listing(
            (
                (0, 12, 34567, "AABBCCDD", 16, 15, 1),
                (1, 11, 20000, "11223344", 16, 16, 0),
            )
        )
    )
    assert [r.seq for r in results.results] == [12, 11]
    assert results.used == 2


def test_a_malformed_token_is_ignored_not_guessed():
    data = _listing()
    data["mifare_result0"] = "seq=12 ts=abc uid= sectors=x read=1 failed=0"
    results = parse_device_results(data)
    entry = results.results[0]
    assert entry.seq == 12
    assert entry.ts_ms == 0
    assert entry.uid is None
    assert entry.sectors == 0
    assert entry.read == 1


def test_an_empty_listing_is_empty():
    results = parse_device_results({"mifare_results_stat": "slots=8 used=0 seq=0"})
    assert results.empty
    assert results.used == 0
    assert results.slots == 8


# ── the round-trips ──────────────────────────────────────────────────────────


def test_the_host_asks_the_board_for_its_results():
    link = ResultsLink(listing=_listing())
    results = run_device_results(link)
    assert "mifare results" in link.sent
    assert results.results[0].uid == "AABBCCDD"


def test_an_older_firmware_is_reported_as_unsupported():
    link = FakeLink(responses={"mifare results": err("unknown mifare subcommand")})
    with pytest.raises(DeviceResultsUnsupported):
        run_device_results(link)


def test_a_refusal_is_reported_as_an_error():
    link = FakeLink(
        responses={"mifare results": err("busy - the card workflow is running")}
    )
    with pytest.raises(DeviceResultsError):
        run_device_results(link)


def test_a_stored_result_is_fetched_with_its_holes():
    link = ResultsLink(stored=_stored_result())
    stored = run_device_stored_result(link)
    assert "mifare results show 0" in link.sent
    assert stored.meta is not None
    assert stored.meta.uid == "AABBCCDD"
    assert stored.dump.read == 1
    assert stored.dump.failed == 1
    assert stored.dump.total == 2
    assert stored.dump.sectors[0].data_hex == _sector_hex(0)
    # The hole keeps its reason: it is never reported as read.
    assert stored.dump.sectors[1].data_hex is None
    assert stored.dump.sectors[1].reason == "nokey"


def test_a_missing_stored_result_says_so():
    link = ResultsLink(stored={})
    with pytest.raises(DeviceResultsError):
        run_device_stored_result(link)


def test_clear_asks_the_board_to_erase():
    link = ResultsLink(listing=_listing())
    run_device_results_clear(link)
    assert "mifare results clear" in link.sent


def test_clear_on_an_old_firmware_is_unsupported():
    link = FakeLink(
        responses={"mifare results clear": err("unknown mifare subcommand")}
    )
    with pytest.raises(DeviceResultsUnsupported):
        run_device_results_clear(link)


def test_clear_reports_a_flash_failure():
    link = FakeLink(
        responses={"mifare results clear": err("could not erase the stored results")}
    )
    with pytest.raises(DeviceResultsError):
        run_device_results_clear(link)


# ── the CLI ──────────────────────────────────────────────────────────────────


def test_the_results_command_lists_what_the_board_kept(runner, use_link):
    link = use_link(mifare_session, ResultsLink(listing=_listing()))
    result = runner.invoke(mifare_results_cmd, [])
    assert result.exit_code == 0
    out = flat(result.stdout)
    assert "stored results" in out
    assert "AABBCCDD" in out
    assert "15/16" in out
    assert "mifare results" in link.sent


def test_the_results_command_says_when_there_is_nothing(runner, use_link):
    link = use_link(
        mifare_session,
        ResultsLink(listing={"mifare_results_stat": "slots=8 used=0 seq=0"}),
    )
    result = runner.invoke(mifare_results_cmd, [])
    assert result.exit_code == 0
    assert "no stored results" in flat(result.stdout)


def test_the_results_command_emits_json(runner, use_link):
    use_link(mifare_session, ResultsLink(listing=_listing()))
    result = runner.invoke(mifare_results_cmd, ["--json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["used"] == 1
    assert doc["results"][0]["uid"] == "AABBCCDD"
    assert doc["results"][0]["failed"] == 1


def test_the_results_command_clears_the_board(runner, use_link):
    link = use_link(mifare_session, ResultsLink(listing=_listing()))
    result = runner.invoke(mifare_results_cmd, ["--clear"])
    assert result.exit_code == 0
    assert "mifare results clear" in link.sent
    assert "erased" in flat(result.stdout)


def test_the_results_command_clear_emits_json(runner, use_link):
    use_link(mifare_session, ResultsLink(listing=_listing()))
    result = runner.invoke(mifare_results_cmd, ["--clear", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"cleared": True}


def test_the_results_command_refuses_an_old_firmware(runner, use_link):
    use_link(
        mifare_session,
        FakeLink(responses={"mifare results": err("unknown mifare subcommand")}),
    )
    result = runner.invoke(mifare_results_cmd, [])
    assert result.exit_code == 1
    assert "does not keep results" in flat(result.output)


# ── the dump of a stored result ──────────────────────────────────────────────


def test_dump_last_rebuilds_the_canonical_dump(runner, use_link, tmp_path):
    link = use_link(mifare_session, ResultsLink(stored=_stored_result()))
    out_file = tmp_path / "card.json"
    result = runner.invoke(mifare_dump_cmd, ["--last", "--out", str(out_file)])
    assert result.exit_code == 1  # the card had a gap: the dump is incomplete
    assert "mifare results show 0" in link.sent
    dump = json.loads(out_file.read_text())
    assert dump["uid"] == "DECAFBAD"
    assert dump["sectors_total"] == 2
    assert dump["sectors_read"] == 1
    assert dump["source_keyfile"] == "device-standalone"
    # The hole is kept, with its reason.
    assert dump["failed_sectors"] == [{"sector": 1, "reason": "no key available"}]
    # The board has no real-time clock: its timestamp is its own uptime.
    assert dump["device_uptime_s"] == pytest.approx(34.567)
    assert dump["device_seq"] == 12


def test_dump_last_refuses_a_keys_file(runner, use_link, tmp_path):
    keys = tmp_path / "keys.txt"
    keys.write_text("0:FFFFFFFFFFFF:FFFFFFFFFFFF\n")
    use_link(mifare_session, ResultsLink(stored=_stored_result()))
    result = runner.invoke(
        mifare_dump_cmd,
        ["--last", "--keys-file", str(keys), "--out", str(tmp_path / "x.json")],
    )
    assert result.exit_code == 1
    assert "cannot be combined" in flat(result.output)


def test_the_stored_dump_can_be_analyzed_offline(runner, use_link, tmp_path):
    """The point of task 4.6: the standalone result gets the same honest
    report as a card read with the PC attached."""
    use_link(mifare_session, ResultsLink(stored=_stored_result()))
    out_file = tmp_path / "card.json"
    runner.invoke(mifare_dump_cmd, ["--last", "--out", str(out_file)])
    dump = json.loads(out_file.read_text())

    report = analyze_dump(dump, default_keys=set())
    # The sector that was never opened is DECLARED, never called secure.
    assert [g["sector"] for g in report["gaps"]] == [1]
    assert report["summary"]["gap_sectors"] == [1]
    assert report["sectors_analyzed"] == 1
    assert report["sectors_total"] == 2
