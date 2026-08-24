#!/usr/bin/env python3

# Electronic Cats
# `bombercat tags read|watch` — NFC tag detection over the DetectTags REPL.
# Turns the device's `:tag` events (or, on older .uf2 images, the legacy
# displayCardInfo() text) into short, scriptable output.
# docs/CLI_IMPROVEMENTS_DetectTags.md §3.1-3.2.
# Distributed as-is; no warranty is given.

import json
import re
import time
from contextlib import contextmanager
from typing import Dict, Iterator, Optional, Tuple

import click
import serial

from ..core.bombercat import DeviceError, DeviceLink, resolve_port
from ..utils.cli_options import device_options
from ..utils.output import console, make_tracer, print_error, print_info, print_warning
from .parser import Tag, TagParser

# Firmware chatter the CLI's own (non -v) output hides by default: boot/idle
# noise printed on every loop iteration, not a detection.
_NOISE_RE = re.compile(
    r"^(Restarting\.\.\.|Waiting for a Card\.\.\.|Card removed!)\s*$"
)


@click.group("tags", context_settings={"help_option_names": ["-h", "--help"]})
def tags():
    """NFC tag detection commands (requires the DetectTags firmware)."""


def _verbosity(ctx: click.Context, local: int) -> int:
    """Combine the root `-v` (before the verb) with a command's own `-v`
    (after it) — either position means the same thing, so the higher count
    wins rather than the two adding up."""
    root = (ctx.obj or {}).get("verbose", 0)
    return max(root, local)


@contextmanager
def _tags_session(
    port: Optional[str],
    device_id: Optional[int] = None,
    trace=None,
) -> Iterator[Tuple[str, DeviceLink]]:
    """Open a verified link for the `tags` commands, yield ``(target, link)``,
    and always close it. Mirrors `nfcgate.cli._device_session`, with a
    DetectTags-flavored hint on a failed handshake."""
    link: Optional[DeviceLink] = None
    try:
        target = resolve_port(port, device_id)
        link = DeviceLink(target, trace=trace).open()
        if not link.ping():
            print_error(
                f"{target} did not answer the handshake. "
                "`tags` needs the DetectTags firmware — check what's flashed "
                "with:  bombercat status"
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


def _tag_to_dict(tag: Tag) -> Dict[str, object]:
    d: Dict[str, object] = {
        "uid": tag.uid,
        "tech": tag.tech,
        "protocol": tag.protocol,
        "ts_ms": tag.ts_ms,
    }
    d.update(tag.extra)
    return d


def _print_field(label: str, value: str) -> None:
    console.print(f"  [cyan]{label:<13}[/cyan] {value}")


def _emit_tag(tag: Tag, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_tag_to_dict(tag)))
        return
    console.print("")
    console.print("  [green bold]Tag detected[/green bold]")
    _print_field("uid", tag.pretty_uid)
    _print_field("technology", tag.tech or "[dim]—[/dim]")
    _print_field("protocol", tag.protocol or "[dim]—[/dim]")
    for key, value in tag.extra.items():
        _print_field(key.replace("_", " "), value)


# ── read ─────────────────────────────────────────────────────────────────────


@tags.command("read", context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "-t",
    "--timeout",
    default=15.0,
    show_default=True,
    help="Seconds to wait for a tag.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit one JSON object on stdout.")
@device_options
@click.pass_context
def read_cmd(ctx, timeout, as_json, verbose, port, device_id):
    """Wait for one tag and print its UID.

    Exit code: 0 tag read, 1 timeout or link error.
    """
    level = _verbosity(ctx, verbose)
    tag: Optional[Tag] = None
    with _tags_session(port, device_id, trace=make_tracer(level)) as (target, link):
        if not as_json:
            print_info(f"Waiting for a tag on {target} — Ctrl-C to abort")
        parser = TagParser()
        deadline = time.monotonic() + timeout
        try:
            for line in link.stream(yield_empty=True):
                if line:
                    tag = parser.feed(line)
                    if tag is not None:
                        break
                if time.monotonic() > deadline:
                    break
        except KeyboardInterrupt:
            print_warning("aborted")
            raise SystemExit(1)

    if tag is None:
        print_error(f"no tag detected in {timeout:g}s")
        raise SystemExit(1)
    _emit_tag(tag, as_json)


# ── watch ────────────────────────────────────────────────────────────────────


def _watch_line(tag: Tag, repeat: int) -> str:
    ts = time.strftime("%H:%M:%S")
    suffix = f" (x{repeat})" if repeat > 1 else ""
    return (
        f"[{ts}] {tag.tech or '?':<8}{tag.protocol or '?':<11}{tag.pretty_uid}{suffix}"
    )


@tags.command("watch", context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--dedupe",
    is_flag=True,
    help="Collapse repeat detections of the same UID into a counter instead "
    "of reprinting them.",
)
@click.option(
    "--quiet-noise/--no-quiet-noise",
    default=True,
    show_default=True,
    help="Hide firmware boot/idle chatter (Restarting…, Waiting for a Card…, "
    "Card removed!).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit one JSON object per line.")
@device_options
@click.pass_context
def watch_cmd(ctx, dedupe, quiet_noise, as_json, verbose, port, device_id):
    """Stream tag detections continuously. Ctrl-C to stop and print a summary."""
    level = _verbosity(ctx, verbose)
    detections = 0
    counts: Dict[str, int] = {}
    start = time.monotonic()
    with _tags_session(port, device_id, trace=make_tracer(level)) as (target, link):
        if not as_json:
            print_info(f"Watching {target} — Ctrl-C to stop")
        parser = TagParser()
        try:
            for line in link.stream():
                if not line:
                    continue
                tag = parser.feed(line)
                if tag is None:
                    if (
                        not quiet_noise
                        and not as_json
                        and _NOISE_RE.match(line.strip())
                    ):
                        console.print(f"[dim]{line}[/dim]")
                    continue

                detections += 1
                key = tag.uid or f"{tag.tech}:{tag.protocol}:{tag.ts_ms}"
                counts[key] = counts.get(key, 0) + 1
                if dedupe and counts[key] > 1:
                    if not as_json:
                        console.print(
                            f"[dim]  ↳ {tag.pretty_uid} seen again "
                            f"(x{counts[key]})[/dim]"
                        )
                    continue
                if as_json:
                    print(json.dumps(_tag_to_dict(tag)))
                else:
                    console.print(_watch_line(tag, counts[key]))
        except KeyboardInterrupt:
            pass

    if not as_json:
        duration = time.monotonic() - start
        console.print("")
        print_info(
            f"{detections} detections, {len(counts)} unique UIDs, {duration:.0f}s"
        )
