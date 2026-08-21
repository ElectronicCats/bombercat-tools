#!/usr/bin/env python3

# Electronic Cats
# `bombercat flash` — download and write the prebuilt .uf2 firmware images
# published by ElectronicCats/bombercat-firmware, so putting a firmware on a
# board no longer needs the ~1 GB arduino-cli toolchain.
# Design and phases: docs/FLASH_PLAN.md.
#
# The network is only touched from here, on demand: the release cache is filled
# lazily by the first command that needs it, never in a constructor
# (FLASH_PLAN §2.3.1). A local .uf2 path never consults it at all.
# Distributed as-is; no warranty is given.

from pathlib import Path

import click
from rich.table import Table
from rich.text import Text

from ..core.usb_connection import describe_devices, find_device, find_devices
from ..utils.cli_options import target_options
from ..utils.output import (
    console,
    fmt_command,
    print_dim,
    print_error,
    print_error_panel,
    print_info,
    print_success,
    print_warning,
)
from .flasher import flash as write_image
from .releases import FirmwareError, FirmwareImage, ReleaseCache
from .uf2 import BootloaderTimeout, DRIVE_LABEL, find_uf2_drive, unmounted_rp2_device


def human_size(size: int) -> str:
    """`412 KB` — the .uf2 images are all in the hundreds of kilobytes."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _ensure_cache(cache: ReleaseCache, refresh: bool) -> None:
    """Populate/revalidate the cache, tolerating GitHub being unreachable.

    An empty cache with no network is fatal — there is nothing to show. A
    *populated* cache with no network is not: the images on disk are still
    perfectly flashable, so we say why the check failed and carry on.
    """
    if not (refresh or cache.tag is None or cache.is_stale()):
        return
    try:
        cache.refresh(force=refresh)
    except FirmwareError as e:
        if cache.tag is None:
            raise
        print_warning(f"could not check GitHub ({e}) — showing the cached release.")


def _show_list(cache: ReleaseCache, full: bool) -> None:
    images = cache.images()
    if not images:
        print_error("no firmware images in the cache.")
        print_info("Try `bombercat flash --refresh --list`.")
        raise SystemExit(1)

    table = Table(title=f"Firmware images — {cache.tag}", header_style="cyan bold")
    table.add_column("Firmware")
    table.add_column("Size", justify="right")
    # The column itself stays wrappable — that is what lets rich shrink *it*
    # to fit the terminal instead of squeezing Firmware and Size to nothing —
    # while each cell clips itself to a single line. `--full` drops the clip
    # and lets the paragraph wrap.
    table.add_column("Description")
    for image in images:
        description = image.description.strip()
        if not description:
            cell = Text("—", style="dim")
        elif full:
            cell = Text(description)
        else:
            cell = Text(description, no_wrap=True, overflow="ellipsis")
        table.add_row(image.stem, human_size(image.size), cell)
    console.print(table)
    print_info(f"Flash one with:  bombercat flash {images[0].stem}")


def _resolve_image(cache: ReleaseCache, firmware: str, refresh: bool) -> Path:
    """What the user typed -> a .uf2 on disk (FLASH_PLAN §3.5).

    A local path wins and short-circuits: `bombercat flash ./build/NFCGate.uf2`
    must work with no network and no cache at all.
    """
    local = Path(firmware).expanduser()
    if local.exists():
        if not local.is_file():
            raise FirmwareError(f"{local} is not a file.")
        return local

    _ensure_cache(cache, refresh)
    image: FirmwareImage = cache.find(firmware)
    if image is None:
        names = ", ".join(i.stem for i in cache.images())
        raise FirmwareError(
            f"no firmware named '{firmware}' in release {cache.tag}. "
            f"Available: {names or '(none)'}"
        )
    return image.path


def _resolve_target(port, device_id, in_bootloader: bool):
    """Which board to reboot (FLASH_PLAN §3.6).

    Deliberately *not* `core.bombercat.resolve_port`: its auto-detect branch
    handshakes the REPL, which only NFCGate answers, so it would refuse to
    flash a board running any of the other eight firmwares.
    """
    if port and device_id is not None:
        raise FirmwareError("--port and --device are mutually exclusive; pass one")
    if port:
        return port

    if device_id is not None:
        device = find_device(device_id)
        if device is None:
            known = describe_devices()
            raise FirmwareError(
                f"no BomberCat with ID {device_id}"
                + (f"; attached: {known}" if known else ": none is attached")
                + " (see `bombercat device list`)"
            )
        return device.port

    devices = find_devices()
    if len(devices) == 1:
        return devices[0].port
    if len(devices) > 1:
        raise FirmwareError(
            f"multiple BomberCats found ({describe_devices(devices)}); "
            "pick one with -d/--device or -p/--port"
        )
    if in_bootloader:
        # The board is already in BOOTSEL, so it has no serial port to find.
        return None
    raise FirmwareError(
        "no BomberCat found. Connect one, or put it in bootloader mode "
        "(double-tap RESET) and run this again."
    )


def _bootloader_help(image_name: str) -> None:
    """The panel for "the 1200-bps touch did not get us into the bootloader"."""
    device = unmounted_rp2_device()
    if device:
        print_error_panel(
            title="Bootloader drive not mounted",
            problem=f"The board is in bootloader mode, but {DRIVE_LABEL} is not mounted.",
            why=(
                f"The kernel sees the bootloader ({device}) but nothing mounted "
                "it — usually a headless box with no udisks. Mounting it needs "
                "privileges this command will not take on its own."
            ),
            fix=[
                f"Mount it:  {fmt_command(f'udisksctl mount -b {device}')}",
                f"or:  {fmt_command(f'sudo mkdir -p /mnt/{DRIVE_LABEL} && sudo mount {device} /mnt/{DRIVE_LABEL}')}",
                f"Run {fmt_command(f'bombercat flash {image_name}')} again.",
            ],
        )
        return

    print_error_panel(
        title="Board did not enter bootloader mode",
        problem=f"No {DRIVE_LABEL} drive appeared after the 1200-bps reset.",
        why=(
            "The firmware currently on the board may not implement the "
            "1200-bps reboot (a sketch built against a different core does "
            "not). You can always get there by hand — the bootloader is in ROM."
        ),
        fix=[
            "Double-tap the RESET button on the board.",
            f"Check that a drive named {DRIVE_LABEL} appears.",
            f"Run {fmt_command(f'bombercat flash {image_name}')} again — it "
            "will find the drive and copy straight to it.",
        ],
    )


@click.command("flash", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("firmware", required=False)
@click.option(
    "-l", "--list", "list_only", is_flag=True, help="List the available firmwares."
)
@click.option(
    "--refresh", is_flag=True, help="Re-check GitHub for a newer release now."
)
@click.option("--full", is_flag=True, help="Show full descriptions (do not truncate).")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
@target_options
def flash(firmware, list_only, refresh, full, yes, port, device_id):
    """Download and flash a BomberCat firmware image.

    FIRMWARE is a name from `--list`, or the path to a local .uf2.

    \b
        bombercat flash --list          # what is available
        bombercat flash NFCGate         # download (if needed) and flash
        bombercat flash NFCGate -d 2    # pick a board by ID
        bombercat flash ./mine.uf2      # flash a local image
    """
    cache = ReleaseCache()

    if list_only or not firmware:
        try:
            _ensure_cache(cache, refresh)
        except FirmwareError as e:
            print_error(str(e))
            raise SystemExit(1)
        _show_list(cache, full)
        if not firmware:
            return
        return

    try:
        image = _resolve_image(cache, firmware, refresh)
        in_bootloader = find_uf2_drive() is not None
        target = _resolve_target(port, device_id, in_bootloader)
    except FirmwareError as e:
        print_error(str(e))
        raise SystemExit(1)

    where = target or f"the {DRIVE_LABEL} drive"
    if not yes:
        print_info(f"About to flash {image.name} ({human_size(image.stat().st_size)})")
        if not click.confirm(f"  Write it to {where}?", default=False):
            print_dim("Nothing was written.")
            return

    try:
        outcome = write_image(image, target, progress=print_dim)
    except BootloaderTimeout:
        _bootloader_help(firmware)
        raise SystemExit(1)
    except FirmwareError as e:
        print_error(str(e))
        raise SystemExit(1)
    except Exception as e:  # serial/OS errors we did not anticipate
        print_error(f"{type(e).__name__}: {e}")
        raise SystemExit(1)

    print_success(f"{image.name} written to {outcome.drive}")
    if outcome.port:
        print_info(f"The board came back on {outcome.port}")
        if image.stem.lower() == "nfcgate":
            print_dim("Check it with:  bombercat device info")
    else:
        print_warning(
            "The board did not re-enumerate as a serial port within the "
            "timeout. Unplug and replug it, then run `bombercat device list`."
        )
