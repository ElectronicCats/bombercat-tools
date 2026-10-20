#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

# test_mifare_device_sweep.py — Fase 4 of the "Plan de Implementación Firmware
# MIFARE" (task 4.1): the host client drives the DEVICE's own `mifare check`
# sweep (ONE round-trip, consolidated result) instead of one `mifare auth` USB
# round-trip per candidate, with the host-driven sweep kept as the fallback.
#
# Proves:
#   - the device's wire format is parsed exactly (per-sector keys, holes as
#     "-", the counters line), and a malformed key is a hole, never a guess;
#   - `--mode` picks the right side, and `auto` only moves the sweep to the
#     device when both sides can try the SAME candidates (otherwise the
#     device's smaller table would turn a recovered key into a false unknown);
#   - in device mode the CLI issues ONE command and no per-key auth, and
#     reports the device's own counters;
#   - a firmware without the on-device sweep falls back to the host-driven one
#     (auto) or fails loudly (device);
#   - the trailer-read key-B recovery still runs in device mode.
#
# No PN7150: the device is a fake that answers like the firmware does.

import json

import pytest

from modules.core.bombercat import DeviceError, Response
from modules.tags.mifare import session as mifare_session
from modules.tags.mifare.check import mifare_check_cmd
from modules.tags.mifare.device_sweep import (
    DEVICE_SWEEP_MAX_KEYS,
    build_device_command,
    parse_device_sweep,
)

from conftest import FakeLink, err, flat, ok

import mifare_card as mc


# ── a fake that speaks the device's sweep protocol ───────────────────────────


class SweepLink(FakeLink):
    """A fake device that answers `mifare check` the way the firmware does:
    one `:mifare_check_sN <A|-> <B|->` line per sector plus the counters
    line, then `+OK`."""

    def __init__(self, sectors: dict, stat: str = "", **kw):
        super().__init__(**kw)
        self.sectors = sectors  # {sector: (keyA, keyB)} — None for a hole
        self.stat = stat
        self.checks = []  # the `mifare check ...` lines it was asked to run

    def command(self, line: str, read_timeout=None) -> Response:
        stripped = line.strip()
        if stripped.startswith("mifare check"):
            self.sent.append(stripped)
            self.checks.append(stripped)
            data = {}
            for sector, (key_a, key_b) in self.sectors.items():
                data[f"mifare_check_s{sector}"] = f"{key_a or '-'} {key_b or '-'}"
            if self.stat:
                data["mifare_check_stat"] = self.stat
            return ok(**data)
        return super().command(stripped, read_timeout)


class DisconnectingLink(SweepLink):
    """A board that loses USB the first time it is asked to sweep — what a
    reboot looks like from the host: the CDC re-enumerates and the open port
    dies mid-command."""

    def __init__(self, *a, come_back: bool = True, **kw):
        super().__init__(*a, **kw)
        self.come_back = come_back
        self.dropped = False
        self.attempts = 0

    def command(self, line: str, read_timeout=None) -> Response:
        stripped = line.strip()
        if stripped.startswith("mifare check"):
            self.attempts += 1
            if not self.dropped:
                self.dropped = True
                raise DeviceError(
                    "the device disconnected while answering 'mifare check' "
                    "(device reports readiness to read but returned no data "
                    "(device disconnected or multiple access on port?)) — it "
                    "rebooted or lost USB; re-plug it and retry"
                )
        return super().command(stripped, read_timeout)

    def reopen(self, ready_timeout: float = 15.0) -> bool:
        self.reopened += 1
        return self.come_back


def _sector_lines(link: SweepLink):
    return [line for line in link.sent if line.startswith("mifare check")]


def _auths(link) -> int:
    return sum(1 for line in link.sent if line.startswith("mifare auth"))


# ── the wire format ──────────────────────────────────────────────────────────


def test_parse_reads_the_per_sector_keys_and_the_counters():
    result = parse_device_sweep(
        {
            "mifare_check_s0": "A0A1A2A3A4A5 FFFFFFFFFFFF",
            "mifare_check_s1": "D3F7D3F7D3F7 -",
            "mifare_check_s3": "- -",
            "mifare_check_stat": "attempts=66 naive=1024 recovered=3/4",
        }
    )
    assert result.keys[0] == ("A0A1A2A3A4A5", "FFFFFFFFFFFF")
    assert result.keys[1] == ("D3F7D3F7D3F7", None)
    assert result.keys[3] == (None, None)
    assert result.attempts == 66
    assert result.naive == 1024
    assert result.recovered == 3
    assert result.total == 4
    assert result.recovered_count() == 3


def test_parse_never_guesses_a_malformed_key():
    # A hole is "-", never zeros and never a guess: a token that isn't a
    # well-formed 12-hex key is a hole too.
    result = parse_device_sweep({"mifare_check_s0": "notahexkey 000000000000"})
    assert result.keys[0] == (None, "000000000000")
    assert parse_device_sweep({"mifare_check_s0": "ZZZZZZZZZZZZ -"}).keys[0] == (
        None,
        None,
    )


def test_parse_ignores_unrelated_lines():
    result = parse_device_sweep(
        {
            "mifare": "1234 DECAFBAD 4 - auth_fail",
            "mifare_check_s0": "FFFFFFFFFFFF FFFFFFFFFFFF",
            "mifare_check_stat": "attempts=2 naive=64 recovered=2/2",
        }
    )
    assert set(result.keys) == {0}


def test_command_carries_the_sectors_and_the_keys():
    assert build_device_command(16) == "mifare check --sectors 16"
    assert (
        build_device_command(4, ["FFFFFFFFFFFF", "a0a1a2a3a4a5"])
        == "mifare check --sectors 4 --keys FFFFFFFFFFFF,A0A1A2A3A4A5"
    )


# ── the mode policy ──────────────────────────────────────────────────────────


def test_auto_keeps_the_host_sweep_when_the_universe_is_bigger():
    # No --keys: the bundled 2477-key dictionary is far bigger than the
    # device's compact table, so the host sweep keeps the coverage.
    from modules.tags.mifare.check import _plan_device_sweep

    assert _plan_device_sweep("auto", [], "both", None) == (False, None)


def test_auto_moves_to_the_device_when_both_sides_try_the_same_keys():
    from modules.tags.mifare.check import _plan_device_sweep

    keys = ["FFFFFFFFFFFF", "A0A1A2A3A4A5"]
    assert _plan_device_sweep("auto", [], "both", keys) == (True, keys)


def test_auto_stays_on_the_host_for_host_only_sources():
    from modules.tags.mifare.check import _plan_device_sweep

    keys = ["FFFFFFFFFFFF"]
    # --dict / --dict-file / --uid-derived are host-side candidate sources.
    assert _plan_device_sweep("auto", ["--dict"], "both", keys) == (False, None)
    # A single key type: the device always sweeps A and B.
    assert _plan_device_sweep("auto", [], "A", keys) == (False, None)
    # More keys than the device's --keys cap.
    too_many = [f"{i:012X}" for i in range(DEVICE_SWEEP_MAX_KEYS + 1)]
    assert _plan_device_sweep("auto", [], "both", too_many) == (False, None)


def test_device_mode_uses_the_devices_own_universe():
    from modules.tags.mifare.check import _plan_device_sweep

    assert _plan_device_sweep("device", [], "both", None) == (True, None)
    keys = ["FFFFFFFFFFFF"]
    assert _plan_device_sweep("device", [], "both", keys) == (True, keys)


def test_device_mode_refuses_what_the_device_cannot_do():
    from modules.tags.mifare.check import _plan_device_sweep

    with pytest.raises(SystemExit):
        _plan_device_sweep("device", ["--dict"], "both", None)
    with pytest.raises(SystemExit):
        _plan_device_sweep(
            "device",
            [],
            "both",
            [f"{i:012X}" for i in range(DEVICE_SWEEP_MAX_KEYS + 1)],
        )


def test_host_mode_never_touches_the_device_sweep():
    from modules.tags.mifare.check import _plan_device_sweep

    assert _plan_device_sweep("host", [], "both", ["FFFFFFFFFFFF"]) == (
        False,
        None,
    )


# ── the device sweep, end to end ─────────────────────────────────────────────


def _keyfile(tmp_path, *keys):
    path = tmp_path / "keys.keys"
    path.write_text("".join(k + "\n" for k in keys))
    return path


def test_device_mode_asks_the_device_once_and_never_auths_per_key(
    runner, use_link, tmp_path
):
    # The whole point of the on-device sweep: ONE command for the whole
    # sweep, instead of one `mifare auth` round-trip per candidate.
    link = use_link(
        mifare_session,
        SweepLink(
            {0: ("A0A1A2A3A4A5", "FFFFFFFFFFFF"), 1: ("FFFFFFFFFFFF", None)},
            stat="attempts=6 naive=64 recovered=3/4",
        ),
    )
    keyfile = _keyfile(tmp_path, "FFFFFFFFFFFF", "A0A1A2A3A4A5")
    result = runner.invoke(
        mifare_check_cmd,
        ["--mode", "device", "--keys", str(keyfile), "--sectors", "2", "--json"],
    )
    assert result.exit_code == 1  # sector 1 key B is a hole
    doc = json.loads(result.stdout)
    assert doc["sectors"][0] == {
        "sector": 0,
        "key_a": "A0A1A2A3A4A5",
        "key_b": "FFFFFFFFFFFF",
    }
    assert doc["sectors"][1] == {"sector": 1, "key_a": "FFFFFFFFFFFF", "key_b": None}
    assert doc["stats"]["mode"] == "device"
    # the counters are the DEVICE's own
    assert doc["stats"]["attempts"] == 6
    assert doc["stats"]["naive_attempts"] == 64
    assert _sector_lines(link) == [
        "mifare check --sectors 2 --keys FFFFFFFFFFFF,A0A1A2A3A4A5"
    ]
    assert _auths(link) == 0


def test_device_mode_reports_a_fully_recovered_card(runner, use_link, tmp_path):
    link = use_link(
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
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["recovered"] == 2
    assert doc["total"] == 2
    assert _auths(link) == 0


def test_device_mode_writes_the_sector_keyfile(runner, use_link, tmp_path):
    use_link(
        mifare_session,
        SweepLink(
            {0: ("FFFFFFFFFFFF", "FFFFFFFFFFFF"), 1: ("A0A1A2A3A4A5", None)},
            stat="attempts=4 naive=64 recovered=3/4",
        ),
    )
    keyfile = _keyfile(tmp_path, "FFFFFFFFFFFF")
    out = tmp_path / "sector.keys"
    result = runner.invoke(
        mifare_check_cmd,
        [
            "--mode",
            "device",
            "--keys",
            str(keyfile),
            "--sectors",
            "2",
            "--output-keys",
            str(out),
        ],
    )
    assert result.exit_code == 1
    assert out.read_text() == ("0:FFFFFFFFFFFF:FFFFFFFFFFFF\n1:A0A1A2A3A4A5:\n")


def test_device_mode_still_recovers_key_b_by_trailer_read(runner, use_link, tmp_path):
    # The trailer-read fallback is a host-side step (it needs the sector's
    # access bits) — it must still run after a device sweep.
    link = use_link(
        mifare_session,
        SweepLink(
            {0: ("FFFFFFFFFFFF", None)},
            stat="attempts=2 naive=64 recovered=1/2",
            responses={
                # 4 blocks: 3 data + the trailer (key A read back as zeros,
                # transport access bits, key B in cleartext).
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
    assert "mifare sector 0 A FFFFFFFFFFFF" in link.sent


def test_device_mode_filters_the_report_to_the_requested_key_type(
    runner, use_link, tmp_path
):
    # The device always sweeps A and B; --key-type only filters the report.
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
        [
            "--mode",
            "device",
            "--keys",
            str(keyfile),
            "--sectors",
            "1",
            "--key-type",
            "A",
            "--json",
        ],
    )
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["sectors"][0] == {
        "sector": 0,
        "key_a": "FFFFFFFFFFFF",
        "key_b": None,
    }
    assert doc["total"] == 1


# ── the fallback ─────────────────────────────────────────────────────────────


def test_auto_falls_back_to_the_host_sweep_on_an_older_firmware(
    runner, use_link, tmp_path
):
    # The flashed firmware has no `mifare check`: the host-driven sweep takes
    # over, with the same result it always had.
    link = use_link(
        mifare_session,
        FakeLink(
            responses={
                "mifare check --sectors 2 --keys FFFFFFFFFFFF": err(
                    "unknown mifare subcommand"
                ),
                "mifare auth 0 A FFFFFFFFFFFF": ok(),
                "mifare auth 0 B FFFFFFFFFFFF": ok(),
                "mifare auth 4 A FFFFFFFFFFFF": ok(),
            }
        ),
    )
    keyfile = _keyfile(tmp_path, "FFFFFFFFFFFF")
    result = runner.invoke(
        mifare_check_cmd,
        ["--keys", str(keyfile), "--sectors", "2", "--json"],
    )
    doc = json.loads(result.stdout)
    assert doc["stats"]["mode"] == "host"
    assert doc["sectors"][0]["key_a"] == "FFFFFFFFFFFF"
    assert _auths(link) == 4  # the host sweep ran (2 sectors x 2 key types)


def test_device_mode_fails_loudly_on_an_older_firmware(runner, use_link, tmp_path):
    use_link(
        mifare_session,
        FakeLink(
            responses={
                "mifare check --sectors 1 --keys FFFFFFFFFFFF": err(
                    "unknown mifare subcommand"
                )
            }
        ),
    )
    keyfile = _keyfile(tmp_path, "FFFFFFFFFFFF")
    result = runner.invoke(
        mifare_check_cmd,
        ["--mode", "device", "--keys", str(keyfile), "--sectors", "1"],
    )
    assert result.exit_code == 1
    assert "no on-device sweep" in flat(result.output)


def test_auto_ignores_a_device_answer_without_a_sweep_result(
    runner, use_link, tmp_path
):
    # "+OK" but no sweep data: nothing to trust — fall back instead of
    # reporting every sector as unknown.
    link = use_link(
        mifare_session,
        FakeLink(
            responses={
                "mifare check --sectors 1 --keys FFFFFFFFFFFF": ok(),
                "mifare auth 0 A FFFFFFFFFFFF": ok(),
            }
        ),
    )
    keyfile = _keyfile(tmp_path, "FFFFFFFFFFFF")
    result = runner.invoke(
        mifare_check_cmd,
        ["--keys", str(keyfile), "--sectors", "1", "--json"],
    )
    doc = json.loads(result.stdout)
    assert doc["stats"]["mode"] == "host"
    assert doc["sectors"][0]["key_a"] == "FFFFFFFFFFFF"
    assert _auths(link) == 2  # the host sweep ran (1 sector x 2 key types)


def test_device_mode_reports_a_failed_sweep(runner, use_link, tmp_path):
    use_link(
        mifare_session,
        FakeLink(
            responses={
                "mifare check --sectors 1 --keys FFFFFFFFFFFF": err(
                    "card removed mid-sweep, tap a card again"
                )
            }
        ),
    )
    keyfile = _keyfile(tmp_path, "FFFFFFFFFFFF")
    result = runner.invoke(
        mifare_check_cmd,
        ["--mode", "device", "--keys", str(keyfile), "--sectors", "1"],
    )
    assert result.exit_code == 1
    assert "card removed mid-sweep" in flat(result.output)


# ── the default path is untouched ────────────────────────────────────────────


def test_the_default_sweep_is_still_the_host_one(runner, use_link):
    # No --keys: the bundled dictionary is too big for the device, so the
    # host sweep runs exactly as before — no `mifare check` on the wire.
    link = use_link(mifare_session, FakeLink())
    result = runner.invoke(mifare_check_cmd, ["--sectors", "1", "--json"])
    assert _sector_lines(link) == []
    assert json.loads(result.stdout)["stats"]["mode"] == "host"


# ── a board that goes away mid-command ───────────────────────────────────────


def test_device_mode_reconnects_and_retries_when_the_board_reboots(
    runner, use_link, tmp_path
):
    # A reboot re-enumerates the CDC and kills the open port: the sweep
    # command comes back as a serial error. The sweep is idempotent, so the
    # host re-opens the port and runs it again instead of dying.
    link = use_link(
        mifare_session,
        DisconnectingLink(
            {0: ("FFFFFFFFFFFF", "FFFFFFFFFFFF")},
            stat="attempts=2 naive=64 recovered=2/2",
        ),
    )
    keyfile = _keyfile(tmp_path, "FFFFFFFFFFFF")
    result = runner.invoke(
        mifare_check_cmd,
        ["--mode", "device", "--keys", str(keyfile), "--sectors", "1", "--json"],
    )
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["recovered"] == 2
    assert link.reopened == 1
    assert link.attempts == 2  # the sweep was asked twice


def test_device_mode_says_so_when_the_board_does_not_come_back(
    runner, use_link, tmp_path
):
    link = use_link(
        mifare_session,
        DisconnectingLink(
            {0: ("FFFFFFFFFFFF", "FFFFFFFFFFFF")},
            stat="attempts=2 naive=64 recovered=2/2",
            come_back=False,
        ),
    )
    keyfile = _keyfile(tmp_path, "FFFFFFFFFFFF")
    result = runner.invoke(
        mifare_check_cmd,
        ["--mode", "device", "--keys", str(keyfile), "--sectors", "1"],
    )
    assert result.exit_code == 1
    out = flat(result.output)
    assert "disconnected" in out
    assert "re-plug" in out


def test_a_rejected_command_is_not_retried_as_a_disconnect(runner, use_link, tmp_path):
    # "authentication failed"-style refusals are answers, not link losses:
    # re-opening the port would not help, so the host must not try.
    link = use_link(
        mifare_session,
        SweepLink({0: ("FFFFFFFFFFFF", "FFFFFFFFFFFF")}),
    )
    keyfile = _keyfile(tmp_path, "FFFFFFFFFFFF")
    result = runner.invoke(
        mifare_check_cmd,
        ["--mode", "device", "--keys", str(keyfile), "--sectors", "1"],
    )
    assert result.exit_code == 0
    assert link.reopened == 0


# ── the tap wait has to outlast the firmware's own auto-probe ───────────────


class _StreamOnly:
    """A link that only streams (the auto-probe's output)."""

    def __init__(self, lines):
        self.lines = list(lines)

    def stream(self, yield_empty: bool = False):
        for line in self.lines:
            yield line


def test_the_tap_wait_outlasts_the_firms_own_auto_probe():
    # The firmware opens the session only AFTER its auto-probe sweep (11 s
    # measured on a 1K card), and streams a line per sector while it runs.
    # The tap timeout must not fire while that sweep is in progress.
    from modules.tags.mifare.session import _wait_for_mifare_card

    link = _StreamOnly(
        [
            ":mifare_check_s0 A0A1A2A3A4A5 FFFFFFFFFFFF",
            ":mifare_check_s1 - -",
            ":mifare 1017398 10922893 4 00112233445566778899AABBCCDDEEFF ok",
        ]
    )
    assert _wait_for_mifare_card(link, 0.0, sweep_timeout=30.0) is True


def test_the_tap_wait_still_gives_up_when_no_card_shows_up():
    from modules.tags.mifare.session import _wait_for_mifare_card

    link = _StreamOnly(["waiting for a card..."])
    assert _wait_for_mifare_card(link, 0.0, sweep_timeout=30.0) is False
