#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

# `bombercat tags mifare ...` — Mifare Classic block access over the
# MifareClassic firmware's REPL (bombercat-firmware's MIFARE_CLASSIC_PLAN.md
# §3.1/Phase 3, CLI side is §4/Phase 4).
#
# This module owns the `mifare` group and its short, single-block commands
# (auth/read/write/sector/keys); the bigger ones live one per module and are
# registered at the bottom of this file. `auth`/`read`/`write`/`sector` need a
# card already selected in the firmware's interactive "mifare session", which
# it opens on its own right after tapping a Mifare Classic card (see
# MifareClassic.ino's handleTagDetected()); rather than just failing when no
# card has been tapped yet, each of those commands asks the user to tap one and
# waits for the firmware's auto-probe ':mifare' event (proof the session is now
# open) before retrying once. `check`, `dump` and `restore` open a session
# themselves the same way.

import json
from typing import Optional

import click
from rich.table import Table

from ...utils.cli_options import device_options
from ...utils.detection_cli import (
    print_field as _print_field,
    verbosity as _verbosity,
)
from ...utils.output import (
    console,
    make_tracer,
    print_dim,
    print_error,
    print_info,
    print_success,
)
from .analyze import mifare_analyze_cmd
from .block0 import parse_block0
from .check import mifare_check_cmd
from .clone_uid import mifare_clone_uid_cmd
from .code import mifare_code_cmd
from .common import (
    _MIFARE_BLOCK_HEX_LEN,
    _MIFARE_KEY_HEX_LEN,
    _load_sector_key_pair,
    _mifare_validate_hex,
    _sector_first_block,
)
from .display import _sector_display_lines, _print_access_bits, _print_block0
from .device_sweep import parse_device_trigger
from .dump import mifare_dump_cmd
from .restore import mifare_restore_cmd
from .session import (
    _MIFARE_KEY_TYPE_OPTION,
    _MIFARE_TIMEOUT_OPTION,
    _mifare_session,
    _read_one_sector,
    _run_mifare_command,
)
from .write_text import mifare_write_text_cmd


@click.group("mifare")
def mifare():
    """Mifare Classic auth/read/write/sector commands (requires the
    MifareClassic firmware).

    Tap a Mifare Classic card to let the firmware select it — `auth`, `read`,
    `write` and `sector` all wait for this automatically if no card is
    selected yet. The card then stays selected for ~10s of inactivity between
    commands before the firmware closes the session and re-arms discovery.
    """


@mifare.command("auth")
@click.option(
    "--block", type=click.IntRange(0, 255), required=True, help="Block number."
)
@_MIFARE_KEY_TYPE_OPTION
@click.option("--key", required=True, metavar="HEX12", help="6-byte key, 12 hex chars.")
@_MIFARE_TIMEOUT_OPTION
@device_options
@click.pass_context
def mifare_auth_cmd(ctx, block, key_type, key, timeout, verbose, port, device_id):
    """Authenticate BLOCK's sector so a following `read`/`write` can access it."""
    err = _mifare_validate_hex(key, _MIFARE_KEY_HEX_LEN, "--key")
    if err:
        print_error(err)
        raise SystemExit(1)

    level = _verbosity(ctx, verbose)
    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = _run_mifare_command(
            link, f"mifare auth {block} {key_type.upper()} {key.upper()}", timeout
        )

    if not r.ok:
        print_error(f"auth failed: {r.message}")
        raise SystemExit(1)
    print_success(f"authenticated block {block} with key {key_type.upper()}")


@mifare.command("read")
@click.option(
    "--block", type=click.IntRange(0, 255), required=True, help="Block number."
)
@click.option(
    "--json", "as_json", is_flag=True, help='Emit {"block": ..., "data": ...}.'
)
@_MIFARE_TIMEOUT_OPTION
@device_options
@click.pass_context
def mifare_read_cmd(ctx, block, as_json, timeout, verbose, port, device_id):
    """Read BLOCK from its already-authenticated sector (run `auth` first)."""
    level = _verbosity(ctx, verbose)
    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = _run_mifare_command(link, f"mifare read {block}", timeout)

    if not r.ok:
        print_error(f"read failed: {r.message}")
        raise SystemExit(1)
    _, _, data_hex = r.data.get("mifare_data", "").partition(" ")
    b0 = parse_block0(data_hex) if block == 0 else None
    if as_json:
        out = {"block": block, "data": data_hex}
        if b0 is not None:
            out["block0"] = b0.to_dict()
        print(json.dumps(out))
        return
    console.print("")
    _print_field("block", str(block))
    _print_field("data", data_hex or "[dim]—[/dim]")
    if b0 is not None:
        _print_block0(b0)


@mifare.command("write")
@click.option(
    "--block", type=click.IntRange(0, 255), required=True, help="Block number."
)
@click.option(
    "--data", required=True, metavar="HEX32", help="16-byte block, 32 hex chars."
)
@_MIFARE_TIMEOUT_OPTION
@device_options
@click.pass_context
def mifare_write_cmd(ctx, block, data, timeout, verbose, port, device_id):
    """Write DATA to BLOCK in its already-authenticated sector (run `auth` first)."""
    err = _mifare_validate_hex(data, _MIFARE_BLOCK_HEX_LEN, "--data")
    if err:
        print_error(err)
        raise SystemExit(1)

    level = _verbosity(ctx, verbose)
    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = _run_mifare_command(link, f"mifare write {block} {data.upper()}", timeout)

    if not r.ok:
        print_error(f"write failed: {r.message}")
        raise SystemExit(1)
    print_success(f"wrote block {block}")


@mifare.command("sector")
@click.option(
    "--sector", type=click.IntRange(0, 255), required=True, help="Sector number."
)
@_MIFARE_KEY_TYPE_OPTION
@click.option("--key", metavar="HEX12", help="6-byte key, 12 hex chars.")
@click.option(
    "-k",
    "--keys-file",
    "keys_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    metavar="FILE",
    help="A `sector:keyA:keyB` file from `mifare check --output-keys`. Uses "
    "the sector's key A (falling back to key B) instead of --key, and shows "
    "the real keys in the trailer line.",
)
@click.option(
    "--json", "as_json", is_flag=True, help='Emit {"sector": ..., "data": ...}.'
)
@_MIFARE_TIMEOUT_OPTION
@device_options
@click.pass_context
def mifare_sector_cmd(
    ctx, sector, key_type, key, keys_file, as_json, timeout, verbose, port, device_id
):
    """Authenticate and read every block of SECTOR in one call (self-contained).

    Pass either --key (a single 12-hex key of --key-type) or --keys-file (a
    `sector:keyA:keyB` file from `mifare check --output-keys`, which tries the
    sector's key A then key B and shows the real keys in the trailer line).
    """
    if keys_file and key:
        print_error("pass either --key or --keys-file, not both")
        raise SystemExit(1)
    if not keys_file and not key:
        print_error("one of --key or --keys-file is required")
        raise SystemExit(1)

    file_key_a: Optional[str] = None
    file_key_b: Optional[str] = None
    if keys_file:
        file_key_a, file_key_b = _load_sector_key_pair(keys_file, sector)
        key_a, key_b = file_key_a, file_key_b
        if not key_a and not key_b:
            print_error(f"sector {sector} has no key A or key B in {keys_file}")
            raise SystemExit(1)
    else:
        err = _mifare_validate_hex(key, _MIFARE_KEY_HEX_LEN, "--key")
        if err:
            print_error(err)
            raise SystemExit(1)
        key_a = key.upper() if key_type.upper() == "A" else None
        key_b = key.upper() if key_type.upper() == "B" else None

    level = _verbosity(ctx, verbose)
    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        data_hex, _used_kt, reason = _read_one_sector(
            link, sector, key_a, key_b, timeout, first=True
        )

    if reason != "ok":
        # reason is already a complete sentence from the firmware (e.g.
        # "authentication failed" or "sector read failed: key authenticated
        # but a block read was denied (access bits)") — don't re-wrap it in
        # another "sector read failed:" prefix, or the two collide into
        # nonsense like "sector read failed: sector read failed".
        print_error(f"sector {sector}: {reason}")
        raise SystemExit(1)
    assert data_hex is not None
    b0 = parse_block0(data_hex[:32]) if sector == 0 else None
    if as_json:
        out = {"sector": sector, "data": data_hex}
        if b0 is not None:
            out["block0"] = b0.to_dict()
        print(json.dumps(out))
        return
    console.print("")
    _print_field("sector", str(sector))
    if data_hex:
        lines, access = _sector_display_lines(sector, data_hex, file_key_a, file_key_b)
        for label, line in lines:
            _print_field(label, line)
        if access is not None:
            _print_access_bits(access, _sector_first_block(sector))
    else:
        console.print("  [dim]—[/dim]")
    if b0 is not None:
        _print_block0(b0)


@mifare.command("keys")
@click.option("--json", "as_json", is_flag=True, help="Emit one JSON object per key.")
@device_options
@click.pass_context
def mifare_keys_cmd(ctx, as_json, verbose, port, device_id):
    """List the firmware's built-in default keys. No card needed."""
    level = _verbosity(ctx, verbose)
    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = link.command("mifare keys")

    if not r.ok:
        print_error(f"keys failed: {r.message}")
        raise SystemExit(1)

    keys = []
    i = 0
    while f"mifare_key{i}" in r.data:
        name, _, hex_value = r.data[f"mifare_key{i}"].partition(" ")
        keys.append((name, hex_value))
        i += 1

    if as_json:
        for name, hex_value in keys:
            print(json.dumps({"name": name, "key": hex_value}))
        return

    if not keys:
        print_dim("no keys reported")
        return
    table = Table(
        title=f"MifareClassic default keys @ {target}", header_style="cyan bold"
    )
    table.add_column("name", style="cyan")
    table.add_column("key")
    for name, hex_value in keys:
        table.add_row(name, hex_value)
    console.print(table)


@mifare.command("timing")
@click.option(
    "--reset",
    "reset",
    is_flag=True,
    help="Zero the board's counters first, so the numbers describe only what "
    "happens from now on.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit one JSON object.")
@device_options
@click.pass_context
def mifare_timing_cmd(ctx, reset, as_json, verbose, port, device_id):
    """Show the board's own wall-clock and detection counters.

    The board measures itself — the time inside each authentication, inside
    each re-select, and how its card detection behaved — so the two sweep
    paths (host-driven and on-device) can be compared without a stopwatch.

    The detection counters are the ones that tell a "the card left the field"
    from a "the re-select did not get the card back": `det_disc` (the chip
    found a card and waited for the host to select it), `det_deact` (the card
    left), `det_err` (the chip refused a command) and `rst_fail` (the reader
    re-arm failed on I²C).
    """
    level = _verbosity(ctx, verbose)
    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        if reset:
            link.command("mifare timing reset")
        r = link.command("mifare timing")

    if not r.ok:
        print_error(f"timing failed: {r.message}")
        raise SystemExit(1)

    raw = r.data.get("mifare_timing", "") or ""
    counters = {}
    for token in raw.split():
        name, _, value = token.partition("=")
        if name and value.isdigit():
            counters[name] = int(value)

    if as_json:
        print(json.dumps({"timing": counters}))
        return

    print_success(f"timing @ {target}")
    table = Table(header_style="cyan bold")
    table.add_column("counter", style="cyan")
    table.add_column("value", justify="right")
    for name, value in counters.items():
        table.add_row(name, str(value))
    console.print(table)


@mifare.command("standalone")
@click.option(
    "--on",
    "state",
    flag_value="on",
    default=None,
    help="Turn the standalone workflow on: the board runs the whole field "
    "workflow by itself when a card is detected (sweep, dump the sectors it "
    "cracked, emit everything over serial).",
)
@click.option(
    "--off",
    "state",
    flag_value="off",
    help="Turn the standalone workflow off (the board waits for the CLI).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit one JSON object.")
@device_options
@click.pass_context
def mifare_standalone_cmd(ctx, state, as_json, verbose, port, device_id):
    """Show or set the board's standalone mode. No card needed.

    With the standalone mode on, the firmware does the whole job on its own
    when a card is detected — dictionary sweep, dump of the sectors it
    cracked, everything emitted over the serial port — so it can be used in
    the field with no PC attached. The setting is stored in the board's flash
    and survives a reset.
    """
    level = _verbosity(ctx, verbose)
    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        command = "mifare standalone"
        if state:
            command += f" {state}"
        r = link.command(command)

    if not r.ok:
        print_error(f"standalone failed: {r.message}")
        raise SystemExit(1)

    current = (r.message or "").strip()
    if current.startswith("standalone "):
        current = current[len("standalone ") :].strip()
    if as_json:
        print(json.dumps({"standalone": current}))
        return
    print_success(f"standalone mode @ {target}: {current}")


@mifare.command("trigger")
@click.option("--json", "as_json", is_flag=True, help="Emit one JSON object.")
@device_options
@click.pass_context
def mifare_trigger_cmd(ctx, as_json, verbose, port, device_id):
    """Show the board's field trigger: the button that starts the standalone
    workflow. No card needed.

    On the BomberCat board the user button (SW2, "GPIO" in the BOM, on RP2040
    GPIO17 = Arduino D5) speaks two gestures:

    • short press — run the standalone chain for the card in the field (dump
      the sectors the sweep cracked and emit them). With no card in the field
      it arms the run for the next card that is tapped.
    • long press — toggle the persistent standalone mode (the chain runs on
      every detection). The mode is stored in the board's flash, so it
      survives a reset; `mifare standalone --on/--off` sets the same flag.

    The board's LED says what happened: one blink for a short press, two for
    the standalone mode armed, three for it disarmed.
    """
    level = _verbosity(ctx, verbose)
    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        r = link.command("mifare trigger")

    if not r.ok:
        print_error(f"trigger failed: {r.message}")
        raise SystemExit(1)

    state = parse_device_trigger(r.data)
    if not state:
        print_error("the board did not report any trigger state")
        raise SystemExit(1)

    if as_json:
        print(json.dumps({"trigger": state}))
        return

    print_success(f"field trigger @ {target}")
    table = Table(header_style="cyan bold")
    table.add_column("field", style="cyan")
    table.add_column("value", justify="right")
    for name, value in state.items():
        table.add_row(name, str(value))
    console.print(table)
    if state.get("standalone"):
        print_info("the standalone mode is ON — the board runs the whole job by itself")
    elif state.get("armed"):
        print_info("a one-shot run is armed — it fires with the next card")
    else:
        print_info("the standalone mode is off — press the board's button to run it")


# ── the rest of the group ────────────────────────────────────────────────────
#
# Commands big enough to own a module. Each is a plain `click.Command`, so
# registering them here — rather than having each decorate the group — keeps
# the imports one-way: this module knows about the parts, the parts don't need
# to know about the group.
mifare.add_command(mifare_check_cmd)
mifare.add_command(mifare_dump_cmd)
mifare.add_command(mifare_analyze_cmd)
mifare.add_command(mifare_restore_cmd)
mifare.add_command(mifare_clone_uid_cmd)
mifare.add_command(mifare_code_cmd)
mifare.add_command(mifare_write_text_cmd)
