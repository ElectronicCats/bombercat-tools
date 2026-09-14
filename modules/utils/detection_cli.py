#!/usr/bin/env python3

# Electronic Cats
# detection_cli.py — shared plumbing for the REPL-driven detection CLIs
# (`bombercat tags`, `bombercat readers`): the device session context
# manager, the root/local `-v` merge, CSV/JSON export writers, the
# CSV-formula-injection guard, the overwrite guard, and
# build_detection_group() — which assembles the read/watch/scan/info command
# group itself from a DetectionSpec. Domain-specific bits (parser, aggregator,
# per-tag/per-reader field layout, table rendering) are supplied by each
# module's own cli.py via that spec.
# Distributed as-is; no warranty is given.

import csv
import json
import os
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, Pattern, Tuple

import click
import serial
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ..core.bombercat import DeviceError
from ..core.ensure_firmware import ensure_firmware, resolve_policy
from ..core.firmwares import resolve_status_port
from ..core.requirements import requirement_for
from .cli_options import device_options
from .output import (
    console,
    make_tracer,
    print_dim,
    print_error,
    print_info,
    print_warning,
)

_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")

# How long `<group> info` listens for a structured event before concluding
# the firmware isn't emitting one (yet, or ever, depending on the spec).
_INFO_PROBE_SECONDS = 2.0


def verbosity(ctx, local: int) -> int:
    """Combine the root `-v` (before the verb) with a command's own `-v`
    (after it) — either position means the same thing, so the higher count
    wins rather than the two adding up."""
    root = (ctx.obj or {}).get("verbose", 0)
    return max(root, local)


def _policy_flag() -> Optional[bool]:
    """The root `--auto-flash`/`--no-auto-flash` value, or None if unset.

    Read from the Click context rather than a function argument so
    `device_session` doesn't need every command to thread the flag through
    (docs/AUTOFLASH_PLAN.md F4).
    """
    ctx = click.get_current_context(silent=True)
    if ctx is None:
        return None
    return (ctx.obj or {}).get("auto_flash")


@contextmanager
def device_session(
    resolve_port_fn: Callable,
    device_link_cls: Callable,
    command_name: str,
    firmware_name: str,
    port: Optional[str],
    device_id: Optional[int] = None,
    trace=None,
    *,
    requires: Optional[str] = None,
) -> Iterator[Tuple[str, object]]:
    """Open a verified link for a detection command group, yield
    ``(target, link)``, and always close it.

    `resolve_port_fn`/`device_link_cls` are taken as arguments rather than
    imported here so that a caller module's own `resolve_port`/`DeviceLink`
    names — the ones tests monkeypatch — are what actually get called.

    `requires`, when given a capability (docs/AUTOFLASH_PLAN.md D-3.1), makes
    this ensure the board can do it before opening the command's own link —
    detecting with `resolve_status_port`/`detect_firmware` (no handshake
    required, D-3.3/D7) and flashing it if missing and the auto-flash policy
    (root `--auto-flash`, read via `_policy_flag`) allows it. `requires=None`
    (the default) keeps today's behavior byte-for-byte: no caller passes it
    yet — see docs/AUTOFLASH_PLAN.md F4.
    """
    link = None
    try:
        if requires is not None:
            target, usb_tagged = resolve_status_port(port, device_id)
            req = requirement_for(requires, command_name)
            outcome = ensure_firmware(
                target, usb_tagged, req, policy=resolve_policy(_policy_flag())
            )
            target = outcome.port
        else:
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


# ── build_detection_group ───────────────────────────────────────────────────
#
# `tags` and `readers` are the same command group twice: read one detection,
# watch a stream of them, scan+aggregate for a while, report firmware info.
# Everything domain-shaped (the parser, the aggregator, how one detection
# turns into JSON/human fields/a table row) is supplied by the caller's
# DetectionSpec; this function owns the four commands' actual control flow.


@dataclass
class DetectionSpec:
    module: Any  # the calling cli.py module — resolve_port/DeviceLink/_write_json
    # are looked up on it *by name* at call time (not captured here) so tests
    # can keep monkeypatching e.g. `tagscli.resolve_port`.
    name: str
    group_help: str
    firmware_name: str
    noun: str
    plural_noun: str
    parser_cls: Callable[[], Any]
    aggregator_cls: Callable[[], Any]
    to_dict: Callable[[Any], Dict[str, object]]
    emit_header: str
    emit_fields: Callable[[Any], None]
    watch_line: Callable[[Any, int], str]
    dedupe_key: Callable[[Any], str]
    seen_again_label: Callable[[Any], str]
    watch_unit: str
    dedupe_help: str
    quiet_noise_help: str
    noise_re: Pattern
    csv_fields: List[str]
    # (column header, row -> cell text, justify-or-None)
    table_columns: List[Tuple[str, Callable[[Dict[str, object]], str], Optional[str]]]
    read_summary: str
    info_summary: str
    info_events: Callable[[Any], str]
    dedupe_cap_attr: Optional[str] = None
    legacy_csv_json_aliases: bool = False
    # Capability the board must have to run this group's commands, or None to
    # skip the auto-flash check entirely (docs/AUTOFLASH_PLAN.md D-3.1). Not
    # yet set by `tags`/`readers` — wiring a real value here is F4b.
    requires: Optional[str] = None


def build_detection_group(spec: DetectionSpec) -> click.Group:
    module = spec.module

    @contextmanager
    def _session(port, device_id=None, trace=None):
        with device_session(
            module.resolve_port,
            module.DeviceLink,
            spec.name,
            spec.firmware_name,
            port,
            device_id,
            trace,
            requires=spec.requires,
        ) as pair:
            yield pair

    def _emit(item, as_json: bool) -> None:
        if as_json:
            print(json.dumps(spec.to_dict(item)))
            return
        console.print("")
        console.print(f"  [green bold]{spec.emit_header}[/green bold]")
        spec.emit_fields(item)

    group = click.Group(name=spec.name, help=spec.group_help)

    # ── read ─────────────────────────────────────────────────────────────
    def _read(ctx, timeout, as_json, verbose, port, device_id):
        level = verbosity(ctx, verbose)
        item = None
        with _session(port, device_id, trace=make_tracer(level)) as (target, link):
            if not as_json:
                print_info(f"Waiting for a {spec.noun} on {target} — Ctrl-C to abort")
            parser = spec.parser_cls()
            deadline = time.monotonic() + timeout
            try:
                for line in link.stream(yield_empty=True):
                    if line:
                        item = parser.feed(line)
                        if item is not None:
                            break
                    if time.monotonic() > deadline:
                        break
            except KeyboardInterrupt:
                print_warning("aborted")
                raise SystemExit(1)

        if item is None:
            print_error(f"no {spec.noun} detected in {timeout:g}s")
            raise SystemExit(1)
        _emit(item, as_json)

    read_fn = click.pass_context(_read)
    read_fn = device_options(read_fn)
    read_fn = click.option(
        "--json", "as_json", is_flag=True, help="Emit one JSON object on stdout."
    )(read_fn)
    read_fn = click.option(
        "-t",
        "--timeout",
        default=15.0,
        show_default=True,
        help=f"Seconds to wait for a {spec.noun}.",
    )(read_fn)
    read_fn.__doc__ = (
        f"{spec.read_summary}\n\n"
        f"    Exit code: 0 {spec.noun} read, 1 timeout or link error."
    )
    group.command("read")(read_fn)

    # ── watch ────────────────────────────────────────────────────────────
    def _watch(ctx, dedupe, quiet_noise, as_json, verbose, port, device_id):
        level = verbosity(ctx, verbose)
        detections = 0
        counts: "OrderedDict[str, int]" = OrderedDict()
        capped = False
        start = time.monotonic()
        max_keys = (
            getattr(module, spec.dedupe_cap_attr) if spec.dedupe_cap_attr else None
        )
        with _session(port, device_id, trace=make_tracer(level)) as (target, link):
            if not as_json:
                print_info(f"Watching {target} — Ctrl-C to stop")
            parser = spec.parser_cls()
            try:
                for line in link.stream():
                    if not line:
                        continue
                    item = parser.feed(line)
                    if item is None:
                        if (
                            not quiet_noise
                            and not as_json
                            and spec.noise_re.match(line.strip())
                        ):
                            console.print(f"[dim]{line}[/dim]")
                        continue

                    detections += 1
                    key = spec.dedupe_key(item)
                    if (
                        max_keys is not None
                        and key not in counts
                        and len(counts) >= max_keys
                    ):
                        counts.popitem(last=False)
                        if not capped:
                            print_warning(
                                f"dedupe table capped at {max_keys} "
                                "entries — oldest UIDs are being evicted"
                            )
                            capped = True
                    counts[key] = counts.get(key, 0) + 1
                    counts.move_to_end(key)
                    if dedupe and counts[key] > 1:
                        if not as_json:
                            console.print(
                                f"[dim]  ↳ {spec.seen_again_label(item)} seen again "
                                f"(x{counts[key]})[/dim]"
                            )
                        continue
                    if as_json:
                        print(json.dumps(spec.to_dict(item)))
                    else:
                        console.print(spec.watch_line(item, counts[key]))
            except KeyboardInterrupt:
                pass

        if not as_json:
            duration = time.monotonic() - start
            console.print("")
            print_info(
                f"{detections} detections, {len(counts)} unique {spec.watch_unit}, "
                f"{duration:.0f}s"
            )

    watch_fn = click.pass_context(_watch)
    watch_fn = device_options(watch_fn)
    watch_fn = click.option(
        "--json", "as_json", is_flag=True, help="Emit one JSON object per line."
    )(watch_fn)
    watch_fn = click.option(
        "--quiet-noise/--no-quiet-noise",
        default=True,
        show_default=True,
        help=spec.quiet_noise_help,
    )(watch_fn)
    watch_fn = click.option("--dedupe", is_flag=True, help=spec.dedupe_help)(watch_fn)
    watch_fn.__doc__ = (
        f"Stream {spec.noun} detections continuously. Ctrl-C to stop and "
        "print a summary."
    )
    group.command("watch")(watch_fn)

    # ── scan ─────────────────────────────────────────────────────────────
    def _scan(
        ctx,
        timeout,
        json_file,
        csv_file,
        force,
        verbose,
        port,
        device_id,
        json_file_legacy=None,
        csv_file_legacy=None,
    ):
        if json_file_legacy:
            print_warning("`scan --json FILE` is deprecated — use `--json-out FILE`.")
            json_file = json_file or json_file_legacy
        if csv_file_legacy:
            print_warning("`scan --csv FILE` is deprecated — use `--csv-out FILE`.")
            csv_file = csv_file or csv_file_legacy

        if json_file:
            refuse_overwrite(json_file, force)
        if csv_file:
            refuse_overwrite(csv_file, force)

        level = verbosity(ctx, verbose)
        aggregator = spec.aggregator_cls()
        with _session(port, device_id, trace=make_tracer(level)) as (target, link):
            print_info(f"Scanning {target} for {timeout:g}s — Ctrl-C to stop early")
            parser = spec.parser_cls()
            start = time.monotonic()
            deadline = start + timeout
            progress = Progress(
                SpinnerColumn(style="cyan"),
                TextColumn("[cyan]scanning[/cyan]"),
                BarColumn(bar_width=24, complete_style="cyan", finished_style="cyan"),
                TextColumn("[dim]{task.fields[detections]} detections[/dim]"),
                console=console,
                transient=True,
            )
            try:
                with progress:
                    task = progress.add_task("", total=timeout, detections=0)
                    for line in link.stream(yield_empty=True):
                        if line:
                            item = parser.feed(line)
                            if item is not None:
                                aggregator.add(item)
                        now = time.monotonic()
                        progress.update(
                            task,
                            completed=min(now - start, timeout),
                            detections=aggregator.total_detections,
                        )
                        if now > deadline:
                            break
            except KeyboardInterrupt:
                pass

        console.print("")
        print_info(
            f"Scan @ {target} — {timeout:g}s, {aggregator.total_detections} "
            f"detections, {len(aggregator)} unique {spec.plural_noun}"
        )

        rows = aggregator.to_dict()
        if rows:
            table = Table(header_style="cyan bold")
            for header, _cell, justify in spec.table_columns:
                table.add_column(header, **({"justify": justify} if justify else {}))
            for row in rows:
                table.add_row(*(cell(row) for _h, cell, _j in spec.table_columns))
            console.print(table)
        else:
            print_dim(f"no {spec.plural_noun} detected")

        if json_file:
            write_export(json_file, rows, module._write_json)
        if csv_file:
            write_export(
                csv_file,
                rows,
                lambda path, rows: write_csv(path, rows, spec.csv_fields),
            )

    scan_fn = click.pass_context(_scan)
    scan_fn = device_options(scan_fn)
    scan_fn = click.option(
        "--force",
        is_flag=True,
        help="Overwrite --json-out/--csv-out output files if they already exist.",
    )(scan_fn)
    if spec.legacy_csv_json_aliases:
        scan_fn = click.option(
            "--csv",
            "csv_file_legacy",
            type=click.Path(dir_okay=False, writable=True),
            default=None,
            hidden=True,
        )(scan_fn)
        scan_fn = click.option(
            "--json",
            "json_file_legacy",
            type=click.Path(dir_okay=False, writable=True),
            default=None,
            hidden=True,
        )(scan_fn)
    scan_fn = click.option(
        "--csv-out",
        "csv_file",
        type=click.Path(dir_okay=False, writable=True),
        default=None,
        metavar="FILE",
        help="Write the aggregate as CSV to FILE.",
    )(scan_fn)
    scan_fn = click.option(
        "--json-out",
        "json_file",
        type=click.Path(dir_okay=False, writable=True),
        default=None,
        metavar="FILE",
        help="Write the aggregate as a JSON array to FILE.",
    )(scan_fn)
    scan_fn = click.option(
        "-t",
        "--timeout",
        default=30.0,
        show_default=True,
        help="Seconds to sample for.",
    )(scan_fn)
    scan_fn.__doc__ = (
        f"Sample {spec.noun} detections for a while and print an "
        "aggregated summary.\n\n"
        f"    Repeat detections of the same key collapse into one row with a "
        "count and\n    a first/last time seen. Ctrl-C ends the sample early."
    )
    group.command("scan")(scan_fn)

    # ── info ─────────────────────────────────────────────────────────────
    def _info(ctx, verbose, port, device_id):
        level = verbosity(ctx, verbose)
        with _session(port, device_id, trace=make_tracer(level)) as (target, link):
            r = link.info()
            if not r.ok:
                print_error(f"info failed: {r.message}")
                raise SystemExit(1)

            parser = spec.parser_cls()
            deadline = time.monotonic() + _INFO_PROBE_SECONDS
            for line in link.stream(yield_empty=True):
                if line:
                    parser.feed(line)
                if parser.structured or time.monotonic() > deadline:
                    break

        table = Table(
            title=f"{spec.firmware_name} @ {target}",
            header_style="cyan bold",
            show_header=False,
        )
        table.add_column("field", style="cyan")
        table.add_column("value")
        table.add_row("version", r.data.get("fw") or "[dim]—[/dim]")
        table.add_row("events", spec.info_events(parser))
        table.add_row("state", r.data.get("state") or "[dim]—[/dim]")
        console.print(table)

    info_fn = click.pass_context(_info)
    info_fn = device_options(info_fn)
    info_fn.__doc__ = spec.info_summary
    group.command("info")(info_fn)

    return group
