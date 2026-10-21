#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

# `bombercat tags mifare results` — the results the board stored while it
# worked on its own (Plan de Implementación Firmware MIFARE, Fase 4, tarea
# 4.6).
#
# The standalone workflow reads a card with no PC attached, so the dump goes
# out over the serial port to nobody. The firmware keeps the last results in
# its own flash; this command lists them. Fetch one with
# `bombercat tags mifare dump --last` (which rebuilds the canonical dump, so
# `mifare analyze` can report on it).

import json

import click
from rich.table import Table

from ...utils.cli_options import device_options
from ...utils.detection_cli import verbosity as _verbosity
from ...utils.output import (
    console,
    make_tracer,
    print_error,
    print_info,
    print_success,
)
from .device_results import (
    DeviceResultsError,
    DeviceResultsUnsupported,
    run_device_results,
)
from .session import _mifare_session


@click.command("results")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the listing as one JSON object.",
)
@device_options
@click.pass_context
def mifare_results_cmd(ctx, as_json, verbose, port, device_id):
    """List the results the board stored while working on its own. No card needed.

    In standalone mode the board reads a card by itself — sweep, dump, emit —
    and nobody is listening to the serial port in the field. The firmware
    keeps the last results in its own flash, so they can be collected later,
    over USB, when the board is plugged into a PC.

    Each result says how many sectors were read and how many were NOT (the
    gaps are declared, never assumed safe). The board has no real-time
    clock, so the timestamp is its own uptime: "how long after the boot the
    card was read".
    """
    level = _verbosity(ctx, verbose)
    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        try:
            results = run_device_results(link)
        except DeviceResultsUnsupported as e:
            print_error(
                f"the flashed firmware does not keep results ({e}) — update "
                "the firmware"
            )
            raise SystemExit(1)
        except DeviceResultsError as e:
            print_error(f"results failed: {e}")
            raise SystemExit(1)

    if as_json:
        print(json.dumps(results.to_dict()))
        return

    if results.empty:
        print_info(
            f"no stored results @ {target} — the board has not read a card on "
            "its own yet"
        )
        return

    print_success(f"stored results @ {target} ({results.used}/{results.slots} slots)")
    table = Table(header_style="cyan bold")
    table.add_column("#", justify="right")
    table.add_column("uid")
    table.add_column("sectors", justify="right")
    table.add_column("gaps", justify="right")
    table.add_column("uptime", justify="right")
    for r in results.results:
        table.add_row(
            str(r.index),
            r.uid or "—",
            f"{r.read}/{r.sectors}",
            str(r.gaps),
            f"{r.uptime_s:.1f}s",
        )
    console.print(table)
    print_info(
        "fetch one with `bombercat tags mifare dump --last` — the dump is "
        "rebuilt with its gaps, so `mifare analyze` can report on it"
    )
