#!/usr/bin/env python3

# Electronic Cats
# `bombercat emvy …` — EMVy Controller swiss-army commands (EMVyBomberCat
# firmware): EMV read, APDU passthrough, tag/magstripe/NDEF/EMV-card emulation.
# Like magspoof, these commands actively drive the board — but unlike every
# other group, gating here is by *firmware identity*, not by an auto-flashable
# capability: EMVyBomberCat is built from source (arduino-cli), not a prebuilt
# release, so there is no image `bombercat flash` could install. We therefore
# verify `info.fw_name == "emvybombercat"` and, on a mismatch, refute with a
# build-and-flash hint instead of reaching for `ensure_firmware` (D2/§5, P-1:
# gate by id).
#
# Discovery (ping/info/identify) speaks the canonical +OK/-ERR REPL, so `emvy
# info` uses DeviceLink directly. The operative verbs (Fases 3-4: read/apdu/tag/
# mag/emu) speak EMVyBomberCat's historical dialect and will drive an EmvyLink
# (link.py) opened after this same identity gate — see
# docs/IMPLEMENTATION_PLAN_EMVyBomberCat_CLI.md §6 Fase 2.
# Distributed as-is; no warranty is given.

from contextlib import contextmanager
from typing import Iterator, Optional, Tuple

import click
from rich.table import Table

from ..core.bombercat import DeviceLink, Response, resolve_port
from ..utils.cli_options import device_options
from ..utils.detection_cli import device_session, verbosity as _verbosity
from ..utils.output import console, make_tracer, print_error, print_info

# The `fw_name` an EMVyBomberCat board reports over `info` (must match its
# registry `id`, see core/firmwares.py). This is the whole gate: a board that
# answers the handshake but names itself something else is the wrong firmware.
EMVY_FW_NAME = "emvybombercat"

# The operative surface EMVyBomberCat serves, shown by `emvy info`. These are
# firmware capabilities (present whatever the CLI exposes yet), each landing as
# an `emvy` subcommand across Fases 3-4. Kept static rather than probed: the
# firmware offers no capability query, and hard-coding the honest list beats
# pretending to discover it (FW-0a would let `info` advertise `:ops …`, but the
# firmware does not do that today).
_OPERATIONS: Tuple[Tuple[str, str], ...] = (
    ("read", "EMV card read (SCAN → PAN/expiry/track2/AID)"),
    ("apdu", "APDU passthrough to a live card (WAIT/APDU/RELEASE)"),
    ("tag", "tag UID read (TAG)"),
    ("mag", "magstripe swipe emulation (MAG)"),
    ("emu", "NDEF tag / EMV-card emulation (EMU/EMUEMV)"),
)


@click.group("emvy")
def emvy() -> None:
    """EMVy Controller swiss-army commands (requires the EMVyBomberCat firmware)."""


@contextmanager
def _emvy_session(
    port: Optional[str], device_id: Optional[int] = None, trace=None
) -> Iterator[Tuple[str, DeviceLink, Response]]:
    """Open a link verified to be running EMVyBomberCat, yield
    ``(target, link, info)``, and always close it.

    Reuses `device_session` (requires=None: resolve the port, open a DeviceLink,
    and confirm the handshake) and then adds the identity gate this group needs:
    `info` must report `fw_name == emvybombercat`. On a mismatch it refutes and
    exits non-zero, pointing at building/flashing the firmware from source — it
    never auto-flashes, because EMVyBomberCat has no prebuilt image (D2/§5).

    `resolve_port`/`DeviceLink` are referenced as this module's globals so tests
    can monkeypatch them (the `use_link` fixture), mirroring `_magspoof_session`.
    """
    with device_session(
        resolve_port,
        DeviceLink,
        "emvy",
        "EMVyBomberCat",
        port,
        device_id,
        trace,
    ) as (target, link):
        info = link.info()
        name = info.data.get("fw_name", "") if info.ok else ""
        if name != EMVY_FW_NAME:
            print_error(
                f"{target} is not running EMVyBomberCat "
                f"(it reports fw_name={name or '—'})."
            )
            print_info(
                "the `emvy` commands need the EMVyBomberCat firmware — build and "
                "flash it from bombercat-firmware/EMVyBomberCat with arduino-cli. "
                "It is not a prebuilt release, so `bombercat flash` cannot install "
                "it."
            )
            raise SystemExit(1)
        yield target, link, info


@emvy.command("info")
@device_options
@click.pass_context
def info_cmd(ctx, verbose, port, device_id):
    """Report the EMVyBomberCat firmware and the operations it exposes.

    Confirms the board is running EMVyBomberCat (refusing otherwise), then
    prints its version, current state (idle/scanning/emulating/hw-error) and the
    operative surface the `emvy` subcommands drive.

    Exit code: 0 identified, 1 wrong firmware or link error.
    """
    level = _verbosity(ctx, verbose)
    with _emvy_session(port, device_id, trace=make_tracer(level)) as (
        target,
        _link,
        info,
    ):
        fw = info.data.get("fw", "")
        state = info.data.get("state", "")

    table = Table(
        title=f"EMVyBomberCat @ {target}", header_style="cyan bold", show_header=False
    )
    table.add_column("field", style="cyan")
    table.add_column("value")
    table.add_row("version", fw or "[dim]—[/dim]")
    table.add_row("state", state or "[dim]—[/dim]")
    console.print(table)

    console.print("")
    console.print("  [cyan bold]operations[/cyan bold]")
    for name, description in _OPERATIONS:
        console.print(f"  [green]{name:<6}[/green] [dim]{description}[/dim]")
