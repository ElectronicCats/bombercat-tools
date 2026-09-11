#!/usr/bin/env python3

# Electronic Cats
# `bombercat readers read|watch|scan|info` — NFC reader/terminal detection
# over the DetectReaders REPL. Turns the device's `:reader` events into
# short, scriptable output. The read/watch/scan/info commands themselves are
# generic — this module only supplies what's genuinely reader-shaped (field
# layout, table columns, dedupe key) via a DetectionSpec;
# modules/utils/detection_cli.py's build_detection_group() does the rest.
# Twin of modules/tags/cli.py; no legacy text mode (DetectReaders was born
# with structured events).
# docs/IMPLEMENTATION_PLAN_DetectReaders_CLI.md §3.2.
# Distributed as-is; no warranty is given.

import re
import sys
import time
from typing import Dict

from ..core.bombercat import DeviceLink, resolve_port  # noqa: F401 (resolved by name)
from ..core.firmwares import CAP_READERS
from ..utils.detection_cli import (
    DetectionSpec,
    build_detection_group,
    print_field as _print_field,
    write_json as _write_json,  # noqa: F401 (resolved by name, for monkeypatching)
)
from .aggregator import _RESERVED_KEYS, ReaderAggregator
from .parser import Reader, ReaderParser

# Firmware chatter the CLI's own (non -v) output hides by default: boot/idle
# noise printed while armed and re-arming, not a detection.
_NOISE_RE = re.compile(
    r"^(Waiting for a Reader \.\.\.|Re-arm: .*|Re-armed\. .*|"
    r"Re-arm completed with errors .*)\s*$"
)

_CSV_FIELDS = [
    "label",
    "tech",
    "protocol",
    "intf",
    "apdu",
    "aid",
    "count",
    "first_s",
    "last_s",
]


def _reader_to_dict(reader: Reader) -> Dict[str, object]:
    d: Dict[str, object] = {
        "ts_ms": reader.ts_ms,
        "tech": reader.tech,
        "protocol": reader.protocol,
        "intf": reader.intf,
        "apdu": reader.apdu,
        "aid": reader.aid,
        "label": reader.label,
        "n": reader.n,
    }
    for key, value in reader.extra.items():
        d["x_" + key if key in _RESERVED_KEYS else key] = value
    return d


def _emit_reader_fields(reader: Reader) -> None:
    _print_field("label", reader.label or "[dim]—[/dim]")
    _print_field("technology", reader.tech or "[dim]—[/dim]")
    _print_field("protocol", reader.protocol or "[dim]—[/dim]")
    _print_field("interface", reader.intf or "[dim]—[/dim]")
    _print_field("fingerprint", reader.fingerprint)
    _print_field("apdu", reader.pretty_apdu)
    if reader.aid:
        _print_field("aid", reader.aid)
    _print_field("n", str(reader.n) if reader.n is not None else "[dim]—[/dim]")
    for key, value in reader.extra.items():
        _print_field(key.replace("_", " "), value)


def _watch_line(reader: Reader, repeat: int) -> str:
    ts = time.strftime("%H:%M:%S")
    suffix = f" (x{repeat})" if repeat > 1 else ""
    return (
        f"[{ts}] {reader.label or '?':<14}{reader.tech or '?':<8}"
        f"{reader.protocol or '?':<11}{reader.pretty_apdu}{suffix}"
    )


def _info_events(parser: ReaderParser) -> str:
    if parser.structured:
        return "structured (':reader' events)"
    return "no ':reader' events seen yet"


_TABLE_COLUMNS = [
    ("Label", lambda row: row["label"] or "[dim]—[/dim]", None),
    ("Tech", lambda row: row["tech"] or "[dim]—[/dim]", None),
    ("Protocol", lambda row: row["protocol"] or "[dim]—[/dim]", None),
    ("Interface", lambda row: row["intf"] or "[dim]—[/dim]", None),
    ("Count", lambda row: str(row["count"]), "right"),
    ("First", lambda row: f"{row['first_s']:.1f}s", "right"),
    ("Last", lambda row: f"{row['last_s']:.1f}s", "right"),
]
# APDU is left out of the table (it can run to hundreds of hex chars and
# would blow out the column width) — it's still in the JSON/CSV exports.

readers = build_detection_group(
    DetectionSpec(
        module=sys.modules[__name__],
        name="readers",
        group_help=(
            "NFC reader/terminal detection commands (requires the DetectReaders "
            "firmware)."
        ),
        firmware_name="DetectReaders",
        noun="reader",
        plural_noun="readers",
        parser_cls=ReaderParser,
        aggregator_cls=ReaderAggregator,
        to_dict=_reader_to_dict,
        emit_header="Reader detected",
        emit_fields=_emit_reader_fields,
        watch_line=_watch_line,
        dedupe_key=lambda reader: reader.fingerprint,
        seen_again_label=lambda reader: reader.label or "unknown",
        watch_unit="fingerprints",
        dedupe_help=(
            "Collapse repeat detections of the same fingerprint into a counter "
            "instead of reprinting them. Two distinct terminals sharing the same "
            "label (e.g. two EMV readers) share a fingerprint too, so the "
            "counter can mix them — see `readers watch -h` in the docs for "
            "details."
        ),
        quiet_noise_help=(
            "Hide firmware boot/idle chatter (Waiting for a Reader…, Re-arm…)."
        ),
        noise_re=_NOISE_RE,
        csv_fields=_CSV_FIELDS,
        table_columns=_TABLE_COLUMNS,
        read_summary="Wait for one reader/terminal to probe the emulated card.",
        info_summary=(
            "Report what this DetectReaders image can do.\n\n"
            "    Shows the firmware version and state, and whether a ':reader' "
            "event has\n    been seen during a short probe window."
        ),
        info_events=_info_events,
        requires=CAP_READERS,
    )
)

read_cmd = readers.commands["read"]
watch_cmd = readers.commands["watch"]
scan_cmd = readers.commands["scan"]
info_cmd = readers.commands["info"]
