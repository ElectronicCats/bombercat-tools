#!/usr/bin/env python3

# Electronic Cats
# analyze.py - `mifare analyze`: turn a canonical `mifare dump --out` JSON into
# an interpretable security report, 100% offline (no PN7150, no card). Sibling
# of `dump`/`restore`. It never breaks Crypto-1 and never guesses a key — it
# only reads what a dump already holds and explains it:
#   - MAD (sector 0, plus MAD2 in sector 16 on 4K) -> which app owns each sector;
#   - value blocks (the ±value/complement/address layout) told apart from data;
#   - weak keys: which sectors opened with a default/known key vs. a custom one;
#   - access bits: insecure trailer/data-block configurations, via access_bits;
#   - gaps: sectors that were never opened are listed as NOT evaluable — the
#     report never calls an unread sector "secure".
# Fase 4 of PLAN_Mejoras_MIFARE.md (Mejora B). Distributed as-is; no warranty.

from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

import click
from rich.table import Table

from ...utils.output import (
    console,
    print_error,
    print_info,
    print_subtitle,
    print_warning,
)
from ...utils.detection_cli import print_field as _print_field
from .access_bits import parse_access_bits
from .common import _MIFARE_BLOCK_HEX_LEN, _MIFARE_KEY_HEX_LEN, _MIFARE_TRAILER_AC_LEN
from .dicts import REGISTRY, UnknownDictError, select as _select_dicts
from .keyfile import (
    SectorKeyfileError,
    default_keyfile,
    load_keys,
    load_sector_keys,
)
from .mad import parse_mad

_ZERO_KEY = "0" * _MIFARE_KEY_HEX_LEN  # the read-back a card gives for a hidden key


def _first_block(sector: int) -> int:
    """Absolute block index of SECTOR's first block. 4-block sectors 0-31, then
    16-block sectors 32-39 (4K). Mirrors `tests/mifare_card.first_block` so a
    reported block number matches what a Proxmark/`dump` user sees."""
    if sector < 32:
        return sector * 4
    return 128 + (sector - 32) * 16


def detect_value_block(block_hex: str) -> Optional[Tuple[int, int]]:
    """A MIFARE value block is ``value | ~value | value | addr ~addr addr ~addr``.
    Return ``(signed_value, address)`` if `block_hex` fits that layout exactly,
    else None — the strictness is what tells a real value block apart from
    ordinary data that happens to share a byte or two."""
    try:
        b = bytes.fromhex(block_hex)
    except ValueError:
        return None
    if len(b) != 16:
        return None
    v, inv, v2, tail = b[0:4], b[4:8], b[8:12], b[12:16]
    if v != v2:
        return None
    if any(x ^ 0xFF != y for x, y in zip(v, inv)):
        return None
    a = tail[0]
    if tail != bytes([a, a ^ 0xFF, a, a ^ 0xFF]):
        return None
    return int.from_bytes(v, "little", signed=True), a


def _classify_key(key: Optional[str], default_keys: set) -> str:
    """'not_recovered' (no key), 'default' (in the known/default dictionary) or
    'custom' (recovered but not a known key). A custom key is the *good* sign;
    a default key is the weakness this report exists to surface."""
    if not key:
        return "not_recovered"
    return "default" if key.upper() in default_keys else "custom"


def _sector_keys(
    trailer: str,
    sector: int,
    keyfile_keys: Optional[Dict[int, Tuple[Optional[str], Optional[str]]]],
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve ``(keyA, keyB)`` for a sector. A `--keys-file` (from
    `check --output-keys`) is authoritative when given: blank there means "not
    recovered", so a genuine all-zero key is disambiguated from a hidden one.
    Without it, keys come from the dump trailer, where the all-zero read-back a
    card gives for a key it won't reveal is treated as not recovered."""
    if keyfile_keys is not None:
        return keyfile_keys.get(sector, (None, None))
    key_a = trailer[:_MIFARE_KEY_HEX_LEN]
    key_b = trailer[_MIFARE_KEY_HEX_LEN + _MIFARE_TRAILER_AC_LEN :]
    return (
        key_a.upper() if key_a and key_a != _ZERO_KEY else None,
        key_b.upper() if key_b and key_b != _ZERO_KEY else None,
    )


def _access_report(trailer: str) -> Optional[Dict[str, object]]:
    """Decode a trailer's access bits into ``{raw, valid, gpb, issues}``, where
    `issues` is the list of insecure conditions found. None if the trailer isn't
    a full block (a gap) — no dissection over garbage."""
    if len(trailer) != _MIFARE_BLOCK_HEX_LEN:
        return None
    ac_field = trailer[
        _MIFARE_KEY_HEX_LEN : _MIFARE_KEY_HEX_LEN + _MIFARE_TRAILER_AC_LEN
    ]
    gpb = ac_field[6:8]
    access = parse_access_bits(ac_field[:6])
    if access is None:
        return None

    issues: List[str] = []
    if not access.valid:
        issues.append("access bits fail their own inverse check (corrupt or not read)")
    if access.trailer.key_b_read != "never":
        issues.append(
            f"key B is readable ({access.trailer.key_b_read}) — it protects nothing, "
            "treat it as public data"
        )
    for i, blk in enumerate(access.blocks):
        if blk.write == "key A or B":
            issues.append(
                f"data block {i} is writable with key A or key B (unprotected)"
            )

    return {
        "raw": access.raw,
        "valid": access.valid,
        "gpb": gpb.upper(),
        "issues": issues,
    }


def analyze_dump(
    dump: Dict[str, object],
    default_keys: set,
    keyfile_keys: Optional[Dict[int, Tuple[Optional[str], Optional[str]]]] = None,
    source: Optional[str] = None,
) -> Dict[str, object]:
    """Pure engine behind `mifare analyze`: a canonical dump dict in, a
    JSON-serializable report out. No I/O, no device — unit-testable on its own."""
    sectors_in = list(dump.get("sectors", []))
    by_sector = {int(s["sector"]): s for s in sectors_in}
    total = int(dump.get("sectors_total", len(sectors_in)))

    # ── MAD (sector 0) and MAD2 (sector 16, 4K) ────────────────────────────────
    mad = mad2 = None
    s0 = by_sector.get(0)
    if s0 is not None and len(s0.get("blocks", [])) >= 3:
        mad_obj = parse_mad(s0["blocks"][1], s0["blocks"][2], version=1, first_sector=1)
        mad = mad_obj.to_dict() if mad_obj is not None else None
    s16 = by_sector.get(16)
    if s16 is not None and len(s16.get("blocks", [])) >= 2:
        mad2_obj = parse_mad(
            s16["blocks"][0], s16["blocks"][1], version=2, first_sector=17
        )
        mad2 = mad2_obj.to_dict() if mad2_obj is not None else None

    # ── per-sector analysis ────────────────────────────────────────────────────
    sector_reports: List[Dict[str, object]] = []
    weak, custom, insecure = [], [], []
    value_block_total = 0
    for s in sorted(by_sector):
        entry = by_sector[s]
        blocks = list(entry.get("blocks", []))
        trailer = blocks[-1] if blocks else ""
        data_blocks = blocks[:-1] if blocks else []

        key_a, key_b = _sector_keys(trailer, s, keyfile_keys)
        class_a = _classify_key(key_a, default_keys)
        class_b = _classify_key(key_b, default_keys)
        weak_key = "default" in (class_a, class_b)
        if weak_key:
            weak.append(s)
        if "custom" in (class_a, class_b):
            custom.append(s)

        access = _access_report(trailer)
        if access is not None and access["issues"]:
            insecure.append(s)

        vblocks = []
        base = _first_block(s)
        for i, blk in enumerate(data_blocks):
            if s == 0 and i == 0:
                continue  # sector 0 block 0 is manufacturer data, never a value block
            vb = detect_value_block(blk)
            if vb is not None:
                value_block_total += 1
                vblocks.append(
                    {"block": base + i, "index": i, "value": vb[0], "address": vb[1]}
                )

        mad_role = None
        if s == 0 and mad is not None:
            mad_role = "MAD1 directory"
        elif s == 16 and mad2 is not None:
            mad_role = "MAD2 directory"

        sector_reports.append(
            {
                "sector": s,
                "opened_with": entry.get("opened_with"),
                "key_a": key_a,
                "key_a_class": class_a,
                "key_b": key_b,
                "key_b_class": class_b,
                "weak_key": weak_key,
                "access": access,
                "value_blocks": vblocks,
                "mad_role": mad_role,
            }
        )

    # ── gaps: never-opened sectors are declared, never called "secure" ─────────
    gaps: List[Dict[str, object]] = []
    failed = {
        int(f["sector"]): f.get("reason", "read failed")
        for f in dump.get("failed_sectors", [])
    }
    for s in sorted(failed):
        gaps.append({"sector": s, "reason": failed[s]})
    for s in range(total):
        if s not in by_sector and s not in failed:
            gaps.append({"sector": s, "reason": "not present in dump (never read)"})
    gaps.sort(key=lambda g: g["sector"])

    return {
        "source": source,
        "uid": dump.get("uid"),
        "size": dump.get("size"),
        "sectors_total": total,
        "sectors_analyzed": len(sector_reports),
        "mad": mad,
        "mad2": mad2,
        "sectors": sector_reports,
        "gaps": gaps,
        "summary": {
            "weak_key_sectors": weak,
            "custom_key_sectors": custom,
            "insecure_access_sectors": insecure,
            "value_block_count": value_block_total,
            "gap_sectors": [g["sector"] for g in gaps],
        },
    }


# ── rendering ──────────────────────────────────────────────────────────────────

_CLASS_STYLE = {
    "default": "[red]default[/red]",
    "custom": "[green]custom[/green]",
    "not_recovered": "[dim]not recovered[/dim]",
}


def _fmt_key(key: Optional[str], klass: str) -> str:
    return f"{key or '—':<12} {_CLASS_STYLE[klass]}"


def _print_mad(mad: Dict[str, object]) -> None:
    version = mad["version"]
    crc_note = (
        "[green]CRC ok[/green]" if mad["crc_valid"] else "[red]CRC MISMATCH ⚠[/red]"
    )
    print_subtitle(f"MAD{version if version > 1 else ''} (application directory)")
    _print_field("crc", f"{mad['crc']}  ({crc_note})")
    _print_field("info byte", mad["info"])
    allocated = [e for e in mad["entries"] if e["allocated"]]
    if not allocated:
        _print_field("apps", "[dim]no sectors allocated (all AIDs free)[/dim]")
        return
    for e in allocated:
        _print_field(f"sector {e['sector']}", f"AID {e['aid']}")


def _print_report(report: Dict[str, object]) -> None:
    console.print("")
    print_info(
        "MIFARE Classic dump analysis"
        + (f" — uid {report['uid']}" if report.get("uid") else "")
        + (f" ({report['size']})" if report.get("size") else "")
    )
    _print_field(
        "sectors",
        f"{report['sectors_analyzed']} analyzed / {report['sectors_total']} total",
    )

    if report.get("mad"):
        _print_mad(report["mad"])
    if report.get("mad2"):
        _print_mad(report["mad2"])

    print_subtitle("Sectors")
    table = Table(header_style="cyan bold", show_lines=False)
    table.add_column("S", justify="right")
    table.add_column("Opened")
    table.add_column("Key A")
    table.add_column("Key B")
    table.add_column("Notes")
    for s in report["sectors"]:
        notes = []
        if s["mad_role"]:
            notes.append(s["mad_role"])
        for vb in s["value_blocks"]:
            notes.append(f"value block @{vb['block']} = {vb['value']}")
        if s["access"] and s["access"]["issues"]:
            notes.append(
                f"[yellow]{len(s['access']['issues'])} access issue(s)[/yellow]"
            )
        weak_tag = " [red]weak[/red]" if s["weak_key"] else ""
        table.add_row(
            str(s["sector"]),
            (s["opened_with"] or "—") + weak_tag,
            _fmt_key(s["key_a"], s["key_a_class"]),
            _fmt_key(s["key_b"], s["key_b_class"]),
            "\n".join(notes) if notes else "",
        )
    console.print(table)

    # Access-bit issues spelled out under the table (the table only counts them).
    issue_sectors = [
        s for s in report["sectors"] if s["access"] and s["access"]["issues"]
    ]
    if issue_sectors:
        print_subtitle("Access-bit findings")
        for s in issue_sectors:
            for issue in s["access"]["issues"]:
                _print_field(f"sector {s['sector']}", issue)

    # Gaps: always listed, never concluded "secure".
    print_subtitle("Gaps (not evaluable)")
    if report["gaps"]:
        for g in report["gaps"]:
            _print_field(f"sector {g['sector']}", g["reason"])
        print_warning("gaps were never opened — no security claim is made about them")
    else:
        _print_field("none", "every sector was read")

    # Summary.
    summ = report["summary"]
    print_subtitle("Summary")
    _print_field("weak-key sectors", _fmt_list(summ["weak_key_sectors"]))
    _print_field("custom-key sectors", _fmt_list(summ["custom_key_sectors"]))
    _print_field("insecure access", _fmt_list(summ["insecure_access_sectors"]))
    _print_field("value blocks", str(summ["value_block_count"]))
    _print_field("gap sectors", _fmt_list(summ["gap_sectors"]))


def _fmt_list(items: List[int]) -> str:
    return ", ".join(str(i) for i in items) if items else "[dim]none[/dim]"


# ── command ────────────────────────────────────────────────────────────────────


@click.command("analyze")
@click.argument("dump_file", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "-k",
    "--keys-file",
    "keys_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    metavar="FILE",
    help="A `sector:keyA:keyB` file (from `mifare check --output-keys`). When "
    "given it is authoritative for which keys were recovered — it tells a "
    "genuine all-zero key apart from a sector whose key was never found.",
)
@click.option(
    "--dict",
    "dict_names",
    multiple=True,
    metavar="NAMES",
    help="Named key families counted as 'known/weak' when classifying keys, "
    f"comma-separated and repeatable. Known: {', '.join(REGISTRY)}, or 'all'. "
    "The same families `mifare check --dict` uses — see its `--list-dicts`.",
)
@click.option(
    "--dict-file",
    "dict_files",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False),
    metavar="FILE",
    help="Extra key dictionary file counted as 'known/weak' when classifying "
    "keys (repeatable). The bundled default dictionary is always included.",
)
@click.option(
    "--json", "as_json", is_flag=True, help="Emit the full report as JSON on stdout."
)
def mifare_analyze_cmd(dump_file, keys_file, dict_names, dict_files, as_json):
    """Analyze a canonical `mifare dump --out` JSON — 100% offline, no card.

    Reports the MAD (which application owns each sector), value blocks, which
    sectors opened with a weak/default key vs. a custom one, insecure access-bit
    configurations, and — always — the sectors that were never opened, declared
    as gaps rather than assumed secure. Use it to justify migrating off MIFARE
    Classic. Pass --keys-file to classify recovered keys precisely; --dict/
    --dict-file to count extra known-weak families/files; --json for a
    machine-readable report.
    """
    try:
        named = _select_dicts(dict_names)
    except UnknownDictError as e:
        print_error(str(e))
        raise SystemExit(1)

    try:
        with open(dump_file, encoding="utf-8") as f:
            dump = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print_error(f"could not read dump {dump_file}: {e}")
        raise SystemExit(1)
    if not isinstance(dump, dict) or "sectors" not in dump:
        print_error(
            f"{dump_file} is not a canonical `mifare dump --out` JSON "
            "(no 'sectors' key)"
        )
        raise SystemExit(1)

    default_keys = set(load_keys([str(default_keyfile()), *dict_files]))
    for nd in named:
        default_keys.update(nd.load())

    keyfile_keys = None
    if keys_file:
        try:
            keyfile_keys = load_sector_keys(keys_file)
        except OSError as e:
            print_error(f"could not read {keys_file}: {e}")
            raise SystemExit(1)
        except SectorKeyfileError as e:
            print_error(str(e))
            raise SystemExit(1)

    report = analyze_dump(dump, default_keys, keyfile_keys, source=dump_file)

    if as_json:
        print(json.dumps(report))
        return
    _print_report(report)
