#!/usr/bin/env python3

# Electronic Cats
# test_cli_relay.py — `bombercat relay config|run|stop|status|monitor`
# (modules/nfcgate/cli.py, exposed as the `relay` group). Every command is
# driven through Click's CliRunner with the link replaced by a FakeLink, so what
# is checked is the CLI's own behaviour: which commands it sends the board, what
# it prints, and the exit code it leaves behind.
# docs/NFCGATE_PLAN.md Fase 6, docs/GENERALIZE_CLI_PLAN.md §2.3.

import pytest
import serial

from conftest import DeviceError, FakeLink, err, flat, ok
from modules.nfcgate import cli as nfc
from modules.nfcgate.cli import (
    config,
    monitor_cmd,
    relay,
    run_cmd,
    status_cmd,
    stop_cmd,
)


@pytest.fixture(autouse=True)
def instant_polls(monkeypatch):
    """`run` polls `status` every 0.5 s; tests shouldn't wait for that."""
    monkeypatch.setattr(nfc.time, "sleep", lambda _s: None)


# ── _device_session (shared by every command here) ───────────────────────────


def test_session_reports_a_board_that_will_not_handshake(runner, use_link):
    link = use_link(nfc, FakeLink(ping_ok=False))
    result = runner.invoke(config, ["show"])

    assert result.exit_code == 1
    assert "did not answer the handshake" in flat(result.output)
    assert link.closed  # the port is released even on the error path


def test_session_reports_an_unresolvable_target(runner, monkeypatch):
    monkeypatch.setattr(
        nfc,
        "resolve_port",
        lambda *a, **k: (_ for _ in ()).throw(
            DeviceError("no BomberCat found; pass --port")
        ),
    )
    result = runner.invoke(config, ["show"])

    assert result.exit_code == 1
    assert "no BomberCat found" in flat(result.output)


def test_session_reports_a_serial_error_as_one_line(runner, monkeypatch):
    monkeypatch.setattr(nfc, "resolve_port", lambda *a, **k: "/dev/fake0")

    def _open(*a, **k):
        raise serial.SerialException("could not open port")

    monkeypatch.setattr(nfc, "DeviceLink", _open)
    result = runner.invoke(config, ["show"])

    assert result.exit_code == 1
    assert "SerialException: could not open port" in flat(result.output)
    assert "Traceback" not in result.output


def test_session_closes_the_port_after_a_successful_command(runner, use_link):
    link = use_link(nfc, FakeLink({"info": ok(fw="0.8.0")}))
    runner.invoke(config, ["show"])

    assert link.opened and link.closed


# ── config wifi ──────────────────────────────────────────────────────────────


def test_config_wifi_sets_credentials_saves_and_blinks(runner, use_link):
    link = use_link(nfc, FakeLink())
    result = runner.invoke(
        config, ["wifi", "--ssid", "HomeNet", "--password", "s3cret"]
    )

    assert result.exit_code == 0
    assert link.sent == [
        "set ssid HomeNet",
        "set pass s3cret",
        "save",
        "identify",
    ]
    assert "saved to flash" in flat(result.output)


def test_config_wifi_never_echoes_the_passphrase(runner, use_link):
    use_link(nfc, FakeLink())
    result = runner.invoke(
        config, ["wifi", "--ssid", "HomeNet", "--password", "s3cret"]
    )

    assert "s3cret" not in result.output
    assert "••••••" in result.output


def test_config_wifi_accepts_an_open_network(runner, use_link):
    link = use_link(nfc, FakeLink())
    result = runner.invoke(config, ["wifi", "--ssid", "HomeNet"])

    assert result.exit_code == 0
    assert link.sent[1] == "set pass"  # empty passphrase, no value


def test_config_wifi_without_save_warns_it_is_volatile(runner, use_link):
    link = use_link(nfc, FakeLink())
    result = runner.invoke(config, ["wifi", "--ssid", "HomeNet", "--no-save"])

    assert "save" not in link.sent
    assert "lost on reboot" in flat(result.output)


def test_config_wifi_requires_an_ssid(runner):
    result = runner.invoke(config, ["wifi"])
    assert result.exit_code == 2  # click usage error


def test_config_wifi_stops_at_the_first_rejected_set(runner, use_link):
    link = use_link(nfc, FakeLink({"set ssid HomeNet": err("ssid too long")}))
    result = runner.invoke(config, ["wifi", "--ssid", "HomeNet"])

    assert result.exit_code == 1
    assert "set ssid failed: ssid too long" in flat(result.output)
    assert link.sent == ["set ssid HomeNet"]  # no `pass`, no `save`


def test_config_wifi_reports_a_failed_save(runner, use_link):
    use_link(nfc, FakeLink({"save": err("flash write error")}))
    result = runner.invoke(config, ["wifi", "--ssid", "HomeNet"])

    assert result.exit_code == 1
    assert "save failed: flash write error" in flat(result.output)


def test_config_warns_when_the_firmware_cannot_blink(runner, use_link):
    use_link(nfc, FakeLink({"identify": err("unknown command")}))
    result = runner.invoke(config, ["wifi", "--ssid", "HomeNet"])

    assert result.exit_code == 0  # the config is already saved: never fatal
    assert "predates `identify`" in flat(result.output)


def test_config_survives_a_link_error_while_blinking(runner, use_link):
    use_link(nfc, FakeLink({"identify": DeviceError("timed out")}))
    result = runner.invoke(config, ["wifi", "--ssid", "HomeNet"])

    assert result.exit_code == 0
    assert "could not blink" in flat(result.output)


# ── config nfcgate ───────────────────────────────────────────────────────────


BASE_NFCGATE = ["nfcgate", "--session", "42", "--role", "reader"]


def test_config_nfcgate_sets_server_session_and_role(runner, use_link):
    link = use_link(nfc, FakeLink())
    result = runner.invoke(config, BASE_NFCGATE + ["--server", "192.168.1.5"])

    assert result.exit_code == 0
    assert link.sent[:4] == [
        "set server 192.168.1.5",
        "set session 42",
        "set role reader",
        "save",
    ]


def test_config_nfcgate_splits_host_and_port(runner, use_link):
    link = use_link(nfc, FakeLink())
    runner.invoke(config, BASE_NFCGATE + ["--server", "192.168.1.5:9999"])

    assert link.sent[:2] == ["set server 192.168.1.5", "set port 9999"]


@pytest.mark.parametrize("bad", ["host:0", "host:70000", "host:abc"])
def test_config_nfcgate_rejects_an_invalid_port(runner, use_link, bad):
    link = use_link(nfc, FakeLink())
    result = runner.invoke(config, BASE_NFCGATE + ["--server", bad])

    assert result.exit_code == 1
    assert "invalid port in --server" in flat(result.output)
    assert link.sent == []  # rejected before touching the board


def test_config_nfcgate_rejects_a_server_value_with_no_host(runner, use_link):
    link = use_link(nfc, FakeLink())
    result = runner.invoke(config, BASE_NFCGATE + ["--server", ":5566"])

    assert result.exit_code == 1
    assert "missing host" in flat(result.output)
    assert link.sent == []  # rejected before touching the board


@pytest.mark.parametrize("session", ["0", "256"])
def test_config_nfcgate_rejects_a_session_outside_1_255(runner, session):
    result = runner.invoke(
        config,
        ["nfcgate", "--server", "h", "--role", "card", "--session", session],
    )
    assert result.exit_code == 2


def test_config_nfcgate_rejects_an_unknown_role(runner):
    result = runner.invoke(
        config, ["nfcgate", "--server", "h", "--session", "1", "--role", "sniffer"]
    )
    assert result.exit_code == 2


# ── config show ──────────────────────────────────────────────────────────────


def test_config_show_renders_the_devices_configuration(runner, use_link):
    use_link(
        nfc,
        FakeLink({"info": ok(fw="0.8.0", role="reader", ssid="HomeNet", state="idle")}),
    )
    result = runner.invoke(config, ["show"])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "0.8.0" in out and "reader" in out and "HomeNet" in out


def test_config_show_reports_a_failed_info(runner, use_link):
    use_link(nfc, FakeLink({"info": err("busy")}))
    result = runner.invoke(config, ["show"])

    assert result.exit_code == 1
    assert "info failed: busy" in flat(result.output)


# ── run ──────────────────────────────────────────────────────────────────────


def test_run_waits_for_the_relay_to_come_up(runner, use_link):
    link = use_link(
        nfc,
        FakeLink(
            {
                "run": ok("accepted"),
                "status": [
                    ok(state="connecting", detail="associating WiFi"),
                    ok(state="relaying"),
                ],
            }
        ),
    )
    result = runner.invoke(run_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "associating WiFi" in out  # progress detail is surfaced once
    assert "relay started on /dev/fake0" in out
    assert link.sent[0] == "run"


def test_run_reports_a_rejected_request(runner, use_link):
    use_link(nfc, FakeLink({"run": err("no ssid configured")}))
    result = runner.invoke(run_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 1
    assert "relay rejected 'run': no ssid configured" in out
    assert "bombercat config show" in out


def test_run_reports_a_link_error_on_the_request(runner, use_link):
    use_link(nfc, FakeLink({"run": DeviceError("timed out waiting for a reply")}))
    result = runner.invoke(run_cmd, [])

    assert result.exit_code == 1
    assert "device did not accept 'run'" in flat(result.output)


def test_run_reports_a_bringup_failure_with_its_detail(runner, use_link):
    use_link(
        nfc,
        FakeLink({"status": ok(state="error", detail="wifi auth failed")}),
    )
    result = runner.invoke(run_cmd, [])

    assert result.exit_code == 1
    assert "relay failed to start: wifi auth failed" in flat(result.output)


def test_run_keeps_polling_through_a_transient_status_timeout(runner, use_link):
    """A bring-up phase can briefly occupy the firmware; that is not a failure."""
    use_link(
        nfc,
        FakeLink({"status": [DeviceError("timed out"), ok(state="relaying")]}),
    )
    result = runner.invoke(run_cmd, [])

    assert result.exit_code == 0
    assert "relay started" in flat(result.output)


def test_run_aborts_immediately_on_a_lost_link(runner, use_link):
    """A board that was unplugged fails every status poll the same way — abort
    instead of burning the whole 45s bring-up budget on a dead link (M18)."""
    use_link(nfc, FakeLink({"status": DeviceError("serial link lost: device gone")}))
    result = runner.invoke(run_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 1
    assert "lost contact" in out
    assert "plugged in" in out


def test_run_gives_up_after_consecutive_status_failures(runner, use_link):
    """Transient timeouts during bring-up are fine one at a time, but not
    forever — cap consecutive failures instead of polling to the full budget
    (M18)."""
    use_link(nfc, FakeLink({"status": DeviceError("timed out")}))
    result = runner.invoke(run_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 1
    assert "stopped responding to status polls" in out


def test_run_gives_up_after_the_bringup_budget(runner, use_link, monkeypatch):
    monkeypatch.setattr(nfc, "_RUN_BRINGUP_TIMEOUT", 0.0)
    use_link(nfc, FakeLink({"status": ok(state="connecting")}))
    result = runner.invoke(run_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 1
    assert "did not reach 'relaying' in time" in out
    assert "still responsive" in out  # the device is not wedged; keep looking


# ── stop / status ────────────────────────────────────────────────────────────


def test_stop_confirms_the_relay_stopped(runner, use_link):
    link = use_link(nfc, FakeLink())
    result = runner.invoke(stop_cmd, [])

    assert result.exit_code == 0
    assert link.sent == ["stop"]
    assert "relay stopped on /dev/fake0" in flat(result.output)


def test_stop_reports_a_refusal(runner, use_link):
    use_link(nfc, FakeLink({"stop": err("not running")}))
    result = runner.invoke(stop_cmd, [])

    assert result.exit_code == 1
    assert "stop failed: not running" in flat(result.output)


def test_status_renders_the_live_relay_state(runner, use_link):
    use_link(
        nfc,
        FakeLink(
            {"status": ok(state="relaying", connected="1", peer="0", relayed="7")}
        ),
    )
    result = runner.invoke(status_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "relaying" in out
    assert "yes" in out and "no" in out  # connected -> yes, peer -> no
    assert "7" in out


def test_status_reports_a_failure(runner, use_link):
    use_link(nfc, FakeLink({"status": err("busy")}))
    result = runner.invoke(status_cmd, [])

    assert result.exit_code == 1
    assert "status failed: busy" in flat(result.output)


# ── monitor ──────────────────────────────────────────────────────────────────


def test_monitor_streams_and_restores_the_log_level(runner, use_link):
    link = use_link(
        nfc,
        FakeLink(
            stream_lines=[
                "RelayEngine cmd: 00a404",
                "-ERR nfc init failed",
                ":state relaying",
                "",  # blank lines are skipped
                "plain log line",
            ]
        ),
    )
    result = runner.invoke(monitor_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert link.sent[0] == "loglevel 4"  # raise verbosity while monitoring…
    assert link.sent[-1] == "loglevel 2"  # …and leave the hot path silent again
    assert "cmd: 00a404" in out and "plain log line" in out


def test_monitor_survives_a_device_line_with_malformed_markup(runner, use_link):
    """A device log line containing something that looks like a stray Rich
    closing tag must not crash the live stream (M16)."""
    use_link(nfc, FakeLink(stream_lines=["stray closing tag [/oops] in device log"]))
    result = runner.invoke(monitor_cmd, [], catch_exceptions=False)

    assert result.exit_code == 0
    assert "stray closing tag" in flat(result.output)


def test_monitor_exits_cleanly_on_ctrl_c(runner, use_link):
    def _lines():
        yield "RelayEngine: alive"
        raise KeyboardInterrupt

    link = use_link(nfc, FakeLink(stream_lines=_lines()))
    result = runner.invoke(monitor_cmd, [])

    assert result.exit_code == 0
    assert "stopped" in flat(result.output)
    assert link.sent[-1] == "loglevel 2"


def test_monitor_works_on_firmware_without_loglevel(runner, use_link):
    """Old firmware replies -ERR unknown command; that's ignorable, not a
    real problem (M17)."""
    use_link(
        nfc,
        FakeLink(
            {"loglevel": err("unknown command")},
            stream_lines=["RelayEngine: alive"],
        ),
    )
    result = runner.invoke(monitor_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "alive" in out
    assert "log level" not in out  # ignorable case stays silent


def test_monitor_warns_on_a_real_loglevel_failure(runner, use_link):
    """A genuine link problem while setting loglevel must not vanish
    silently like the old bare `except Exception: pass` did (M17)."""
    use_link(
        nfc,
        FakeLink(
            {"loglevel": DeviceError("serial link lost")},
            stream_lines=["RelayEngine: alive"],
        ),
    )
    result = runner.invoke(monitor_cmd, [])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "could not raise log level" in out
    assert "alive" in out


# ── relay group wiring ───────────────────────────────────────────────────────
# The commands above are also reachable through the `relay` group; these prove
# the subcommands are wired and run their real logic when invoked that way.


def test_relay_group_exposes_every_subcommand():
    assert set(relay.commands) == {"config", "run", "stop", "status", "monitor"}


def test_relay_status_runs_through_the_group(runner, use_link):
    use_link(
        nfc,
        FakeLink(
            {"status": ok(state="relaying", connected="1", peer="0", relayed="3")}
        ),
    )
    result = runner.invoke(relay, ["status"])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "relaying" in out and "3" in out


def test_relay_run_runs_through_the_group(runner, use_link):
    link = use_link(
        nfc, FakeLink({"run": ok("accepted"), "status": ok(state="relaying")})
    )
    result = runner.invoke(relay, ["run"])

    assert result.exit_code == 0
    assert link.sent[0] == "run"
    assert "relay started" in flat(result.output)


def test_relay_config_show_runs_through_the_group(runner, use_link):
    use_link(nfc, FakeLink({"info": ok(fw="0.9.7", role="reader")}))
    result = runner.invoke(relay, ["config", "show"])

    assert result.exit_code == 0
    assert "0.9.7" in flat(result.output)
