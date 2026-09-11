#!/usr/bin/env python3

# Electronic Cats
# `bombercat tags read|watch` — NFC tag detection over the DetectTags REPL.
# Turns the device's `:tag` events (or, on older .uf2 images, the legacy
# displayCardInfo() text) into short, scriptable output. The read/watch/scan/
# info commands themselves are generic — this module only supplies what's
# genuinely tag-shaped (field layout, table columns, dedupe key) via a
# DetectionSpec; modules/utils/detection_cli.py's build_detection_group()
# does the rest. Twin of modules/readers/cli.py.
# docs/CLI_IMPROVEMENTS_DetectTags.md §3.1-3.2.
# Distributed as-is; no warranty is given.

import re
import sys
import time
from typing import Dict

from ..core.bombercat import DeviceLink, resolve_port  # noqa: F401 (resolved by name)
from ..core.firmwares import CAP_TAGS
from ..utils.detection_cli import (
    DetectionSpec,
    build_detection_group,
    print_field as _print_field,
    write_json as _write_json,  # noqa: F401 (resolved by name, for monkeypatching)
)
from .aggregator import _RESERVED_KEYS, TagAggregator
from .mifare.cli import mifare as _mifare
from .parser import Tag, TagParser

# Firmware chatter the CLI's own (non -v) output hides by default: boot/idle
# noise printed on every loop iteration, not a detection.
_NOISE_RE = re.compile(
    r"^(Restarting\.\.\.|Waiting for a Card\.\.\.|Card removed!)\s*$"
)

# Firmware that prints no UID (legacy NFC-B/F) keys `watch`'s dedupe table by
# `tech:protocol:ts_ms`, which never repeats — an unattended `watch` running
# for hours/days would otherwise grow this dict without bound. Cap it as an
# LRU: oldest key evicted once full (M15).
_MAX_DEDUPE_KEYS = 10_000

_CSV_FIELDS = ["uid", "tech", "protocol", "count", "first_s", "last_s"]


def _tag_to_dict(tag: Tag) -> Dict[str, object]:
    d: Dict[str, object] = {
        "uid": tag.uid,
        "tech": tag.tech,
        "protocol": tag.protocol,
        "ts_ms": tag.ts_ms,
    }
    for key, value in tag.extra.items():
        d["x_" + key if key in _RESERVED_KEYS else key] = value
    return d


def _emit_tag_fields(tag: Tag) -> None:
    _print_field("uid", tag.pretty_uid)
    _print_field("technology", tag.tech or "[dim]—[/dim]")
    _print_field("protocol", tag.protocol or "[dim]—[/dim]")
    for key, value in tag.extra.items():
        _print_field(key.replace("_", " "), value)


def _watch_line(tag: Tag, repeat: int) -> str:
    ts = time.strftime("%H:%M:%S")
    suffix = f" (x{repeat})" if repeat > 1 else ""
    return (
        f"[{ts}] {tag.tech or '?':<8}{tag.protocol or '?':<11}{tag.pretty_uid}{suffix}"
    )


def _info_events(parser: TagParser) -> str:
    if parser.structured:
        return "structured (':tag' events)"
    return "legacy text  (no ':tag' events — reflash for exact parsing)"


_TABLE_COLUMNS = [
    ("UID", lambda row: Tag(uid=row["uid"], tech=row["tech"]).pretty_uid, None),
    ("Tech", lambda row: row["tech"] or "[dim]—[/dim]", None),
    ("Protocol", lambda row: row["protocol"] or "[dim]—[/dim]", None),
    ("Count", lambda row: str(row["count"]), "right"),
    ("First", lambda row: f"{row['first_s']:.1f}s", "right"),
    ("Last", lambda row: f"{row['last_s']:.1f}s", "right"),
]

tags = build_detection_group(
    DetectionSpec(
        module=sys.modules[__name__],
        name="tags",
        group_help="NFC tag detection commands (requires the DetectTags firmware).",
        firmware_name="DetectTags",
        noun="tag",
        plural_noun="tags",
        parser_cls=TagParser,
        aggregator_cls=TagAggregator,
        to_dict=_tag_to_dict,
        emit_header="Tag detected",
        emit_fields=_emit_tag_fields,
        watch_line=_watch_line,
        dedupe_key=lambda tag: tag.uid or f"{tag.tech}:{tag.protocol}:{tag.ts_ms}",
        seen_again_label=lambda tag: tag.pretty_uid,
        watch_unit="UIDs",
        dedupe_help=(
            "Collapse repeat detections of the same UID into a counter instead "
            "of reprinting them."
        ),
        quiet_noise_help=(
            "Hide firmware boot/idle chatter (Restarting…, Waiting for a Card…, "
            "Card removed!)."
        ),
        noise_re=_NOISE_RE,
        dedupe_cap_attr="_MAX_DEDUPE_KEYS",
        csv_fields=_CSV_FIELDS,
        table_columns=_TABLE_COLUMNS,
        read_summary="Wait for one tag and print its UID.",
        info_summary=(
            "Report what this DetectTags image can do.\n\n"
            "    Shows the firmware version and state, and — the useful part — "
            "whether it\n    emits structured ':tag' events or the CLI has "
            "fallen back to parsing\n    legacy text "
            "(docs/CLI_IMPROVEMENTS_DetectTags.md §3.4)."
        ),
        info_events=_info_events,
        legacy_csv_json_aliases=True,
        requires=CAP_TAGS,
    )
)

read_cmd = tags.commands["read"]
watch_cmd = tags.commands["watch"]
scan_cmd = tags.commands["scan"]
info_cmd = tags.commands["info"]


# ── mifare ───────────────────────────────────────────────────────────────────
#
# The Mifare Classic commands live in a package of their own (`tags/mifare/`):
# they're the bulk of `tags` by volume and share nothing with the detection
# commands above. The group is assembled there and attached to `tags` here.
tags.add_command(_mifare)
