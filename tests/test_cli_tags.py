#!/usr/bin/env python3

# Electronic Cats
# test_cli_tags.py — `bombercat tags read|watch` (modules/tags/cli.py).
# Same pattern as test_cli_relay.py: CliRunner + FakeLink, driving each
# command's real logic against a scripted `stream()`.
# docs/CLI_IMPROVEMENTS_DetectTags.md §3.1-3.2, §7 (Phase 2).

import csv
import json

import pytest

from conftest import FakeLink, flat, ok
from modules.tags import cli as tagscli
from modules.tags.cli import info_cmd, read_cmd, scan_cmd, tags, watch_cmd

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
    runner.mix_stderr = False
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

    runner.mix_stderr = False
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


def test_watch_caps_the_dedupe_table_with_a_warning(runner, use_link, monkeypatch):
    """Firmware that prints no UID keys the dedupe table by tech:protocol:ts_ms,
    which never repeats — cap growth instead of leaking memory forever (M15)."""
    monkeypatch.setattr(tagscli, "_MAX_DEDUPE_KEYS", 2)

    def _lines():
        yield ":tag 1 NFC-A T2T 000001"
        yield ":tag 2 NFC-A T2T 000002"
        yield ":tag 3 NFC-A T2T 000003"
        raise KeyboardInterrupt

    use_link(tagscli, FakeLink(stream_lines=_lines()))
    result = runner.invoke(watch_cmd, [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "dedupe table capped at 2" in out


def test_tag_dict_renames_extra_keys_that_collide_with_computed_columns(
    runner, use_link
):
    """A device sending `count=`/`uid=` in extra must not corrupt the real
    computed columns of the same name (M13)."""
    use_link(
        tagscli,
        FakeLink(stream_lines=[":tag 10 NFC-A T2T 041A2B count=99 uid=DEADBEEF"]),
    )
    result = runner.invoke(read_cmd, ["--json"])
    payload = json.loads(result.stdout)

    assert payload["uid"] == "041A2B"
    assert payload["x_count"] == "99"
    assert payload["x_uid"] == "DEADBEEF"


def test_watch_json_emits_newline_delimited_objects(runner, use_link):
    def _lines():
        yield STRUCTURED_LINE
        raise KeyboardInterrupt

    use_link(tagscli, FakeLink(stream_lines=_lines()))
    result = runner.invoke(watch_cmd, ["--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout.strip().splitlines()[0])
    assert payload["uid"] == "041A2B3C"


# ── scan ─────────────────────────────────────────────────────────────────────


def test_scan_aggregates_repeats_and_prints_summary(runner, use_link):
    def _lines():
        yield STRUCTURED_LINE
        yield STRUCTURED_LINE
        yield ":tag 2000 NFC-A MIFARE A3912200"

    use_link(tagscli, FakeLink(stream_lines=_lines()))
    result = runner.invoke(scan_cmd, ["-t", "0.05"])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "3 detections, 2 unique tags" in out
    assert "04:1A:2B:3C" in out and "A3:91:22:00" in out


def test_scan_reports_no_tags_detected(runner, use_link):
    use_link(tagscli, FakeLink(stream_lines=[]))
    result = runner.invoke(scan_cmd, ["-t", "0.01"])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "0 detections, 0 unique tags" in out
    assert "no tags detected" in out


def test_scan_writes_json_and_csv_exports(runner, use_link, tmp_path):
    json_path = tmp_path / "scan.json"
    csv_path = tmp_path / "scan.csv"

    def _lines():
        yield STRUCTURED_LINE
        yield STRUCTURED_LINE

    use_link(tagscli, FakeLink(stream_lines=_lines()))
    result = runner.invoke(
        scan_cmd, ["-t", "0.05", "--json", str(json_path), "--csv", str(csv_path)]
    )

    assert result.exit_code == 0

    payload = json.loads(json_path.read_text())
    assert payload == [
        {
            "uid": "041A2B3C",
            "tech": "NFC-A",
            "protocol": "T2T",
            "count": 2,
            "first_s": payload[0]["first_s"],
            "last_s": payload[0]["last_s"],
        }
    ]

    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["uid"] == "041A2B3C"
    assert rows[0]["count"] == "2"


def test_scan_refuses_to_overwrite_an_existing_export_without_force(
    runner, use_link, tmp_path
):
    json_path = tmp_path / "scan.json"
    json_path.write_text("existing")
    use_link(tagscli, FakeLink(stream_lines=[STRUCTURED_LINE]))
    result = runner.invoke(scan_cmd, ["-t", "0.01", "--json", str(json_path)])

    assert result.exit_code == 1
    assert "already exists" in flat(result.stdout)
    assert json_path.read_text() == "existing"


def test_scan_force_overwrites_an_existing_export(runner, use_link, tmp_path):
    json_path = tmp_path / "scan.json"
    json_path.write_text("existing")
    use_link(tagscli, FakeLink(stream_lines=[STRUCTURED_LINE]))
    result = runner.invoke(
        scan_cmd, ["-t", "0.01", "--json", str(json_path), "--force"]
    )

    assert result.exit_code == 0
    assert json_path.read_text() != "existing"


def test_scan_reports_a_write_failure_instead_of_crashing(
    runner, use_link, tmp_path, monkeypatch
):
    use_link(tagscli, FakeLink(stream_lines=[STRUCTURED_LINE]))
    monkeypatch.setattr(
        tagscli,
        "_write_json",
        lambda path, rows: (_ for _ in ()).throw(OSError("disk full")),
    )
    result = runner.invoke(
        scan_cmd, ["-t", "0.01", "--json", str(tmp_path / "scan.json")]
    )

    assert result.exit_code == 1
    assert "could not write" in flat(result.stdout)


def test_scan_reports_a_board_that_will_not_handshake(runner, use_link):
    link = use_link(tagscli, FakeLink(ping_ok=False))
    result = runner.invoke(scan_cmd, ["-t", "0.01"])

    assert result.exit_code == 1
    assert "did not answer the handshake" in flat(result.stdout)
    assert link.closed


# ── info ─────────────────────────────────────────────────────────────────────


def test_info_reports_structured_mode(runner, use_link):
    use_link(
        tagscli,
        FakeLink(
            responses={"info": ok(fw="1.2.0", state="idle")},
            stream_lines=[STRUCTURED_LINE],
        ),
    )
    result = runner.invoke(info_cmd, [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "1.2.0" in out
    assert "structured" in out
    assert "idle" in out


def test_info_falls_back_to_legacy_mode_with_no_tag_events(runner, use_link):
    use_link(
        tagscli,
        FakeLink(responses={"info": ok(fw="1.0.0", state="idle")}, stream_lines=[]),
    )
    result = runner.invoke(info_cmd, [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "1.0.0" in out
    assert "legacy text" in out
    assert "reflash for exact parsing" in out


def test_info_reports_a_device_error_from_the_info_command(runner, use_link):
    from conftest import err

    use_link(tagscli, FakeLink(responses={"info": err("unknown command")}))
    result = runner.invoke(info_cmd, [])

    assert result.exit_code == 1
    assert "info failed" in flat(result.stdout)


def test_info_reports_a_board_that_will_not_handshake(runner, use_link):
    link = use_link(tagscli, FakeLink(ping_ok=False))
    result = runner.invoke(info_cmd, [])

    assert result.exit_code == 1
    assert "did not answer the handshake" in flat(result.stdout)
    assert link.closed


# ── group wiring ─────────────────────────────────────────────────────────────


def test_tags_group_exposes_all_subcommands():
    assert set(tags.commands) == {"read", "watch", "scan", "info"}
