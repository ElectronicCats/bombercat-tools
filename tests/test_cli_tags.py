#!/usr/bin/env python3

# Electronic Cats
# test_cli_tags.py — `bombercat tags read|watch` (modules/tags/cli.py).
# Same pattern as test_cli_relay.py: CliRunner + FakeLink, driving each
# command's real logic against a scripted `stream()`.
# docs/CLI_IMPROVEMENTS_DetectTags.md §3.1-3.2, §7 (Phase 2).

import csv
import json

import pytest

from conftest import FakeLink, err, flat, ok
from modules.tags import cli as tagscli
from modules.tags.cli import info_cmd, read_cmd, scan_cmd, tags, watch_cmd
from modules.tags.cli import (
    mifare_auth_cmd,
    mifare_check_cmd,
    mifare_keys_cmd,
    mifare_read_cmd,
    mifare_sector_cmd,
    mifare_write_cmd,
)

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
    assert "no tag detected" in flat(result.output)


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
    assert "did not answer the handshake" in flat(result.output)
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
    out = flat(result.output)

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
        scan_cmd,
        ["-t", "0.05", "--json-out", str(json_path), "--csv-out", str(csv_path)],
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


def test_scan_json_is_a_deprecated_alias_for_json_out(runner, use_link, tmp_path):
    """L4: `scan --json FILE` used to collide in meaning with the boolean
    `--json` flag on `read`/`watch`. It must keep working as a hidden alias
    for `--json-out`, with a one-line deprecation warning."""
    json_path = tmp_path / "scan.json"
    use_link(tagscli, FakeLink(stream_lines=[STRUCTURED_LINE]))

    result = runner.invoke(scan_cmd, ["-t", "0.01", "--json", str(json_path)])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "deprecated" in out and "--json-out" in out
    assert json.loads(json_path.read_text())


def test_scan_refuses_to_overwrite_an_existing_export_without_force(
    runner, use_link, tmp_path
):
    json_path = tmp_path / "scan.json"
    json_path.write_text("existing")
    use_link(tagscli, FakeLink(stream_lines=[STRUCTURED_LINE]))
    result = runner.invoke(scan_cmd, ["-t", "0.01", "--json-out", str(json_path)])

    assert result.exit_code == 1
    assert "already exists" in flat(result.output)
    assert json_path.read_text() == "existing"


def test_scan_force_overwrites_an_existing_export(runner, use_link, tmp_path):
    json_path = tmp_path / "scan.json"
    json_path.write_text("existing")
    use_link(tagscli, FakeLink(stream_lines=[STRUCTURED_LINE]))
    result = runner.invoke(
        scan_cmd, ["-t", "0.01", "--json-out", str(json_path), "--force"]
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
        scan_cmd, ["-t", "0.01", "--json-out", str(tmp_path / "scan.json")]
    )

    assert result.exit_code == 1
    assert "could not write" in flat(result.output)


def test_scan_csv_export_neutralizes_formula_injection_in_device_fields(
    runner, use_link, tmp_path
):
    """M29: tech/protocol/extra are device-controlled free text (parser.py
    imposes no charset beyond \\S+). A value starting with =/+/-/@ is a live
    formula-injection payload for Excel/LibreOffice, so the CSV writer must
    neutralize it; the JSON export (never opened as a spreadsheet) must stay
    untouched, and the shared `rows` list must not be mutated in the process.
    """
    json_path = tmp_path / "scan.json"
    csv_path = tmp_path / "scan.csv"
    line = ":tag 1234 =CMD -PROTO 041A2B3C note=+INJECT"

    use_link(tagscli, FakeLink(stream_lines=[line]))
    result = runner.invoke(
        scan_cmd,
        ["-t", "0.05", "--json-out", str(json_path), "--csv-out", str(csv_path)],
    )

    assert result.exit_code == 0

    payload = json.loads(json_path.read_text())
    assert payload[0]["tech"] == "=CMD"
    assert payload[0]["protocol"] == "-PROTO"
    assert payload[0]["note"] == "+INJECT"

    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["tech"] == "'=CMD"
    assert rows[0]["protocol"] == "'-PROTO"
    assert rows[0]["note"] == "'+INJECT"
    assert rows[0]["uid"] == "041A2B3C"  # hex UID, never touched


def test_scan_reports_a_board_that_will_not_handshake(runner, use_link):
    link = use_link(tagscli, FakeLink(ping_ok=False))
    result = runner.invoke(scan_cmd, ["-t", "0.01"])

    assert result.exit_code == 1
    assert "did not answer the handshake" in flat(result.output)
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
    assert "info failed" in flat(result.output)


def test_info_reports_a_board_that_will_not_handshake(runner, use_link):
    link = use_link(tagscli, FakeLink(ping_ok=False))
    result = runner.invoke(info_cmd, [])

    assert result.exit_code == 1
    assert "did not answer the handshake" in flat(result.output)
    assert link.closed


# ── mifare ───────────────────────────────────────────────────────────────────

_MIFARE_KEYS_LINE = ok(
    mifare_key0="default_ff FFFFFFFFFFFF",
    mifare_key1="default_00 000000000000",
    mifare_key2="default_a0a1a2 A0A1A2A3A4A5",
)


def test_mifare_auth_succeeds_when_a_session_is_already_open(runner, use_link):
    fake = use_link(tagscli, FakeLink())
    result = runner.invoke(
        mifare_auth_cmd, ["--block", "4", "--key-type", "A", "--key", "FFFFFFFFFFFF"]
    )

    assert result.exit_code == 0
    assert "authenticated block 4" in flat(result.stdout)
    assert "mifare auth 4 A FFFFFFFFFFFF" in fake.sent


def test_mifare_auth_rejects_a_malformed_key_locally(runner, use_link):
    fake = use_link(tagscli, FakeLink())
    result = runner.invoke(
        mifare_auth_cmd, ["--block", "4", "--key-type", "A", "--key", "ZZ"]
    )

    assert result.exit_code == 1
    assert "12 hex characters" in flat(result.output)
    assert fake.sent == []  # never reached the wire


def test_mifare_auth_waits_for_a_tap_when_no_card_is_selected(runner, use_link):
    fake = use_link(
        tagscli,
        FakeLink(
            responses={
                # First attempt: no session yet. After the tap event arrives,
                # the retry succeeds (FakeLink._resolve pops a list in order).
                "mifare auth 4 A FFFFFFFFFFFF": [
                    err("no card selected, tap a Mifare Classic card first"),
                    ok(),
                ]
            },
            stream_lines=[
                ":mifare 1000 041A2B3C 4 00112233445566778899AABBCCDDEEFF ok"
            ],
        ),
    )
    result = runner.invoke(
        mifare_auth_cmd, ["--block", "4", "--key-type", "A", "--key", "FFFFFFFFFFFF"]
    )

    assert result.exit_code == 0
    assert "tap a Mifare Classic card" in flat(result.output)
    # sent twice: the initial attempt, then the retry once the tap was seen
    assert fake.sent.count("mifare auth 4 A FFFFFFFFFFFF") == 2


def test_mifare_auth_reports_a_timeout_waiting_for_a_tap(runner, use_link):
    use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare auth 4 A FFFFFFFFFFFF": err(
                    "no card selected, tap a Mifare Classic card first"
                )
            },
            stream_lines=[],
        ),
    )
    result = runner.invoke(
        mifare_auth_cmd,
        ["--block", "4", "--key-type", "A", "--key", "FFFFFFFFFFFF", "-t", "0.01"],
    )

    assert result.exit_code == 1
    assert "no card selected" in flat(result.output)


def test_mifare_read_reports_the_block_data(runner, use_link):
    use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare read 4": ok(mifare_data="4 00112233445566778899AABBCCDDEEFF")
            }
        ),
    )
    result = runner.invoke(mifare_read_cmd, ["--block", "4"])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "00112233445566778899AABBCCDDEEFF" in out


def test_mifare_read_json_emits_block_and_data(runner, use_link):
    use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare read 4": ok(mifare_data="4 00112233445566778899AABBCCDDEEFF")
            }
        ),
    )
    result = runner.invoke(mifare_read_cmd, ["--block", "4", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload == {"block": 4, "data": "00112233445566778899AABBCCDDEEFF"}


def test_mifare_write_rejects_a_malformed_data_length_locally(runner, use_link):
    fake = use_link(tagscli, FakeLink())
    result = runner.invoke(mifare_write_cmd, ["--block", "4", "--data", "AABB"])

    assert result.exit_code == 1
    assert "32 hex characters" in flat(result.output)
    assert fake.sent == []


def test_mifare_write_succeeds(runner, use_link):
    fake = use_link(tagscli, FakeLink())
    result = runner.invoke(
        mifare_write_cmd,
        ["--block", "4", "--data", "00112233445566778899AABBCCDDEEFF"],
    )

    assert result.exit_code == 0
    assert "wrote block 4" in flat(result.stdout)
    assert "mifare write 4 00112233445566778899AABBCCDDEEFF" in fake.sent


def test_mifare_sector_reports_the_sector_data(runner, use_link):
    use_link(
        tagscli,
        FakeLink(
            responses={"mifare sector 1 A FFFFFFFFFFFF": ok(mifare_sector="00" * 64)}
        ),
    )
    result = runner.invoke(
        mifare_sector_cmd, ["--sector", "1", "--key-type", "A", "--key", "FFFFFFFFFFFF"]
    )
    out = flat(result.stdout)

    assert result.exit_code == 0
    # sector 1 = blocks 4-7 — labeled with their real block numbers, not just
    # a position within the sector.
    assert "block 4" in out
    assert "block 5" in out
    assert "block 6" in out
    assert "block 7 (trailer)" in out
    assert "00" * 16 in out
    # trailer's access bits get decoded into per-block permissions too.
    assert "Access conditions" in out
    assert "read key A or B" in out


def test_mifare_sector_json_emits_sector_and_full_data(runner, use_link):
    use_link(
        tagscli,
        FakeLink(
            responses={"mifare sector 1 A FFFFFFFFFFFF": ok(mifare_sector="00" * 64)}
        ),
    )
    result = runner.invoke(
        mifare_sector_cmd,
        ["--sector", "1", "--key-type", "A", "--key", "FFFFFFFFFFFF", "--json"],
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload == {"sector": 1, "data": "00" * 64}


def test_mifare_keys_lists_every_default_key(runner, use_link):
    use_link(tagscli, FakeLink(responses={"mifare keys": _MIFARE_KEYS_LINE}))
    result = runner.invoke(mifare_keys_cmd, [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "default_ff" in out and "FFFFFFFFFFFF" in out
    assert "default_00" in out and "000000000000" in out
    assert "default_a0a1a2" in out and "A0A1A2A3A4A5" in out


def test_mifare_keys_json_emits_one_object_per_key(runner, use_link):
    use_link(tagscli, FakeLink(responses={"mifare keys": _MIFARE_KEYS_LINE}))
    result = runner.invoke(mifare_keys_cmd, ["--json"])
    lines = [json.loads(line) for line in result.stdout.strip().splitlines()]

    assert result.exit_code == 0
    assert lines == [
        {"name": "default_ff", "key": "FFFFFFFFFFFF"},
        {"name": "default_00", "key": "000000000000"},
        {"name": "default_a0a1a2", "key": "A0A1A2A3A4A5"},
    ]


def test_mifare_reports_a_board_that_will_not_handshake(runner, use_link):
    link = use_link(tagscli, FakeLink(ping_ok=False))
    result = runner.invoke(mifare_keys_cmd, [])

    assert result.exit_code == 1
    assert "did not answer the handshake" in flat(result.output)
    assert link.closed


# ── check (dictionary attack) ─────────────────────────────────────────────────


def test_mifare_check_continues_past_a_failed_key_and_finds_the_real_one(
    runner, use_link, tmp_path
):
    # Regression for the firmware HALT-after-failed-auth bug
    # (CLI_IMPROVEMENTS_MifareCheck.md §4): the correct key is NOT first in the
    # dictionary, so the host sweep must keep going after a "-ERR" instead of
    # giving up. FFFFFFFFFFFF fails on this sector; A0A1A2A3A4A5 (the MAD key)
    # opens it — exactly the "shows all keys failing" symptom before the fix.
    keyfile = tmp_path / "keys.keys"
    keyfile.write_text("FFFFFFFFFFFF\nA0A1A2A3A4A5\n")
    fake = use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare auth 0 A FFFFFFFFFFFF": err("authentication failed"),
                "mifare auth 0 A A0A1A2A3A4A5": ok(),
            }
        ),
    )
    result = runner.invoke(
        mifare_check_cmd,
        ["--keys", str(keyfile), "--sectors", "1", "--key-type", "A", "--json"],
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload == {
        "sectors": [{"sector": 0, "key_a": "A0A1A2A3A4A5", "key_b": None}],
        "recovered": 1,
        "total": 1,
    }
    # The sweep did not stop at the first failure: BOTH keys were tried, in
    # dictionary order.
    assert "mifare auth 0 A FFFFFFFFFFFF" in fake.sent
    assert "mifare auth 0 A A0A1A2A3A4A5" in fake.sent
    assert fake.sent.index("mifare auth 0 A FFFFFFFFFFFF") < fake.sent.index(
        "mifare auth 0 A A0A1A2A3A4A5"
    )


def test_mifare_check_tries_known_keys_first_on_later_sectors(
    runner, use_link, tmp_path
):
    # Once a key opens sector 0, known-keys-first tries it before the rest of
    # the dictionary on sector 1 (block 4) — the speed-up for real cards that
    # reuse one key across sectors. FFFFFFFFFFFF must never be sent for block 4.
    keyfile = tmp_path / "keys.keys"
    keyfile.write_text("FFFFFFFFFFFF\nA0A1A2A3A4A5\n")
    fake = use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare auth 0 A FFFFFFFFFFFF": err("authentication failed"),
                "mifare auth 0 A A0A1A2A3A4A5": ok(),
                "mifare auth 4 A A0A1A2A3A4A5": ok(),
                "mifare auth 4 A FFFFFFFFFFFF": err("authentication failed"),
            }
        ),
    )
    result = runner.invoke(
        mifare_check_cmd,
        ["--keys", str(keyfile), "--sectors", "2", "--key-type", "A", "--json"],
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["recovered"] == 2
    assert "mifare auth 4 A A0A1A2A3A4A5" in fake.sent
    assert "mifare auth 4 A FFFFFFFFFFFF" not in fake.sent


def test_mifare_check_table_reports_vulnerable_verdict(runner, use_link, tmp_path):
    # Both sectors open with the default key: the table shows the recovered
    # key in every cell and the verdict line reports 4/4 keys, 2/2 sectors.
    keyfile = tmp_path / "keys.keys"
    keyfile.write_text("FFFFFFFFFFFF\n")
    use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare auth 0 A FFFFFFFFFFFF": ok(),
                "mifare auth 0 B FFFFFFFFFFFF": ok(),
                "mifare auth 4 A FFFFFFFFFFFF": ok(),
                "mifare auth 4 B FFFFFFFFFFFF": ok(),
            }
        ),
    )
    result = runner.invoke(mifare_check_cmd, ["--keys", str(keyfile), "--sectors", "2"])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert out.count("FFFFFFFFFFFF") == 4
    assert "4/4 keys recovered" in out
    assert "card exposes 2/2 sectors with known keys" in out


def test_mifare_check_reports_unknown_keys_and_fails(runner, use_link, tmp_path):
    # No key in the dictionary opens the card: the table shows [unknown] for
    # every cell and the command exits non-zero (not fully recovered).
    keyfile = tmp_path / "keys.keys"
    keyfile.write_text("FFFFFFFFFFFF\n")
    use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare auth 0 A FFFFFFFFFFFF": err("authentication failed"),
                "mifare auth 0 B FFFFFFFFFFFF": err("authentication failed"),
            }
        ),
    )
    result = runner.invoke(mifare_check_cmd, ["--keys", str(keyfile), "--sectors", "1"])
    out = flat(result.stdout)

    assert result.exit_code == 1
    assert "[unknown]" in out
    assert "0/2 keys recovered" in out


def test_mifare_check_key_type_a_never_tries_b(runner, use_link, tmp_path):
    keyfile = tmp_path / "keys.keys"
    keyfile.write_text("FFFFFFFFFFFF\n")
    fake = use_link(
        tagscli,
        FakeLink(responses={"mifare auth 0 A FFFFFFFFFFFF": ok()}),
    )
    result = runner.invoke(
        mifare_check_cmd,
        ["--keys", str(keyfile), "--sectors", "1", "--key-type", "A", "--json"],
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload == {
        "sectors": [{"sector": 0, "key_a": "FFFFFFFFFFFF", "key_b": None}],
        "recovered": 1,
        "total": 1,
    }
    assert not any(" B " in s for s in fake.sent)


# ── check: trailer-read key-B recovery ────────────────────────────────────────
# When the dictionary opens key A but not key B, `check` reads the sector
# trailer with key A; on cards whose access bits leave key B readable (the
# transport/default config), key B comes back in cleartext. NOT the Crypto-1
# nested attack — the PN7150 runs Crypto-1 in-chip and never surfaces a nonce.


# blocks 0-2 zeroed, then the trailer: key A reads back as zeros, access bytes
# FF0780 (trailer config 001 → key B readable with key A) + GPB 69, key B.
_TRAILER_KEYB_READABLE = "00" * 48 + "000000000000" "FF078069" "B0B1B2B3B4B5"


def test_mifare_check_recovers_key_b_via_trailer_read(runner, use_link, tmp_path):
    keyfile = tmp_path / "keys.keys"
    keyfile.write_text("FFFFFFFFFFFF\n")
    fake = use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare auth 0 A FFFFFFFFFFFF": ok(),
                "mifare auth 0 B FFFFFFFFFFFF": err("authentication failed"),
                "mifare sector 0 A FFFFFFFFFFFF": ok(
                    mifare_sector=_TRAILER_KEYB_READABLE
                ),
            }
        ),
    )
    result = runner.invoke(
        mifare_check_cmd, ["--keys", str(keyfile), "--sectors", "1", "--json"]
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload == {
        "sectors": [{"sector": 0, "key_a": "FFFFFFFFFFFF", "key_b": "B0B1B2B3B4B5"}],
        "recovered": 2,
        "total": 2,
    }
    # It read the trailer with the recovered key A.
    assert "mifare sector 0 A FFFFFFFFFFFF" in fake.sent


def test_mifare_check_trailer_read_prints_recovery_line(runner, use_link, tmp_path):
    keyfile = tmp_path / "keys.keys"
    keyfile.write_text("FFFFFFFFFFFF\n")
    use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare auth 0 A FFFFFFFFFFFF": ok(),
                "mifare auth 0 B FFFFFFFFFFFF": err("authentication failed"),
                "mifare sector 0 A FFFFFFFFFFFF": ok(
                    mifare_sector=_TRAILER_KEYB_READABLE
                ),
            }
        ),
    )
    result = runner.invoke(mifare_check_cmd, ["--keys", str(keyfile), "--sectors", "1"])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "key B recovered via trailer read" in out
    assert "B0B1B2B3B4B5" in out


def test_mifare_check_trailer_read_skips_when_key_b_not_exposed(
    runner, use_link, tmp_path
):
    # The card returns an all-zero trailer (key B not readable): recovery
    # finds nothing and key B stays unknown, so the command still fails.
    keyfile = tmp_path / "keys.keys"
    keyfile.write_text("FFFFFFFFFFFF\n")
    use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare auth 0 A FFFFFFFFFFFF": ok(),
                "mifare auth 0 B FFFFFFFFFFFF": err("authentication failed"),
                "mifare sector 0 A FFFFFFFFFFFF": ok(mifare_sector="00" * 64),
            }
        ),
    )
    result = runner.invoke(
        mifare_check_cmd, ["--keys", str(keyfile), "--sectors", "1", "--json"]
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 1
    assert payload["sectors"] == [{"sector": 0, "key_a": "FFFFFFFFFFFF", "key_b": None}]


def test_mifare_check_trailer_read_reports_protected_key_b(runner, use_link, tmp_path):
    # The card's access bits protect key B (008088 -> key_b_read = never), the
    # secure configuration. Recovery can't read it and says so plainly, so the
    # user can tell "the card protects it" from "the read failed". This is the
    # real limit — the Crypto-1 nested attack the PN7150 can't do would be the
    # only way to get such a key.
    keyfile = tmp_path / "keys.keys"
    keyfile.write_text("FFFFFFFFFFFF\n")
    # key B bytes are present in the dump but unreachable per the access bits.
    protected = "00" * 48 + "000000000000" "008088" "00" "B0B1B2B3B4B5"
    use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare auth 0 A FFFFFFFFFFFF": ok(),
                "mifare auth 0 B FFFFFFFFFFFF": err("authentication failed"),
                "mifare sector 0 A FFFFFFFFFFFF": ok(mifare_sector=protected),
            }
        ),
    )
    result = runner.invoke(mifare_check_cmd, ["--keys", str(keyfile), "--sectors", "1"])
    out = flat(result.stdout)

    assert result.exit_code == 1
    assert "key B not recovered" in out
    assert "protected by the access bits" in out
    # the protected key B was NOT leaked into the output
    assert "B0B1B2B3B4B5" not in out


def test_mifare_check_trailer_read_not_attempted_for_key_type_a(
    runner, use_link, tmp_path
):
    # key-type A only never looks for (or recovers) key B, so no trailer read.
    keyfile = tmp_path / "keys.keys"
    keyfile.write_text("FFFFFFFFFFFF\n")
    fake = use_link(
        tagscli,
        FakeLink(responses={"mifare auth 0 A FFFFFFFFFFFF": ok()}),
    )
    result = runner.invoke(
        mifare_check_cmd,
        ["--keys", str(keyfile), "--sectors", "1", "--key-type", "A", "--json"],
    )

    assert result.exit_code == 0
    assert not any(s.startswith("mifare sector") for s in fake.sent)


# ── group wiring ─────────────────────────────────────────────────────────────


def test_tags_group_exposes_all_subcommands():
    assert set(tags.commands) == {"read", "watch", "scan", "info", "mifare"}


def test_mifare_group_exposes_all_subcommands():
    assert set(tagscli.mifare.commands) == {
        "auth",
        "read",
        "write",
        "sector",
        "keys",
        "check",
    }


# ── key persistence: check --output-keys / sector --keys-file ─────────────────
# docs/CLI_IMPROVEMENTS_MifareCheck.md — `check` writes a `sector:keyA:keyB`
# file that `sector` reads back to auth and to show the real keys in the trailer.


def test_mifare_check_output_keys_writes_sector_lines_and_still_shows_table(
    runner, use_link, tmp_path
):
    keyfile = tmp_path / "dict.keys"
    keyfile.write_text("FFFFFFFFFFFF\n")
    out = tmp_path / "claves.txt"
    # FakeLink answers unscripted commands ok, so spell out the one failure:
    # sector 1's key B never opens and must land as a blank in the file.
    use_link(
        tagscli,
        FakeLink(
            responses={"mifare auth 4 B FFFFFFFFFFFF": err("authentication failed")}
        ),
    )
    result = runner.invoke(
        mifare_check_cmd,
        ["--keys", str(keyfile), "--sectors", "2", "--output-keys", str(out)],
    )

    assert result.exit_code == 1  # sector 1 key B never recovered
    # the on-screen table is still printed alongside the file write
    assert "MifareClassic check" in flat(result.stdout)
    assert f"wrote {out}" in flat(result.stdout)
    # blank for the key type that wasn't recovered, always one line per sector
    assert out.read_text() == ("0:FFFFFFFFFFFF:FFFFFFFFFFFF\n" "1:FFFFFFFFFFFF:\n")


def test_mifare_check_output_keys_refuses_overwrite_without_force(
    runner, use_link, tmp_path
):
    keyfile = tmp_path / "dict.keys"
    keyfile.write_text("FFFFFFFFFFFF\n")
    out = tmp_path / "claves.txt"
    out.write_text("stale\n")
    use_link(tagscli, FakeLink(responses={"mifare auth 0 A FFFFFFFFFFFF": ok()}))
    result = runner.invoke(
        mifare_check_cmd,
        ["--keys", str(keyfile), "--sectors", "1", "--output-keys", str(out)],
    )

    assert result.exit_code != 0
    assert out.read_text() == "stale\n"  # untouched


def test_mifare_sector_keys_file_auths_with_key_a_and_shows_real_keys(
    runner, use_link, tmp_path
):
    keys = tmp_path / "claves.txt"
    keys.write_text("0:A0A1A2A3A4A5:787788C10203\n")
    # trailer reads back as zeros over the wire; --keys-file substitutes reals
    data = "1092289339880400C08E1E9841205212" + "00" * 16 + "00" * 16
    fake = use_link(
        tagscli,
        FakeLink(responses={"mifare sector 0 A A0A1A2A3A4A5": ok(mifare_sector=data)}),
    )
    result = runner.invoke(
        mifare_sector_cmd, ["--sector", "0", "--keys-file", str(keys)]
    )
    flat_out = result.stdout.replace("\n", "")

    assert result.exit_code == 0
    # authenticated with the file's key A, not zeros
    assert "mifare sector 0 A A0A1A2A3A4A5" in fake.sent
    # both real keys appear in the trailer line, not the zeros the card returned
    assert "A0A1A2A3A4A5" in flat_out
    assert "787788C10203" in flat_out


def test_mifare_sector_keys_file_falls_back_to_key_b_when_a_fails(
    runner, use_link, tmp_path
):
    keys = tmp_path / "claves.txt"
    keys.write_text("0:A0A1A2A3A4A5:787788C10203\n")
    data = "00" * 64
    fake = use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare sector 0 A A0A1A2A3A4A5": err("authentication failed"),
                "mifare sector 0 B 787788C10203": ok(mifare_sector=data),
            }
        ),
    )
    result = runner.invoke(
        mifare_sector_cmd, ["--sector", "0", "--keys-file", str(keys)]
    )

    assert result.exit_code == 0
    assert "mifare sector 0 A A0A1A2A3A4A5" in fake.sent
    assert "mifare sector 0 B 787788C10203" in fake.sent


def test_mifare_sector_keys_file_errors_when_sector_missing(runner, use_link, tmp_path):
    keys = tmp_path / "claves.txt"
    keys.write_text("0:A0A1A2A3A4A5:787788C10203\n")
    use_link(tagscli, FakeLink())
    result = runner.invoke(
        mifare_sector_cmd, ["--sector", "5", "--keys-file", str(keys)]
    )

    assert result.exit_code == 1
    assert "sector 5 not found" in flat(result.output)


def test_mifare_sector_keys_file_errors_on_a_malformed_file(runner, use_link, tmp_path):
    keys = tmp_path / "claves.txt"
    keys.write_text("0:nothex:787788C10203\n")
    use_link(tagscli, FakeLink())
    result = runner.invoke(
        mifare_sector_cmd, ["--sector", "0", "--keys-file", str(keys)]
    )

    assert result.exit_code == 1
    assert "expected 'sector:keyA:keyB'" in flat(result.output)


def test_mifare_sector_rejects_both_key_and_keys_file(runner, use_link, tmp_path):
    keys = tmp_path / "claves.txt"
    keys.write_text("0:A0A1A2A3A4A5:787788C10203\n")
    use_link(tagscli, FakeLink())
    result = runner.invoke(
        mifare_sector_cmd,
        ["--sector", "0", "--key", "FFFFFFFFFFFF", "--keys-file", str(keys)],
    )

    assert result.exit_code != 0
    assert "either --key or --keys-file" in flat(result.output)
