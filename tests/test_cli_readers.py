#!/usr/bin/env python3

# Electronic Cats
# test_cli_readers.py — `bombercat readers read|watch|scan|info`
# (modules/readers/cli.py). Same pattern as test_cli_tags.py: CliRunner +
# FakeLink, driving each command's real logic against a scripted `stream()`.
# docs/IMPLEMENTATION_PLAN_DetectReaders_CLI.md §5 phase 4.

import csv
import json

import pytest

from conftest import FakeLink, flat, ok
from modules.readers import cli as readerscli
from modules.readers.cli import info_cmd, read_cmd, readers, scan_cmd, watch_cmd

PPSE_APDU = "00A4040007325041592E5359532E444446303100"
STRUCTURED_LINE = (
    f":reader 1234 NFC-A ISODEP intf=ISODEP apdu={PPSE_APDU} label=emv-payment n=3"
)

# A realistic wallet -> PPSE -> emv-payment session, with prose noise around it.
REALISTIC_SESSION_LINES = [
    "Waiting for a Reader ...",
    " - LISTEN MODE: Remote reader activated emulated card",
    "\tTechnology: NFC-A",
    "\tProtocol: ISODEP",
    "\tInterface: ISODEP",
    "\tAPDU[1/8] = " + PPSE_APDU,
    STRUCTURED_LINE,
    "Re-armed. Emulation discovery running.",
    "Waiting for a Reader ...",
]


# ── read ─────────────────────────────────────────────────────────────────────


def test_read_reports_a_detection(runner, use_link):
    use_link(readerscli, FakeLink(stream_lines=[STRUCTURED_LINE]))
    result = runner.invoke(read_cmd, [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "Reader detected" in out
    assert "emv-payment" in out
    assert "NFC-A" in out and "ISODEP" in out


def test_read_reports_a_detection_from_a_realistic_session(runner, use_link):
    use_link(readerscli, FakeLink(stream_lines=REALISTIC_SESSION_LINES))
    result = runner.invoke(read_cmd, [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "emv-payment" in out


def test_read_times_out_with_no_reader(runner, use_link):
    use_link(readerscli, FakeLink(stream_lines=[]))
    result = runner.invoke(read_cmd, ["-t", "0.01"])

    assert result.exit_code == 1
    assert "no reader detected" in flat(result.output)


def test_read_json_emits_one_clean_object_on_stdout(runner, use_link):
    use_link(readerscli, FakeLink(stream_lines=[STRUCTURED_LINE]))
    result = runner.invoke(read_cmd, ["--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["label"] == "emv-payment"
    assert payload["tech"] == "NFC-A"
    assert payload["protocol"] == "ISODEP"
    assert payload["ts_ms"] == 1234
    assert payload["n"] == 3


def test_read_reports_a_board_that_will_not_handshake(runner, use_link):
    link = use_link(readerscli, FakeLink(ping_ok=False))
    result = runner.invoke(read_cmd, [])

    assert result.exit_code == 1
    assert "did not answer the handshake" in flat(result.output)
    assert link.closed


def test_read_verbose_traces_to_stderr_and_keeps_stdout_clean_for_json(
    runner, use_link
):
    runner.mix_stderr = False
    use_link(readerscli, FakeLink(stream_lines=[STRUCTURED_LINE]))
    result = runner.invoke(read_cmd, ["--json", "-v"])

    assert result.exit_code == 0
    json.loads(result.stdout)  # still valid JSON: -v never leaked into stdout
    assert f"< {STRUCTURED_LINE}" in flat(result.stderr)


# ── watch ────────────────────────────────────────────────────────────────────


def test_watch_prints_each_detection_and_a_ctrl_c_summary(runner, use_link):
    other_line = ":reader 2000 NFC-A ISODEP label=visa aid=A0000000031010 n=1"

    def _lines():
        yield STRUCTURED_LINE
        yield other_line
        raise KeyboardInterrupt

    use_link(readerscli, FakeLink(stream_lines=_lines()))
    result = runner.invoke(watch_cmd, [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "emv-payment" in out and "visa" in out
    assert "2 detections, 2 unique fingerprints" in out


def test_watch_dedupe_collapses_repeats(runner, use_link):
    def _lines():
        yield STRUCTURED_LINE
        yield STRUCTURED_LINE
        yield STRUCTURED_LINE
        raise KeyboardInterrupt

    use_link(readerscli, FakeLink(stream_lines=_lines()))
    result = runner.invoke(watch_cmd, ["--dedupe"])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "seen again" in out
    assert "3 detections, 1 unique fingerprints" in out


def test_watch_hides_noise_by_default(runner, use_link):
    def _lines():
        yield "Waiting for a Reader ..."
        yield "Re-armed. Emulation discovery running."
        raise KeyboardInterrupt

    use_link(readerscli, FakeLink(stream_lines=_lines()))
    result = runner.invoke(watch_cmd, [])

    assert "Waiting for a Reader" not in result.stdout


def test_watch_shows_noise_with_no_quiet_noise(runner, use_link):
    def _lines():
        yield "Waiting for a Reader ..."
        raise KeyboardInterrupt

    use_link(readerscli, FakeLink(stream_lines=_lines()))
    result = runner.invoke(watch_cmd, ["--no-quiet-noise"])

    assert "Waiting for a Reader ..." in result.stdout


def test_watch_json_emits_newline_delimited_objects(runner, use_link):
    def _lines():
        yield STRUCTURED_LINE
        raise KeyboardInterrupt

    use_link(readerscli, FakeLink(stream_lines=_lines()))
    result = runner.invoke(watch_cmd, ["--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout.strip().splitlines()[0])
    assert payload["label"] == "emv-payment"


# ── scan ─────────────────────────────────────────────────────────────────────


def test_scan_aggregates_repeats_and_prints_summary(runner, use_link):
    other_line = ":reader 2000 NFC-A ISODEP label=visa aid=A0000000031010 n=1"

    def _lines():
        yield STRUCTURED_LINE
        yield STRUCTURED_LINE
        yield other_line

    use_link(readerscli, FakeLink(stream_lines=_lines()))
    result = runner.invoke(scan_cmd, ["-t", "0.05"])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "3 detections, 2 unique readers" in out
    assert "emv-payment" in out and "visa" in out


def test_scan_reports_no_readers_detected(runner, use_link):
    use_link(readerscli, FakeLink(stream_lines=[]))
    result = runner.invoke(scan_cmd, ["-t", "0.01"])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "0 detections, 0 unique readers" in out
    assert "no readers detected" in out


def test_scan_writes_json_and_csv_exports(runner, use_link, tmp_path):
    json_path = tmp_path / "scan.json"
    csv_path = tmp_path / "scan.csv"

    def _lines():
        yield STRUCTURED_LINE
        yield STRUCTURED_LINE

    use_link(readerscli, FakeLink(stream_lines=_lines()))
    result = runner.invoke(
        scan_cmd,
        ["-t", "0.05", "--json-out", str(json_path), "--csv-out", str(csv_path)],
    )

    assert result.exit_code == 0

    payload = json.loads(json_path.read_text())
    assert payload[0]["label"] == "emv-payment"
    assert payload[0]["count"] == 2

    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["label"] == "emv-payment"
    assert rows[0]["count"] == "2"


def test_scan_refuses_to_overwrite_an_existing_export_without_force(
    runner, use_link, tmp_path
):
    json_path = tmp_path / "scan.json"
    json_path.write_text("existing")
    use_link(readerscli, FakeLink(stream_lines=[STRUCTURED_LINE]))
    result = runner.invoke(scan_cmd, ["-t", "0.01", "--json-out", str(json_path)])

    assert result.exit_code == 1
    assert "already exists" in flat(result.output)
    assert json_path.read_text() == "existing"


def test_scan_force_overwrites_an_existing_export(runner, use_link, tmp_path):
    json_path = tmp_path / "scan.json"
    json_path.write_text("existing")
    use_link(readerscli, FakeLink(stream_lines=[STRUCTURED_LINE]))
    result = runner.invoke(
        scan_cmd, ["-t", "0.01", "--json-out", str(json_path), "--force"]
    )

    assert result.exit_code == 0
    assert json_path.read_text() != "existing"


def test_scan_reports_a_write_failure_instead_of_crashing(
    runner, use_link, tmp_path, monkeypatch
):
    use_link(readerscli, FakeLink(stream_lines=[STRUCTURED_LINE]))
    monkeypatch.setattr(
        readerscli,
        "_write_json",
        lambda path, rows: (_ for _ in ()).throw(OSError("disk full")),
    )
    result = runner.invoke(
        scan_cmd, ["-t", "0.01", "--json-out", str(tmp_path / "scan.json")]
    )

    assert result.exit_code == 1
    assert "could not write" in flat(result.output)


def test_scan_csv_export_neutralizes_formula_injection_in_device_fields(
    runner, use_link, tmp_path
):
    """tech/protocol/label/extra are device-controlled free text (parser.py
    imposes no charset beyond \\S+). A value starting with =/+/-/@ is a live
    formula-injection payload for Excel/LibreOffice, so the CSV writer must
    neutralize it; the JSON export must stay untouched."""
    json_path = tmp_path / "scan.json"
    csv_path = tmp_path / "scan.csv"
    line = ":reader 1234 =CMD -PROTO label=+INJECT n=1 note=@formula"

    use_link(readerscli, FakeLink(stream_lines=[line]))
    result = runner.invoke(
        scan_cmd,
        ["-t", "0.05", "--json-out", str(json_path), "--csv-out", str(csv_path)],
    )

    assert result.exit_code == 0

    payload = json.loads(json_path.read_text())
    assert payload[0]["tech"] == "=CMD"
    assert payload[0]["protocol"] == "-PROTO"
    assert payload[0]["label"] == "+INJECT"
    assert payload[0]["note"] == "@formula"

    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["tech"] == "'=CMD"
    assert rows[0]["protocol"] == "'-PROTO"
    assert rows[0]["label"] == "'+INJECT"
    assert rows[0]["note"] == "'@formula"


def test_scan_reports_a_board_that_will_not_handshake(runner, use_link):
    link = use_link(readerscli, FakeLink(ping_ok=False))
    result = runner.invoke(scan_cmd, ["-t", "0.01"])

    assert result.exit_code == 1
    assert "did not answer the handshake" in flat(result.output)
    assert link.closed


# ── info ─────────────────────────────────────────────────────────────────────


def test_info_reports_structured_events_seen(runner, use_link):
    use_link(
        readerscli,
        FakeLink(
            responses={"info": ok(fw="1.0.0", state="listening")},
            stream_lines=[STRUCTURED_LINE],
        ),
    )
    result = runner.invoke(info_cmd, [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "1.0.0" in out
    assert "structured" in out
    assert "listening" in out


def test_info_reports_no_events_seen_yet(runner, use_link):
    use_link(
        readerscli,
        FakeLink(
            responses={"info": ok(fw="1.0.0", state="listening")}, stream_lines=[]
        ),
    )
    result = runner.invoke(info_cmd, [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "1.0.0" in out
    assert "no ':reader' events seen yet" in out


def test_info_reports_a_device_error_from_the_info_command(runner, use_link):
    from conftest import err

    use_link(readerscli, FakeLink(responses={"info": err("unknown command")}))
    result = runner.invoke(info_cmd, [])

    assert result.exit_code == 1
    assert "info failed" in flat(result.output)


def test_info_reports_a_board_that_will_not_handshake(runner, use_link):
    link = use_link(readerscli, FakeLink(ping_ok=False))
    result = runner.invoke(info_cmd, [])

    assert result.exit_code == 1
    assert "did not answer the handshake" in flat(result.output)
    assert link.closed


# ── group wiring ─────────────────────────────────────────────────────────────


def test_readers_group_exposes_all_subcommands():
    assert set(readers.commands) == {"read", "watch", "scan", "info"}
