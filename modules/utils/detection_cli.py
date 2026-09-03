#!/usr/bin/env python3

# Electronic Cats
# detection_cli.py — shared plumbing for the REPL-driven detection CLIs
# (`bombercat tags`, `bombercat readers`): the device session context
# manager, the root/local `-v` merge, CSV/JSON export writers, the
# CSV-formula-injection guard, and the overwrite guard. Domain-specific bits
# (parser, aggregator, per-tag/per-reader field layout, table rendering) stay
# in each module's own cli.py.
# Distributed as-is; no warranty is given.

import csv
import json
import os
from contextlib import contextmanager
from typing import Callable, Dict, Iterator, List, Optional, Tuple

import serial

from ..core.bombercat import DeviceError
from .output import console, print_error, print_info

_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


def verbosity(ctx, local: int) -> int:
    """Combine the root `-v` (before the verb) with a command's own `-v`
    (after it) — either position means the same thing, so the higher count
    wins rather than the two adding up."""
    root = (ctx.obj or {}).get("verbose", 0)
    return max(root, local)


@contextmanager
def device_session(
    resolve_port_fn: Callable,
    device_link_cls: Callable,
    command_name: str,
    firmware_name: str,
    port: Optional[str],
    device_id: Optional[int] = None,
    trace=None,
) -> Iterator[Tuple[str, object]]:
    """Open a verified link for a detection command group, yield
    ``(target, link)``, and always close it.

    `resolve_port_fn`/`device_link_cls` are taken as arguments rather than
    imported here so that a caller module's own `resolve_port`/`DeviceLink`
    names — the ones tests monkeypatch — are what actually get called.
    """
    link = None
    try:
        target = resolve_port_fn(port, device_id)
        link = device_link_cls(target, trace=trace).open()
        if not link.ping():
            print_error(
                f"{target} did not answer the handshake. "
                f"`{command_name}` needs the {firmware_name} firmware — check "
                "what's flashed with:  bombercat status"
            )
            raise SystemExit(1)
        yield target, link
    except DeviceError as e:
        print_error(str(e))
        raise SystemExit(1)
    except (serial.SerialException, OSError) as e:
        print_error(f"{type(e).__name__}: {e}")
        raise SystemExit(1)
    finally:
        if link is not None:
            link.close()


def print_field(label: str, value: str) -> None:
    console.print(f"  [cyan]{label:<13}[/cyan] {value}")


def write_json(path: str, rows: List[Dict[str, object]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)
        f.write("\n")


def csv_safe(value: object) -> object:
    """Neutralize CSV/formula injection (device-controlled fields are free
    text): a cell starting with =/+/-/@ is interpreted as a formula by
    Excel/LibreOffice on open."""
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def write_csv(path: str, rows: List[Dict[str, object]], fieldnames: List[str]) -> None:
    """`fieldnames` is the caller's preferred column order; any extra keys
    present in `rows` (e.g. `x_`-prefixed extras) are appended in
    first-seen order."""
    fieldnames = list(fieldnames)
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: csv_safe(v) for k, v in row.items()})


def refuse_overwrite(path: str, force: bool) -> None:
    """These outputs are audit evidence (a scan export may reflect live
    device data) — refuse a silent overwrite unless --force."""
    if not force and os.path.exists(path):
        print_error(f"{path} already exists — pass --force to overwrite")
        raise SystemExit(1)


def write_export(path: str, rows: List[Dict[str, object]], writer: Callable) -> None:
    try:
        writer(path, rows)
    except OSError as e:
        print_error(f"could not write {path}: {e}")
        raise SystemExit(1)
    print_info(f"wrote {path}")
