#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

# `mifare check` — dictionary attack: try known keys against every sector
# and report which ones open. docs/CLI_IMPROVEMENTS_MifareCheck.md §6.3.

import json
import time
from collections import OrderedDict
from contextlib import nullcontext
from typing import Dict, List, Optional, Tuple

import click
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from ...utils.cli_options import device_options
from ...utils.detection_cli import (
    print_field as _print_field,
    refuse_overwrite as _refuse_overwrite,
    verbosity as _verbosity,
)
from ...utils.output import (
    console,
    make_tracer,
    print_dim,
    print_error,
    print_info,
    print_subtitle,
    print_success,
    print_warning,
)
from .access_bits import _KEY_A, parse_access_bits
from .block0 import parse_block0
from .dicts import REGISTRY, CandidatePlan, UnknownDictError, select as _select_dicts
from . import uidkeys
from .keyfile import default_keyfile, load_keys
from .common import (
    _MIFARE_BLOCK_HEX_LEN,
    _MIFARE_CHECK_MAX_SECTORS,
    _MIFARE_HEX_RE,
    _MIFARE_KEY_HEX_LEN,
    _MIFARE_TRAILER_AC_LEN,
    _sector_first_block,
)
from .session import (
    _MIFARE_TIMEOUT_OPTION,
    _mifare_session,
    _run_mifare_command,
)


_TIER_LABEL = {0: "default", 1: "app", 2: "vendor"}


def _print_dict_registry() -> None:
    """Render the registered `--dict` families (name, tier, key count, sector
    bias, description). Pure host-side; no device involved."""
    table = Table(
        title="mifare check — named key dictionaries", header_style="cyan bold"
    )
    table.add_column("name", style="cyan")
    table.add_column("tier")
    table.add_column("keys", justify="right")
    table.add_column("sectors")
    table.add_column("description")
    for nd in REGISTRY.values():
        try:
            count = str(len(nd.load()))
        except OSError:
            count = "[red]missing[/red]"
        sectors = ", ".join(str(s) for s in sorted(nd.sectors)) if nd.sectors else "—"
        table.add_row(
            nd.name,
            _TIER_LABEL.get(nd.tier, str(nd.tier)),
            count,
            sectors,
            nd.description,
        )
    console.print(table)
    print_info(
        "add with --dict <names> (comma-separated, or 'all'); "
        "one-off files with --dict-file <path>"
    )


def _print_uid_schemes() -> None:
    """Render the registered `--uid-derived` schemes (name, whether they need a
    secret master key, sectors covered, description). Pure host-side."""
    table = Table(
        title="mifare check — UID-derived key schemes", header_style="cyan bold"
    )
    table.add_column("name", style="cyan")
    table.add_column("generable")
    table.add_column("sectors")
    table.add_column("description")
    for sc in uidkeys.SCHEMES.values():
        generable = (
            "[red]no (secret)[/red]" if sc.requires_secret else "[green]yes[/green]"
        )
        sectors = ", ".join(str(s) for s in sorted(sc.sectors)) if sc.sectors else "—"
        table.add_row(sc.name, generable, sectors, sc.description)
    console.print(table)
    print_info(
        "enable with --uid-derived <names> (comma-separated, or 'all' for every "
        "public scheme); needs the card's UID (read from block 0). Secret-key "
        "schemes are out of scope and refuse."
    )


def _read_uid_via_block0(link, key_type: str, key: str) -> Optional[str]:
    """Read block 0 of sector 0 with a key already known to open it and return
    the 4-byte UID (8 hex chars), or None if it couldn't be read/parsed. Pure
    read — no cryptography; UID-derived schemes need it as their input."""
    r = link.command(f"mifare sector 0 {key_type} {key.upper()}")
    if not r.ok:
        return None
    data_hex = r.data.get("mifare_sector", "")
    if len(data_hex) < _MIFARE_BLOCK_HEX_LEN:
        return None
    b0 = parse_block0(data_hex[:_MIFARE_BLOCK_HEX_LEN])
    return b0.uid if b0 and len(b0.uid) == uidkeys._UID4_HEX_LEN else None


def _write_keyfile(path: str, keys: List[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for key in keys:
            f.write(key + "\n")


def _write_sector_keyfile(
    path: str, sectors: int, found: Dict[Tuple[int, str], Optional[str]]
) -> None:
    """Write the per-sector `sector:keyA:keyB` file consumed by
    `mifare sector --keys-file`. A key type that wasn't recovered is left
    blank (e.g. `3::FFFFFFFFFFFF`), so the sector line is always present even
    when only one of its two keys is known."""
    with open(path, "w", encoding="utf-8") as f:
        for s in range(sectors):
            key_a = found.get((s, "A")) or ""
            key_b = found.get((s, "B")) or ""
            f.write(f"{s}:{key_a}:{key_b}\n")


# Trailer-read key-B recovery. This is NOT the Crypto-1 nested attack (that
# needs raw nonce/parity capture the PN7150 never surfaces — it runs Crypto-1
# in-chip and only reports auth pass/fail). Instead, once the dictionary has
# opened a sector with key A, we read its trailer with that key: on cards whose
# access bits leave key B *readable with key A* (the transport/default configs
# 000 and 001 — NXP MF1S50yyX Table 8), key B comes back in cleartext in the
# trailer's last 6 bytes. Key A itself never reads back under any access
# condition, so only key B is recoverable this way.
#
# Offsets into a `mifare sector` dump (4 blocks x 32 hex chars); the trailer is
# the 4th block, laid out key A (bytes 0-5) | access+GPB (6-9) | key B (10-15).
_TRAILER_HEX_START = 3 * _MIFARE_BLOCK_HEX_LEN
_TRAILER_AC_HEX = slice(
    _TRAILER_HEX_START + _MIFARE_KEY_HEX_LEN,
    _TRAILER_HEX_START + _MIFARE_KEY_HEX_LEN + 6,
)
_TRAILER_KEYB_HEX = slice(
    _TRAILER_HEX_START + _MIFARE_KEY_HEX_LEN + _MIFARE_TRAILER_AC_LEN,
    _TRAILER_HEX_START + _MIFARE_BLOCK_HEX_LEN,
)


def _recover_key_b_via_trailer(
    link, sector: int, key_a: str
) -> Tuple[Optional[str], str]:
    """Read SECTOR's key B off the card using its known key A, no cryptography.

    Returns ``(key_b, reason)``: the 12-hex key B and ``"ok"`` on success, or
    ``(None, reason)`` explaining why not — so the caller can tell "the card
    protects key B" (nothing more we can do without a real nested attack) from
    "the read failed" (worth retrying). Key A itself never reads back under
    any access condition, so only key B is ever recoverable this way."""
    r = link.command(f"mifare sector {sector} A {key_a.upper()}")
    if not r.ok:
        return None, f"trailer read failed ({r.message or 'auth/read error'})"
    data_hex = r.data.get("mifare_sector", "")
    if len(data_hex) < _MIFARE_BLOCK_HEX_LEN * 4:
        return None, "trailer read returned no data"
    access = parse_access_bits(data_hex[_TRAILER_AC_HEX])
    if access is None:
        return None, "trailer access bits unreadable"
    if access.trailer.key_b_read != _KEY_A:
        return None, "key B is protected by the access bits (not readable)"
    key_b = data_hex[_TRAILER_KEYB_HEX].upper()
    if not _MIFARE_HEX_RE.match(key_b) or key_b == "0" * _MIFARE_KEY_HEX_LEN:
        return None, "card returned zeros for key B"
    return key_b, "ok"


@click.command("check")
@click.option(
    "--keys",
    "keyfiles",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False),
    metavar="FILE",
    help="Key dictionary (.keys/.dic/.md), repeatable. Defaults to the "
    "bundled dictionary (2477 known keys); passing --keys replaces it — "
    "include the bundled file yourself alongside others if you want both.",
)
@click.option(
    "--dict",
    "dict_names",
    multiple=True,
    metavar="NAMES",
    help="Named key families to add on top, comma-separated and repeatable "
    f"(e.g. --dict mad,transport). Known: {', '.join(REGISTRY)}, or 'all'. "
    "Their higher-probability keys are tried before the base dictionary; MAD "
    "keys are tried first on the MAD sectors. See --list-dicts.",
)
@click.option(
    "--dict-file",
    "dict_files",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False),
    metavar="FILE",
    help="Extra .dic/.keys file to add as candidates (repeatable), at the "
    "lowest/generic priority. Unlike --keys, this adds to the base dictionary "
    "instead of replacing it.",
)
@click.option(
    "--list-dicts",
    is_flag=True,
    help="List the registered named key families and exit (no device needed).",
)
@click.option(
    "--uid-derived",
    "uid_schemes",
    multiple=True,
    metavar="NAMES",
    help="Also try keys derived from the card UID by a PUBLIC diversification "
    f"scheme, comma-separated and repeatable. Known: {', '.join(uidkeys.SCHEMES)}, "
    "or 'all'. Off by default; needs the UID (read from block 0). Only public "
    "algorithms — secret-key (AES/HMAC) schemes are out of scope. See "
    "--list-uid-schemes.",
)
@click.option(
    "--uid-max",
    type=click.IntRange(0, None),
    default=0,
    show_default=True,
    metavar="N",
    help="Cap UID-derived candidates tried per sector (0 = no cap). Bounds the "
    "extra authentications over the slow I2C link.",
)
@click.option(
    "--list-uid-schemes",
    is_flag=True,
    help="List the registered UID-derived key schemes and exit (no device).",
)
@click.option(
    "--sectors",
    type=click.IntRange(1, _MIFARE_CHECK_MAX_SECTORS),
    default=16,
    show_default=True,
    help=f"Number of sectors to check (16 = 1K). Max {_MIFARE_CHECK_MAX_SECTORS} "
    "— 4K's 16-block sectors (32-39) aren't supported yet.",
)
@click.option(
    "--key-type",
    "key_type",
    type=click.Choice(["A", "B", "both"], case_sensitive=False),
    default="both",
    show_default=True,
    help="Which key slot(s) to try.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit one JSON object on stdout.")
@click.option(
    "--out",
    "out_file",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    metavar="FILE",
    help="Write the recovered keys as a keyfile (one 12-hex key per line, "
    "mfoc/proxmark-compatible).",
)
@click.option(
    "-o",
    "--output-keys",
    "output_keys_file",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    metavar="FILE",
    help="Write recovered keys as one `sector:keyA:keyB` line per sector, "
    "for `mifare sector --keys-file`. A key type not recovered is left blank.",
)
@click.option(
    "--force", is_flag=True, help="Overwrite --out/--output-keys if they exist."
)
@_MIFARE_TIMEOUT_OPTION
@device_options
@click.pass_context
def mifare_check_cmd(
    ctx,
    keyfiles,
    dict_names,
    dict_files,
    list_dicts,
    uid_schemes,
    uid_max,
    list_uid_schemes,
    sectors,
    key_type,
    as_json,
    out_file,
    output_keys_file,
    force,
    timeout,
    verbose,
    port,
    device_id,
):
    """Dictionary attack: try known keys against every sector and report
    which ones open.

    Authorized use only — test only cards you own or with the owner's
    explicit permission. A sector that opens with a dictionary key (e.g. the
    default `FFFFFFFFFFFF`) is evidence that card is exposed.

    Exit code: 0 if every requested (sector, key type) was recovered, 1
    otherwise (some unknown, or the sweep was interrupted early).
    """
    if list_dicts:
        _print_dict_registry()
        raise SystemExit(0)

    if list_uid_schemes:
        _print_uid_schemes()
        raise SystemExit(0)

    try:
        schemes = uidkeys.select(uid_schemes)
    except (uidkeys.UnknownUidSchemeError, uidkeys.SecretKeyRequiredError) as e:
        print_error(str(e))
        raise SystemExit(1)

    if out_file:
        _refuse_overwrite(out_file, force)
    if output_keys_file:
        _refuse_overwrite(output_keys_file, force)

    base = load_keys(keyfiles or [str(default_keyfile())])
    try:
        named = _select_dicts(dict_names)
    except UnknownDictError as e:
        print_error(str(e))
        raise SystemExit(1)
    extra = load_keys(dict_files) if dict_files else []
    plan = CandidatePlan(base, named, extra)
    if not plan:
        print_error("no keys loaded — check --keys/--dict/--dict-file")
        raise SystemExit(1)

    key_types = ["A", "B"] if key_type.lower() == "both" else [key_type.upper()]
    total = sectors * len(key_types)

    level = _verbosity(ctx, verbose)
    found: Dict[Tuple[int, str], Optional[str]] = {}
    known: "OrderedDict[str, None]" = OrderedDict()
    interrupted = False

    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        if not as_json:
            if named or extra:
                sources = [nd.name for nd in named]
                sources += [f"file:{f}" for f in dict_files]
                print_info(f"dictionaries: base + {', '.join(sources)}")
            print_info(
                f"Checking {target} — {sectors} sector(s) x {len(key_types)} key "
                f"type(s), {plan.size} keys — Ctrl-C for partial results"
            )
        total_attempts = total * plan.size
        progress = (
            None
            if as_json
            else Progress(
                SpinnerColumn(style="cyan"),
                TextColumn(
                    "[cyan]sector {task.fields[sector]:>2} key {task.fields[key_type]}"
                    "[/cyan]"
                ),
                BarColumn(bar_width=24, complete_style="cyan", finished_style="cyan"),
                TextColumn(
                    "[dim]{task.fields[tried]}/{task.fields[key_total]} keys[/dim]"
                ),
                TextColumn("[dim]{task.fields[rate]:.0f} keys/s[/dim]"),
                TimeRemainingColumn(),
                TimeElapsedColumn(),
                TextColumn(
                    "[dim]{task.fields[recovered]}/{task.fields[pairs]} "
                    "recovered[/dim]"
                ),
                console=console,
                transient=True,
            )
        )
        first = True
        attempts = 0
        # Honest baseline for the "auths saved" stat: how many authentications a
        # naive sweep of the SAME candidate universe would cost — every (sector,
        # key type) slot trying its full pool to the end, with no reuse shortcut
        # and no early cut. Accumulated per slot below; attempts is what we
        # actually issued. See the Timing summary / JSON `stats`.
        naive_attempts = 0
        uid: Optional[str] = None  # filled once sector 0 opens (UID-derived schemes)
        start_time = time.monotonic()
        try:
            with progress or nullcontext():
                task = (
                    progress.add_task(
                        "",
                        total=total_attempts,
                        recovered=0,
                        pairs=total,
                        sector=0,
                        key_type="",
                        tried=0,
                        key_total=plan.size,
                        rate=0.0,
                    )
                    if progress
                    else None
                )
                for s in range(sectors):
                    block = _sector_first_block(s)
                    uid_cands = (
                        uidkeys.candidates_for(uid, schemes, s, uid_max)
                        if schemes and uid
                        else []
                    )
                    for kt in key_types:
                        # The universe a naive sweep would try for this slot:
                        # UID-derived candidates + the full dictionary for this
                        # sector, deduped — WITHOUT the reuse shortcut and run to
                        # completion. This is the baseline "auths saved" is
                        # measured against (equals sectors x universe when no
                        # --dict/--uid-derived, matching the Fase 0 pins).
                        naive_pool = list(
                            dict.fromkeys(uid_cands + list(plan.for_sector(s)))
                        )
                        naive_attempts += len(naive_pool)
                        # What we actually try: confirmed keys (reuse) first, then
                        # that pool; deduped, first occurrence wins, with an early
                        # cut on the first hit.
                        candidates = list(dict.fromkeys(list(known) + naive_pool))
                        key = None
                        tried = 0
                        for candidate in candidates:
                            line = f"mifare auth {block} {kt} {candidate}"
                            if first:
                                r = _run_mifare_command(link, line, timeout)
                                first = False
                            else:
                                r = link.command(line)
                            tried += 1
                            attempts += 1
                            if progress:
                                elapsed = time.monotonic() - start_time
                                progress.update(
                                    task,
                                    advance=1,
                                    sector=s,
                                    key_type=kt,
                                    tried=tried,
                                    rate=attempts / elapsed if elapsed > 0 else 0.0,
                                )
                            if r.ok:
                                key = candidate
                                break
                        found[(s, kt)] = key
                        if key:
                            known.setdefault(key, None)
                        if progress:
                            recovered = sum(1 for v in found.values() if v)
                            skipped = len(candidates) - tried
                            if skipped:
                                progress.update(task, advance=skipped)
                            progress.update(task, recovered=recovered)

                    # Once sector 0 is open, read block 0 for the UID so the
                    # UID-derived schemes can generate candidates for the rest.
                    # (Feedback is printed after the progress bar closes, below.)
                    if schemes and uid is None and s == 0:
                        kt0, key0 = next(
                            (
                                (kt, found[(0, kt)])
                                for kt in ("A", "B")
                                if found.get((0, kt))
                            ),
                            (None, None),
                        )
                        if key0:
                            uid = _read_uid_via_block0(link, kt0, key0)
        except KeyboardInterrupt:
            interrupted = True

        if schemes and not as_json:
            if uid:
                print_info(
                    f"UID {uid} — UID-derived candidates via "
                    f"{', '.join(sc.name for sc in schemes)}"
                    + (f" (max {uid_max}/sector)" if uid_max else "")
                )
            else:
                print_dim(
                    "UID unavailable (sector 0 not opened / block 0 unreadable) "
                    "— UID-derived schemes skipped"
                )

        # Trailer-read fallback (still inside the open session): for sectors
        # the dictionary opened with key A but not key B, try to read key B
        # straight off the card. Not the Crypto-1 nested attack — that needs
        # nonce capture this reader can't do — but it recovers key B for free
        # on cards that leave it readable. See _recover_key_b_via_trailer.
        if not interrupted and "B" in key_types:
            targets = [
                (s, key_a)
                for s in range(sectors)
                if found.get((s, "B")) is None
                and (key_a := found.get((s, "A"))) is not None
            ]
            if targets and not as_json:
                print_info(
                    f"trailer read: {len(targets)} sector(s) opened key A but "
                    "not key B — reading key B off the card (no Crypto-1 attack)"
                )
            for s, key_a in targets:
                key_b, reason = _recover_key_b_via_trailer(link, s, key_a)
                if key_b:
                    found[(s, "B")] = key_b
                    known.setdefault(key_b, None)
                    if not as_json:
                        print_success(
                            f"sector {s} key B recovered via trailer read: {key_b}"
                        )
                elif not as_json:
                    print_dim(f"  sector {s} key B not recovered — {reason}")

    if interrupted and not as_json:
        print_warning("interrupted — showing partial results")

    recovered = sum(1 for v in found.values() if v)
    exposed_sectors = len({s for (s, _kt), v in found.items() if v})

    # Auths saved by known-key reuse + per-sector early cut vs. a naive sweep of
    # the same candidate universe (Fase 1). Honest even on interrupt: both
    # counters stop together, so the ratio reflects the work actually done.
    auths_saved = naive_attempts - attempts
    saved_pct = (auths_saved / naive_attempts * 100.0) if naive_attempts else 0.0

    if out_file:
        try:
            _write_keyfile(out_file, list(known))
        except OSError as e:
            print_error(f"could not write {out_file}: {e}")
            raise SystemExit(1)
        if not as_json:
            print_info(f"wrote {out_file}")

    if output_keys_file:
        try:
            _write_sector_keyfile(output_keys_file, sectors, found)
        except OSError as e:
            print_error(f"could not write {output_keys_file}: {e}")
            raise SystemExit(1)
        if not as_json:
            print_info(f"wrote {output_keys_file}")

    if as_json:
        rows = []
        for s in range(sectors):
            key_a = found.get((s, "A")) if "A" in key_types else None
            key_b = found.get((s, "B")) if "B" in key_types else None
            rows.append({"sector": s, "key_a": key_a, "key_b": key_b})
        print(
            json.dumps(
                {
                    "sectors": rows,
                    "recovered": recovered,
                    "total": total,
                    "stats": {
                        "attempts": attempts,
                        "naive_attempts": naive_attempts,
                        "auths_saved": auths_saved,
                        "saved_pct": round(saved_pct, 1),
                        "interrupted": interrupted,
                    },
                }
            )
        )
        raise SystemExit(0 if recovered == total else 1)

    console.print("")
    table = Table(title=f"MifareClassic check @ {target}", header_style="cyan bold")
    table.add_column("Sector", justify="right")
    table.add_column("Key A")
    table.add_column("Key B")
    for s in range(sectors):
        row = []
        for kt in ("A", "B"):
            if kt not in key_types:
                row.append("[dim]—[/dim]")
                continue
            key = found.get((s, kt))
            row.append(f"[green]{key}[/green]" if key else r"[dim]\[unknown][/dim]")
        table.add_row(str(s), *row)
    console.print(table)

    console.print("")
    print_info(
        f"{recovered}/{total} keys recovered — card exposes {exposed_sectors}/"
        f"{sectors} sectors with known keys"
    )

    duration = time.monotonic() - start_time
    console.print("")
    print_subtitle("Timing summary")
    _print_field("total time", f"{duration:.1f}s")
    _print_field("attempts", str(attempts))
    _print_field(
        "avg rate", f"{attempts / duration:.1f} keys/s" if duration > 0 else "—"
    )
    _print_field("keys identified", f"{recovered}/{total}")
    _print_field("failures", str(total - recovered))
    _print_field("sectors exposed", f"{exposed_sectors}/{sectors}")
    _print_field("naive sweep", f"{naive_attempts} auths")
    _print_field("auths saved", f"{auths_saved} ({saved_pct:.1f}% vs. naive sweep)")
    if interrupted:
        _print_field("status", "[yellow]interrupted — partial results[/yellow]")

    raise SystemExit(0 if recovered == total else 1)
