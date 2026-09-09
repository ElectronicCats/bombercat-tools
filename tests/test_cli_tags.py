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
    mifare_code_cmd,
    mifare_dump_cmd,
    mifare_keys_cmd,
    mifare_read_cmd,
    mifare_restore_cmd,
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
        "dump",
        "restore",
        "code",
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


# ── dump ─────────────────────────────────────────────────────────────────────
# docs/CLI_IMPROVEMENTS_MifareDump.md §7. `dump` reads every sector in one
# session; a sector with no usable key, or whose read fails, is recorded as a
# gap and never aborts the rest of the card. Reuses the same `sector:keyA:
# keyB` keyfile shape as `mifare sector`/`mifare check --output-keys`.

_DUMP_UID = "DEADBEEF"


def _dump_sector_hex(block1: str = "11" * 16, block2: str = "22" * 16) -> str:
    """A full 4-block sector (128 hex chars): block 0 carries `_DUMP_UID`,
    blocks 1-2 are arbitrary data, and the trailer's key bytes read back as
    zeros — exactly what a real card returns, so `--keys-file` substitution
    can be asserted against."""
    block0 = _DUMP_UID + "00" + "08" + "0400" + "00" * 8
    trailer = "00" * 6 + "FF078069" + "00" * 6
    return block0 + block1 + block2 + trailer


def test_mifare_dump_reads_all_sectors_into_canonical_json(runner, use_link, tmp_path):
    keys = tmp_path / "keys.txt"
    keys.write_text("0:A0A1A2A3A4A5:B0B1B2B3B4B5\n1:C0C1C2C3C4C5:D0D1D2D3D4D5\n")
    out = tmp_path / "dump.json"
    fake = use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare sector 0 A A0A1A2A3A4A5": ok(mifare_sector=_dump_sector_hex()),
                "mifare sector 1 A C0C1C2C3C4C5": ok(
                    mifare_sector=_dump_sector_hex("33" * 16, "44" * 16)
                ),
            }
        ),
    )
    result = runner.invoke(
        mifare_dump_cmd,
        ["--keys-file", str(keys), "--sectors", "2", "--out", str(out)],
    )

    assert result.exit_code == 0
    payload = json.loads(out.read_text())
    assert payload["uid"] == _DUMP_UID
    assert payload["sectors_read"] == 2
    assert payload["sectors_total"] == 2
    assert payload["failed_sectors"] == []
    assert payload["source_keyfile"] == str(keys)
    assert [s["sector"] for s in payload["sectors"]] == [0, 1]
    assert payload["sectors"][0]["opened_with"] == "A"
    # real keys substituted into the trailer, not the zeros the card returned
    assert payload["sectors"][0]["blocks"][3] == "A0A1A2A3A4A5FF078069B0B1B2B3B4B5"
    assert "mifare sector 0 A A0A1A2A3A4A5" in fake.sent
    assert "mifare sector 1 A C0C1C2C3C4C5" in fake.sent
    assert f"wrote {out}" in flat(result.stdout)


def test_mifare_dump_records_gap_for_sector_missing_from_keyfile(
    runner, use_link, tmp_path
):
    # sector 1 has no line in the keyfile at all -> gap, not an abort.
    keys = tmp_path / "keys.txt"
    keys.write_text("0:A0A1A2A3A4A5:\n")
    fake = use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare sector 0 A A0A1A2A3A4A5": ok(mifare_sector=_dump_sector_hex())
            }
        ),
    )
    result = runner.invoke(
        mifare_dump_cmd, ["--keys-file", str(keys), "--sectors", "2", "--json"]
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 1  # incomplete
    assert payload["sectors_read"] == 1
    assert payload["failed_sectors"] == [{"sector": 1, "reason": "no key available"}]
    assert not any(s.startswith("mifare sector 1") for s in fake.sent)


def test_mifare_dump_records_gap_when_a_sector_read_fails(runner, use_link, tmp_path):
    # only key A on file for sector 0, and it fails -> a gap, not an abort.
    keys = tmp_path / "keys.txt"
    keys.write_text("0:A0A1A2A3A4A5:\n")
    use_link(
        tagscli,
        FakeLink(
            responses={"mifare sector 0 A A0A1A2A3A4A5": err("authentication failed")}
        ),
    )
    result = runner.invoke(
        mifare_dump_cmd, ["--keys-file", str(keys), "--sectors", "1", "--json"]
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 1
    assert payload["sectors_read"] == 0
    assert payload["failed_sectors"] == [
        {"sector": 0, "reason": "authentication failed"}
    ]


def test_mifare_dump_missing_keyfile_fails_cleanly(runner, tmp_path):
    result = runner.invoke(
        mifare_dump_cmd,
        ["--keys-file", str(tmp_path / "missing.txt"), "--sectors", "1"],
    )

    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_mifare_dump_malformed_keyfile_fails_cleanly(runner, use_link, tmp_path):
    keys = tmp_path / "keys.txt"
    keys.write_text("0:nothex:B0B1B2B3B4B5\n")
    use_link(tagscli, FakeLink())
    result = runner.invoke(
        mifare_dump_cmd, ["--keys-file", str(keys), "--sectors", "1"]
    )

    assert result.exit_code == 1
    assert "expected 'sector:keyA:keyB'" in flat(result.output)
    assert "Traceback" not in result.output


def test_mifare_dump_writes_out_mfd_and_eml(runner, use_link, tmp_path):
    keys = tmp_path / "keys.txt"
    keys.write_text("0:A0A1A2A3A4A5:B0B1B2B3B4B5\n")
    out = tmp_path / "dump.json"
    mfd = tmp_path / "dump.mfd"
    eml = tmp_path / "dump.eml"
    use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare sector 0 A A0A1A2A3A4A5": ok(mifare_sector=_dump_sector_hex())
            }
        ),
    )
    result = runner.invoke(
        mifare_dump_cmd,
        [
            "--keys-file",
            str(keys),
            "--sectors",
            "1",
            "--out",
            str(out),
            "--mfd",
            str(mfd),
            "--eml",
            str(eml),
        ],
    )

    assert result.exit_code == 0
    assert json.loads(out.read_text())["sectors_read"] == 1

    raw = mfd.read_bytes()
    assert len(raw) == 64  # 1 sector * 4 blocks * 16 bytes
    assert raw[:4] == bytes.fromhex(_DUMP_UID)

    lines = eml.read_text().splitlines()
    assert len(lines) == 4
    assert all(len(line) == 32 for line in lines)
    assert lines[0] == _DUMP_UID + "00" + "08" + "0400" + "00" * 8
    assert lines[3] == "A0A1A2A3A4A5FF078069B0B1B2B3B4B5"


def test_mifare_dump_mfd_eml_fill_gaps_with_zeros(runner, use_link, tmp_path):
    keys = tmp_path / "keys.txt"
    keys.write_text("")  # no keys at all -> sector 0 is a gap
    mfd = tmp_path / "dump.mfd"
    eml = tmp_path / "dump.eml"
    use_link(tagscli, FakeLink())
    result = runner.invoke(
        mifare_dump_cmd,
        [
            "--keys-file",
            str(keys),
            "--sectors",
            "1",
            "--mfd",
            str(mfd),
            "--eml",
            str(eml),
        ],
    )

    assert result.exit_code == 1  # incomplete: sector 0 never read
    assert mfd.read_bytes() == b"\x00" * 64
    assert eml.read_text().splitlines() == ["0" * 32] * 4


def test_mifare_dump_out_refuses_overwrite_without_force(runner, tmp_path):
    keys = tmp_path / "keys.txt"
    keys.write_text("0:A0A1A2A3A4A5:B0B1B2B3B4B5\n")
    out = tmp_path / "dump.json"
    out.write_text("stale")
    result = runner.invoke(
        mifare_dump_cmd, ["--keys-file", str(keys), "--sectors", "1", "--out", str(out)]
    )

    assert result.exit_code != 0
    assert out.read_text() == "stale"  # untouched


def test_mifare_dump_mfd_refuses_overwrite_without_force(runner, tmp_path):
    keys = tmp_path / "keys.txt"
    keys.write_text("0:A0A1A2A3A4A5:B0B1B2B3B4B5\n")
    mfd = tmp_path / "dump.mfd"
    mfd.write_bytes(b"stale")
    result = runner.invoke(
        mifare_dump_cmd, ["--keys-file", str(keys), "--sectors", "1", "--mfd", str(mfd)]
    )

    assert result.exit_code != 0
    assert mfd.read_bytes() == b"stale"  # untouched


def test_mifare_dump_force_overwrites_existing_outputs(runner, use_link, tmp_path):
    keys = tmp_path / "keys.txt"
    keys.write_text("0:A0A1A2A3A4A5:B0B1B2B3B4B5\n")
    out = tmp_path / "dump.json"
    out.write_text("stale")
    use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare sector 0 A A0A1A2A3A4A5": ok(mifare_sector=_dump_sector_hex())
            }
        ),
    )
    result = runner.invoke(
        mifare_dump_cmd,
        ["--keys-file", str(keys), "--sectors", "1", "--out", str(out), "--force"],
    )

    assert result.exit_code == 0
    assert json.loads(out.read_text())["sectors_read"] == 1


def test_mifare_dump_ctrl_c_dumps_partial_results(runner, use_link, tmp_path):
    keys = tmp_path / "keys.txt"
    keys.write_text("0:A0A1A2A3A4A5:B0B1B2B3B4B5\n1:C0C1C2C3C4C5:D0D1D2D3D4D5\n")
    fake = use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare sector 0 A A0A1A2A3A4A5": ok(mifare_sector=_dump_sector_hex())
            }
        ),
    )
    original_command = fake.command

    def _interrupt_on_sector_1(line, read_timeout=None):
        if line.startswith("mifare sector 1"):
            raise KeyboardInterrupt
        return original_command(line, read_timeout)

    fake.command = _interrupt_on_sector_1

    result = runner.invoke(
        mifare_dump_cmd, ["--keys-file", str(keys), "--sectors", "2", "--json"]
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 1
    assert payload["sectors_read"] == 1
    assert payload["sectors"][0]["sector"] == 0
    assert payload["failed_sectors"] == []  # sector 1 wasn't marked failed, just unread


def test_mifare_dump_json_flag_emits_json_on_stdout(runner, use_link, tmp_path):
    keys = tmp_path / "keys.txt"
    keys.write_text("0:A0A1A2A3A4A5:B0B1B2B3B4B5\n")
    use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare sector 0 A A0A1A2A3A4A5": ok(mifare_sector=_dump_sector_hex())
            }
        ),
    )
    result = runner.invoke(
        mifare_dump_cmd, ["--keys-file", str(keys), "--sectors", "1", "--json"]
    )
    payload = json.loads(result.stdout)  # doesn't raise -> stdout is pure JSON

    assert result.exit_code == 0
    assert payload["uid"] == _DUMP_UID
    assert payload["sectors_read"] == 1
    # the table/progress UI mifare dump shows without --json didn't leak in
    assert "MifareClassic dump" not in result.stdout


def test_mifare_dump_decode_shows_ascii_and_block0(runner, use_link, tmp_path):
    # --decode prints each read sector's raw blocks with an ASCII column and,
    # for sector 0, the dissected block 0 (UID/SAK/ATQA).
    keys = tmp_path / "keys.txt"
    keys.write_text("0:A0A1A2A3A4A5:B0B1B2B3B4B5\n")
    block1 = b"Cuarta prueba!!!".hex()  # 16 printable bytes -> readable ASCII
    use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare sector 0 A A0A1A2A3A4A5": ok(
                    mifare_sector=_dump_sector_hex(block1)
                )
            }
        ),
    )
    result = runner.invoke(
        mifare_dump_cmd, ["--keys-file", str(keys), "--sectors", "1", "--decode"]
    )
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "Decoded sectors" in out
    assert "|Cuarta prueba!!!|" in out  # ASCII column beside the data block
    # block 0 dissected
    assert f"uid {_DUMP_UID}" in out
    assert "MIFARE Classic 1K" in out


def test_mifare_dump_decode_is_suppressed_by_json(runner, use_link, tmp_path):
    keys = tmp_path / "keys.txt"
    keys.write_text("0:A0A1A2A3A4A5:B0B1B2B3B4B5\n")
    use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare sector 0 A A0A1A2A3A4A5": ok(mifare_sector=_dump_sector_hex())
            }
        ),
    )
    result = runner.invoke(
        mifare_dump_cmd,
        ["--keys-file", str(keys), "--sectors", "1", "--decode", "--json"],
    )

    # --json owns stdout: the decoded human view must not leak into it.
    payload = json.loads(result.stdout)
    assert payload["sectors_read"] == 1
    assert "Decoded sectors" not in result.stdout


# ── mifare restore ────────────────────────────────────────────────────────────
# docs/CLI_IMPROVEMENTS_MifareRestore.md §7 (gen2). Restore consumes the
# canonical `mifare dump` JSON and writes it back block by block: data blocks,
# then the trailer last (unless --skip-trailers), with block 0 gated behind
# --write-block0 + a non-destructive probe. FakeLink answers unscripted writes
# ok, so a test only scripts the one command it wants to fail.

# Access-bit byte triples for a trailer's C1/C2/C3 (hex chars 12-17):
_AC_VALID = "FF0780"  # transport default — valid, not self-locking
_AC_FROZEN = "7F0F08"  # valid but leaves the trailer permanently unwritable
_AC_INVALID = "000000"  # C-bits equal their inverses -> invalid


def _restore_trailer_block(
    ac: str = _AC_VALID, key_a: str = "A0A1A2A3A4A5", key_b: str = "B0B1B2B3B4B5"
) -> str:
    """A 32-hex trailer block: key A (12) + access bits (6) + GPB (2) + key B."""
    return key_a + ac + "69" + key_b


def _restore_dump(tmp_path, ac: str = _AC_VALID, sectors=None, sectors_total: int = 2):
    """Write a canonical `mifare dump` JSON to a temp file and return its path.

    Two sectors by default; sector 0's block 0 carries a UID so --write-block0
    has something to write, and both trailers use the same access bits `ac`."""
    data = "11" * 16
    if sectors is None:
        sectors = [
            {
                "sector": 0,
                "blocks": ["DE" * 16, data, data, _restore_trailer_block(ac)],
            },
            {"sector": 1, "blocks": [data, data, data, _restore_trailer_block(ac)]},
        ]
    dump = {"sectors_total": sectors_total, "sectors": sectors}
    path = tmp_path / "dump.json"
    path.write_text(json.dumps(dump))
    return path


def test_mifare_restore_writes_data_and_trailers_skipping_block0(
    runner, use_link, tmp_path
):
    dump = _restore_dump(tmp_path)
    fake = use_link(tagscli, FakeLink())

    result = runner.invoke(
        mifare_restore_cmd, ["--dump", str(dump), "--json"], catch_exceptions=False
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["sectors_written"] == 2
    assert payload["block0_written"] is False
    assert payload["trailers_skipped"] is False
    # authenticated per sector with key A from the dump trailer
    assert "mifare auth 0 A A0A1A2A3A4A5" in fake.sent
    assert "mifare auth 4 A A0A1A2A3A4A5" in fake.sent
    # trailers written last (blocks 3 and 7)
    trailer = _restore_trailer_block()
    assert f"mifare write 3 {trailer}" in fake.sent
    assert f"mifare write 7 {trailer}" in fake.sent
    # block 0 (the UID) left untouched by default
    assert not any(s.startswith("mifare write 0 ") for s in fake.sent)


def test_mifare_restore_skip_trailers_never_writes_a_trailer_block(
    runner, use_link, tmp_path
):
    dump = _restore_dump(tmp_path)
    fake = use_link(tagscli, FakeLink())

    result = runner.invoke(
        mifare_restore_cmd,
        ["--dump", str(dump), "--skip-trailers", "--json"],
        catch_exceptions=False,
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["trailers_skipped"] is True
    assert not any(
        s.startswith("mifare write 3 ") or s.startswith("mifare write 7 ")
        for s in fake.sent
    )


def test_mifare_restore_write_block0_prompts_and_declining_skips_it(
    runner, use_link, tmp_path
):
    dump = _restore_dump(tmp_path)
    fake = use_link(tagscli, FakeLink())

    result = runner.invoke(
        mifare_restore_cmd, ["--dump", str(dump), "--write-block0"], input="n\n"
    )

    assert result.exit_code == 1
    assert "UID" in result.output  # the block-0 warning was shown
    # declined before opening the session -> no writes at all to block 0
    assert not any(s.startswith("mifare write 0 ") for s in fake.sent)


def test_mifare_restore_write_block0_yes_probes_then_writes_the_uid(
    runner, use_link, tmp_path
):
    dump = _restore_dump(tmp_path)
    current = "AA" * 16
    fake = use_link(
        tagscli, FakeLink(responses={"mifare read 0": ok(mifare_data="0 " + current)})
    )

    result = runner.invoke(
        mifare_restore_cmd,
        ["--dump", str(dump), "--write-block0", "--yes", "--json"],
        catch_exceptions=False,
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["block0_written"] is True
    # non-destructive probe: read block 0, echo the same bytes back, THEN write
    assert fake.sent.index("mifare read 0") < fake.sent.index(
        f"mifare write 0 {current}"
    )
    assert f"mifare write 0 {'DE' * 16}" in fake.sent  # the dump's UID


def test_mifare_restore_write_block0_aborts_when_probe_shows_a_genuine_card(
    runner, use_link, tmp_path
):
    dump = _restore_dump(tmp_path)
    current = "AA" * 16
    # the echo-write of the card's own block 0 fails -> not a magic card
    fake = use_link(
        tagscli,
        FakeLink(
            responses={
                "mifare read 0": ok(mifare_data="0 " + current),
                f"mifare write 0 {current}": err("write not allowed"),
            }
        ),
    )

    result = runner.invoke(
        mifare_restore_cmd,
        ["--dump", str(dump), "--write-block0", "--yes", "--json"],
        catch_exceptions=False,
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 1
    assert payload["block0_written"] is False
    # the real UID write never happened — the probe stopped it
    assert f"mifare write 0 {'DE' * 16}" not in fake.sent
    assert any("not writable" in s["reason"] for s in payload["failed_sectors"])


def test_mifare_restore_refuses_a_trailer_with_invalid_access_bits(
    runner, use_link, tmp_path
):
    # sector 0's trailer is invalid, sector 1's is fine -> 0 fails, 1 continues
    dump = _restore_dump(
        tmp_path,
        sectors=[
            {
                "sector": 0,
                "blocks": [
                    "DE" * 16,
                    "11" * 16,
                    "11" * 16,
                    _restore_trailer_block(_AC_INVALID),
                ],
            },
            {
                "sector": 1,
                "blocks": [
                    "11" * 16,
                    "11" * 16,
                    "11" * 16,
                    _restore_trailer_block(_AC_VALID),
                ],
            },
        ],
    )
    fake = use_link(tagscli, FakeLink())

    result = runner.invoke(
        mifare_restore_cmd, ["--dump", str(dump), "--json"], catch_exceptions=False
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 1
    assert payload["sectors_written"] == 1  # only sector 1
    failed = {s["sector"]: s["reason"] for s in payload["failed_sectors"]}
    assert 0 in failed and "invalid access bits" in failed[0]
    # the invalid trailer was never written; sector 1's valid one was
    assert not any(s.startswith("mifare write 3 ") for s in fake.sent)
    assert f"mifare write 7 {_restore_trailer_block(_AC_VALID)}" in fake.sent


def test_mifare_restore_warns_but_writes_a_self_locking_trailer(
    runner, use_link, tmp_path
):
    dump = _restore_dump(tmp_path, ac=_AC_FROZEN)
    fake = use_link(tagscli, FakeLink())

    result = runner.invoke(
        mifare_restore_cmd, ["--dump", str(dump), "--json"], catch_exceptions=False
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["sectors_written"] == 2
    # written despite being self-locking, but each sector carries a warning
    assert f"mifare write 3 {_restore_trailer_block(_AC_FROZEN)}" in fake.sent
    assert all("warning" in s for s in payload["sectors"])
    assert "frozen" in payload["sectors"][0]["warning"].lower()


def test_mifare_restore_marks_a_sector_failed_when_a_write_is_denied(
    runner, use_link, tmp_path
):
    # sector 1's block 1 (block 5) can't be written -> sector 1 fails, 0 is fine
    denied = "AB" * 16
    dump = _restore_dump(
        tmp_path,
        sectors=[
            {
                "sector": 0,
                "blocks": ["DE" * 16, "11" * 16, "11" * 16, _restore_trailer_block()],
            },
            {
                "sector": 1,
                "blocks": ["11" * 16, denied, "11" * 16, _restore_trailer_block()],
            },
        ],
    )
    fake = use_link(
        tagscli, FakeLink(responses={f"mifare write 5 {denied}": err("write denied")})
    )

    result = runner.invoke(
        mifare_restore_cmd, ["--dump", str(dump), "--json"], catch_exceptions=False
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 1
    assert payload["sectors_written"] == 1  # sector 0 only
    failed = {s["sector"]: s["reason"] for s in payload["failed_sectors"]}
    assert failed == {1: "write denied"}


def test_mifare_restore_ctrl_c_reports_partial_results(runner, use_link, tmp_path):
    dump = _restore_dump(tmp_path)
    fake = use_link(tagscli, FakeLink())
    original_command = fake.command

    def _interrupt_on_sector_1(line, read_timeout=None):
        if line.startswith("mifare auth 4"):  # sector 1's auth
            raise KeyboardInterrupt
        return original_command(line, read_timeout)

    fake.command = _interrupt_on_sector_1

    result = runner.invoke(
        mifare_restore_cmd, ["--dump", str(dump), "--json"], catch_exceptions=False
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 1
    assert payload["interrupted"] is True
    assert [s["sector"] for s in payload["sectors"]] == [0]  # sector 1 never ran


def test_mifare_restore_only_attempts_sectors_present_in_a_partial_dump(
    runner, use_link, tmp_path
):
    # a partial dump: sectors_total says 4 but only 0 and 1 were captured
    dump = _restore_dump(tmp_path, sectors_total=4)
    fake = use_link(tagscli, FakeLink())

    result = runner.invoke(
        mifare_restore_cmd, ["--dump", str(dump), "--json"], catch_exceptions=False
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["target_sectors"] == 2
    assert [s["sector"] for s in payload["sectors"]] == [0, 1]
    assert not any(s.startswith("mifare auth 8") for s in fake.sent)  # sector 2


def test_mifare_restore_json_flag_emits_pure_json_on_stdout(runner, use_link, tmp_path):
    dump = _restore_dump(tmp_path)
    use_link(tagscli, FakeLink())

    result = runner.invoke(
        mifare_restore_cmd, ["--dump", str(dump), "--json"], catch_exceptions=False
    )
    payload = json.loads(result.stdout)  # doesn't raise -> stdout is pure JSON

    assert result.exit_code == 0
    assert payload["source_dump"] == str(dump)
    assert "MifareClassic restore" not in result.stdout  # the table UI didn't leak


# ── code ─────────────────────────────────────────────────────────────────────
# `mifare code` — offline text → block hex. No link, no card: these drive the
# encoder and the sector layout only.


def test_mifare_code_pads_text_into_one_block(runner):
    result = runner.invoke(mifare_code_cmd, ["ElectronicCats"])
    out = flat(result.stdout)

    assert result.exit_code == 0
    # "ElectronicCats" (14 bytes) zero-padded out to a whole 16-byte block.
    assert "456C656374726F6E6963436174730000" in out
    assert "pass --sector" in out


def test_mifare_code_places_blocks_on_the_sector_data_blocks(runner):
    result = runner.invoke(mifare_code_cmd, ["A" * 40, "--sector", "4"])
    out = flat(result.stdout)

    assert result.exit_code == 0
    # Sector 4 = blocks 16-19; 19 is the trailer and must never be targeted.
    assert "block 16" in out and "block 17" in out and "block 18" in out
    assert "block 19" not in out


def test_mifare_code_skips_block0_of_sector_0(runner):
    result = runner.invoke(mifare_code_cmd, ["hi", "--sector", "0", "--json"])
    out = json.loads(result.stdout.strip().splitlines()[-1])

    assert result.exit_code == 0
    # Block 0 is the factory UID block — the first usable block is 1.
    assert out["blocks"] == [
        {"index": 0, "block": 1, "data": "68690000000000000000000000000000"}
    ]


def test_mifare_code_rejects_text_larger_than_the_sector(runner):
    result = runner.invoke(mifare_code_cmd, ["A" * 60, "--sector", "2"])
    out = flat(result.output)

    assert result.exit_code == 1
    assert "48 byte(s)" in out


def test_mifare_code_honours_a_custom_pad_byte(runner):
    result = runner.invoke(mifare_code_cmd, ["hi", "--pad", "FF", "--json"])
    out = json.loads(result.stdout.strip().splitlines()[-1])

    assert result.exit_code == 0
    assert out["blocks"][0]["data"] == "6869" + "FF" * 14


def test_mifare_code_rejects_a_malformed_pad(runner):
    result = runner.invoke(mifare_code_cmd, ["hi", "--pad", "ZZ"])

    assert result.exit_code == 1
    assert "--pad" in flat(result.output)


def test_mifare_code_keys_file_prints_auth_and_write_lines(runner, tmp_path):
    keyfile = tmp_path / "card.keys"
    keyfile.write_text("1:D3F7D3F7D3F7:FFFFFFFFFFFF\n")
    result = runner.invoke(
        mifare_code_cmd, ["hola", "--sector", "1", "-k", str(keyfile)]
    )
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "auth --block 4 --key-type A --key D3F7D3F7D3F7" in out
    assert "write --block 4 --data 686F6C610000" in out


def test_mifare_code_keys_file_falls_back_to_key_b(runner, tmp_path):
    keyfile = tmp_path / "card.keys"
    keyfile.write_text("1::FFFFFFFFFFFF\n")
    result = runner.invoke(
        mifare_code_cmd, ["hola", "--sector", "1", "-k", str(keyfile), "--json"]
    )
    out = json.loads(result.stdout.strip().splitlines()[-1])

    assert result.exit_code == 0
    assert "--key-type B --key FFFFFFFFFFFF" in out["commands"][0]


def test_mifare_code_keys_file_errors_when_sector_missing(runner, tmp_path):
    keyfile = tmp_path / "card.keys"
    keyfile.write_text("0:A0A1A2A3A4A5:FFFFFFFFFFFF\n")
    result = runner.invoke(
        mifare_code_cmd, ["hola", "--sector", "5", "-k", str(keyfile)]
    )

    assert result.exit_code == 1
    assert "sector 5 not found" in flat(result.output)


def test_mifare_code_keys_file_requires_a_sector(runner, tmp_path):
    keyfile = tmp_path / "card.keys"
    keyfile.write_text("1:D3F7D3F7D3F7:FFFFFFFFFFFF\n")
    result = runner.invoke(mifare_code_cmd, ["hola", "-k", str(keyfile)])

    assert result.exit_code == 1
    assert "--keys-file needs --sector" in flat(result.output)


def test_mifare_code_rejects_empty_text(runner):
    result = runner.invoke(mifare_code_cmd, [""])

    assert result.exit_code == 1
    assert "empty" in flat(result.output)
