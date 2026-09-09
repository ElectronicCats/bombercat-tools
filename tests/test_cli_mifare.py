#!/usr/bin/env python3

# Electronic Cats
# test_cli_mifare.py — `bombercat tags mifare ...` (modules/tags/mifare/).
# Same pattern as test_cli_tags.py: CliRunner + FakeLink, driving each
# command's real logic against a scripted `stream()`. Every command reaches
# the device through modules/tags/mifare/session.py, so `use_link` patches
# that one module no matter which command is under test.
# MIFARE_CLASSIC_PLAN.md §4, docs/CLI_IMPROVEMENTS_Mifare*.md.

import json

from conftest import FakeLink, err, flat, ok
from modules.tags.mifare import cli as mifarecli
from modules.tags.mifare import session as mifare_session
from modules.tags.mifare.check import mifare_check_cmd
from modules.tags.mifare.cli import (
    mifare_auth_cmd,
    mifare_keys_cmd,
    mifare_read_cmd,
    mifare_sector_cmd,
    mifare_write_cmd,
)
from modules.tags.mifare.code import mifare_code_cmd
from modules.tags.mifare.dump import mifare_dump_cmd
from modules.tags.mifare.restore import mifare_restore_cmd
from modules.tags.mifare.write_text import mifare_write_text_cmd


_MIFARE_KEYS_LINE = ok(
    mifare_key0="default_ff FFFFFFFFFFFF",
    mifare_key1="default_00 000000000000",
    mifare_key2="default_a0a1a2 A0A1A2A3A4A5",
)


def test_mifare_auth_succeeds_when_a_session_is_already_open(runner, use_link):
    fake = use_link(mifare_session, FakeLink())
    result = runner.invoke(
        mifare_auth_cmd, ["--block", "4", "--key-type", "A", "--key", "FFFFFFFFFFFF"]
    )

    assert result.exit_code == 0
    assert "authenticated block 4" in flat(result.stdout)
    assert "mifare auth 4 A FFFFFFFFFFFF" in fake.sent


def test_mifare_auth_rejects_a_malformed_key_locally(runner, use_link):
    fake = use_link(mifare_session, FakeLink())
    result = runner.invoke(
        mifare_auth_cmd, ["--block", "4", "--key-type", "A", "--key", "ZZ"]
    )

    assert result.exit_code == 1
    assert "12 hex characters" in flat(result.output)
    assert fake.sent == []  # never reached the wire


def test_mifare_auth_waits_for_a_tap_when_no_card_is_selected(runner, use_link):
    fake = use_link(
        mifare_session,
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
        mifare_session,
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
        mifare_session,
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
        mifare_session,
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
    fake = use_link(mifare_session, FakeLink())
    result = runner.invoke(mifare_write_cmd, ["--block", "4", "--data", "AABB"])

    assert result.exit_code == 1
    assert "32 hex characters" in flat(result.output)
    assert fake.sent == []


def test_mifare_write_succeeds(runner, use_link):
    fake = use_link(mifare_session, FakeLink())
    result = runner.invoke(
        mifare_write_cmd,
        ["--block", "4", "--data", "00112233445566778899AABBCCDDEEFF"],
    )

    assert result.exit_code == 0
    assert "wrote block 4" in flat(result.stdout)
    assert "mifare write 4 00112233445566778899AABBCCDDEEFF" in fake.sent


def test_mifare_sector_reports_the_sector_data(runner, use_link):
    use_link(
        mifare_session,
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
        mifare_session,
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
    use_link(mifare_session, FakeLink(responses={"mifare keys": _MIFARE_KEYS_LINE}))
    result = runner.invoke(mifare_keys_cmd, [])
    out = flat(result.stdout)

    assert result.exit_code == 0
    assert "default_ff" in out and "FFFFFFFFFFFF" in out
    assert "default_00" in out and "000000000000" in out
    assert "default_a0a1a2" in out and "A0A1A2A3A4A5" in out


def test_mifare_keys_json_emits_one_object_per_key(runner, use_link):
    use_link(mifare_session, FakeLink(responses={"mifare keys": _MIFARE_KEYS_LINE}))
    result = runner.invoke(mifare_keys_cmd, ["--json"])
    lines = [json.loads(line) for line in result.stdout.strip().splitlines()]

    assert result.exit_code == 0
    assert lines == [
        {"name": "default_ff", "key": "FFFFFFFFFFFF"},
        {"name": "default_00", "key": "000000000000"},
        {"name": "default_a0a1a2", "key": "A0A1A2A3A4A5"},
    ]


def test_mifare_reports_a_board_that_will_not_handshake(runner, use_link):
    link = use_link(mifare_session, FakeLink(ping_ok=False))
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
        mifare_session,
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
        mifare_session,
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
        mifare_session,
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
        mifare_session,
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
        mifare_session,
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
        mifare_session,
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
        mifare_session,
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
        mifare_session,
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
        mifare_session,
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
        mifare_session,
        FakeLink(responses={"mifare auth 0 A FFFFFFFFFFFF": ok()}),
    )
    result = runner.invoke(
        mifare_check_cmd,
        ["--keys", str(keyfile), "--sectors", "1", "--key-type", "A", "--json"],
    )

    assert result.exit_code == 0
    assert not any(s.startswith("mifare sector") for s in fake.sent)




# ── group wiring ─────────────────────────────────────────────────────────────


def test_mifare_group_exposes_all_subcommands():
    assert set(mifarecli.mifare.commands) == {
        "auth",
        "read",
        "write",
        "sector",
        "keys",
        "check",
        "dump",
        "restore",
        "code",
        "write-text",
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
        mifare_session,
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
    use_link(mifare_session, FakeLink(responses={"mifare auth 0 A FFFFFFFFFFFF": ok()}))
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
        mifare_session,
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
        mifare_session,
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
    use_link(mifare_session, FakeLink())
    result = runner.invoke(
        mifare_sector_cmd, ["--sector", "5", "--keys-file", str(keys)]
    )

    assert result.exit_code == 1
    assert "sector 5 not found" in flat(result.output)


def test_mifare_sector_keys_file_errors_on_a_malformed_file(runner, use_link, tmp_path):
    keys = tmp_path / "claves.txt"
    keys.write_text("0:nothex:787788C10203\n")
    use_link(mifare_session, FakeLink())
    result = runner.invoke(
        mifare_sector_cmd, ["--sector", "0", "--keys-file", str(keys)]
    )

    assert result.exit_code == 1
    assert "expected 'sector:keyA:keyB'" in flat(result.output)


def test_mifare_sector_rejects_both_key_and_keys_file(runner, use_link, tmp_path):
    keys = tmp_path / "claves.txt"
    keys.write_text("0:A0A1A2A3A4A5:787788C10203\n")
    use_link(mifare_session, FakeLink())
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
        mifare_session,
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
        mifare_session,
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
        mifare_session,
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
    use_link(mifare_session, FakeLink())
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
        mifare_session,
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
    use_link(mifare_session, FakeLink())
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
        mifare_session,
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
        mifare_session,
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
        mifare_session,
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
        mifare_session,
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
        mifare_session,
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
    fake = use_link(mifare_session, FakeLink())

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
    fake = use_link(mifare_session, FakeLink())

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
    fake = use_link(mifare_session, FakeLink())

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
        mifare_session, FakeLink(responses={"mifare read 0": ok(mifare_data="0 " + current)})
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
        mifare_session,
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
    fake = use_link(mifare_session, FakeLink())

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
    fake = use_link(mifare_session, FakeLink())

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
        mifare_session, FakeLink(responses={f"mifare write 5 {denied}": err("write denied")})
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
    fake = use_link(mifare_session, FakeLink())
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
    fake = use_link(mifare_session, FakeLink())

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
    use_link(mifare_session, FakeLink())

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


# ── write-text ───────────────────────────────────────────────────────────────
#
# `mifare write-text` — `code`'s encoder plus a real auth/write pass, so these
# drive a FakeLink and assert on the REPL lines it was sent.


def test_mifare_write_text_authenticates_then_writes_the_blocks(runner, use_link):
    keyfile = None
    fake = use_link(mifare_session, FakeLink())

    result = runner.invoke(
        mifare_write_text_cmd,
        ["hola", "--sector", "1", "--key", "d3f7d3f7d3f7", "--json"],
        catch_exceptions=False,
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["blocks_written"] == 1
    assert payload["key_type"] == "A"
    assert fake.sent[0] == "mifare auth 4 A D3F7D3F7D3F7"
    assert "mifare write 4 686F6C61000000000000000000000000" in fake.sent
    assert keyfile is None


def test_mifare_write_text_spans_blocks_and_takes_keys_from_the_keys_file(
    runner, use_link, tmp_path
):
    keyfile = tmp_path / "card.keys"
    keyfile.write_text("1::FFFFFFFFFFFF\n")
    fake = use_link(mifare_session, FakeLink())

    result = runner.invoke(
        mifare_write_text_cmd,
        ["A" * 20, "--sector", "1", "-k", str(keyfile), "--json"],
        catch_exceptions=False,
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["key_type"] == "B"
    assert payload["blocks_written"] == 2
    assert fake.sent[0] == "mifare auth 4 B FFFFFFFFFFFF"
    assert [e["block"] for e in payload["blocks"]] == [4, 5]


def test_mifare_write_text_starts_at_the_requested_block(runner, use_link):
    fake = use_link(mifare_session, FakeLink())

    result = runner.invoke(
        mifare_write_text_cmd,
        ["hola", "--block", "6", "--key", "FFFFFFFFFFFF", "--json"],
        catch_exceptions=False,
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["sector"] == 1
    assert fake.sent[0] == "mifare auth 4 A FFFFFFFFFFFF"
    assert [e["block"] for e in payload["blocks"]] == [6]


def test_mifare_write_text_refuses_a_trailer_block(runner, use_link):
    use_link(mifare_session, FakeLink())
    result = runner.invoke(
        mifare_write_text_cmd, ["hola", "--block", "7", "--key", "FFFFFFFFFFFF"]
    )

    assert result.exit_code == 1
    assert "trailer" in flat(result.output)


def test_mifare_write_text_refuses_block0_of_sector_0(runner, use_link):
    use_link(mifare_session, FakeLink())
    result = runner.invoke(
        mifare_write_text_cmd, ["hola", "--block", "0", "--key", "FFFFFFFFFFFF"]
    )

    assert result.exit_code == 1
    assert "manufacturer block" in flat(result.output)


def test_mifare_write_text_rejects_text_larger_than_the_room_left(runner, use_link):
    use_link(mifare_session, FakeLink())
    result = runner.invoke(
        mifare_write_text_cmd,
        ["A" * 40, "--block", "6", "--key", "FFFFFFFFFFFF"],
    )

    assert result.exit_code == 1
    assert "only 1 data block(s)" in flat(result.output)


def test_mifare_write_text_reports_a_failed_auth(runner, use_link):
    use_link(
        mifare_session,
        FakeLink(
            responses={"mifare auth 4 A FFFFFFFFFFFF": err("authentication failed")}
        ),
    )
    result = runner.invoke(
        mifare_write_text_cmd,
        ["hola", "--sector", "1", "--key", "FFFFFFFFFFFF", "--json"],
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 1
    assert payload["blocks_written"] == 0
    assert payload["reason"] == "authentication failed"


def test_mifare_write_text_stops_at_the_first_denied_write(runner, use_link):
    use_link(
        mifare_session,
        FakeLink(
            responses={
                "mifare write 5 "
                + "41" * 4
                + "00" * 12: err("write denied (access bits)")
            }
        ),
    )
    result = runner.invoke(
        mifare_write_text_cmd,
        ["A" * 20, "--sector", "1", "--key", "FFFFFFFFFFFF", "--json"],
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 1
    assert payload["blocks_written"] == 1
    assert "block 5 write failed" in payload["reason"]


def test_mifare_write_text_needs_exactly_one_destination(runner, use_link):
    use_link(mifare_session, FakeLink())
    both = runner.invoke(
        mifare_write_text_cmd,
        ["hola", "--sector", "1", "--block", "4", "--key", "FFFFFFFFFFFF"],
    )
    neither = runner.invoke(mifare_write_text_cmd, ["hola", "--key", "FFFFFFFFFFFF"])

    assert both.exit_code == 1 and neither.exit_code == 1
    assert "exactly one of --sector or --block" in flat(both.output)
    assert "exactly one of --sector or --block" in flat(neither.output)


def test_mifare_write_text_needs_exactly_one_key_source(runner, use_link, tmp_path):
    keyfile = tmp_path / "card.keys"
    keyfile.write_text("1:D3F7D3F7D3F7:FFFFFFFFFFFF\n")
    use_link(mifare_session, FakeLink())

    both = runner.invoke(
        mifare_write_text_cmd,
        ["hola", "--sector", "1", "--key", "FFFFFFFFFFFF", "-k", str(keyfile)],
    )
    neither = runner.invoke(mifare_write_text_cmd, ["hola", "--sector", "1"])

    assert both.exit_code == 1 and neither.exit_code == 1
    assert "not both" in flat(both.output)
    assert "one of --key or --keys-file is required" in flat(neither.output)


def test_mifare_write_text_honours_a_custom_pad_byte(runner, use_link):
    fake = use_link(mifare_session, FakeLink())
    result = runner.invoke(
        mifare_write_text_cmd,
        ["hi", "--sector", "1", "--key", "FFFFFFFFFFFF", "--pad", "FF"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "mifare write 4 6869" + "FF" * 14 in fake.sent
