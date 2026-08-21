#!/usr/bin/env python3

# Electronic Cats
# `bombercat flash` — list (and, from Fase B on, write) the prebuilt .uf2
# firmware images published by ElectronicCats/bombercat-firmware, so putting a
# firmware on a board no longer needs the ~1 GB arduino-cli toolchain.
# Design and phases: docs/FLASH_PLAN.md.
#
# The network is only touched from here, on demand: the cache below is filled
# lazily by the first command that needs it, never in a constructor
# (FLASH_PLAN §2.3.1).
# Distributed as-is; no warranty is given.

import click
from rich.table import Table
from rich.text import Text

from .releases import FirmwareError, ReleaseCache
from ..utils.output import (
    console,
    print_error,
    print_info,
    print_warning,
)


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


@click.command("flash", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("firmware", required=False)
@click.option(
    "-l", "--list", "list_only", is_flag=True, help="List the available firmwares."
)
@click.option(
    "--refresh", is_flag=True, help="Re-check GitHub for a newer release now."
)
@click.option("--full", is_flag=True, help="Show full descriptions (do not truncate).")
def flash(firmware, list_only, refresh, full):
    """Download and flash a BomberCat firmware image.

    \b
        bombercat flash --list          # what is available
        bombercat flash --list --full   # with the full descriptions
        bombercat flash --refresh -l    # force a re-check against GitHub
    """
    cache = ReleaseCache()

    try:
        _ensure_cache(cache, refresh)
    except FirmwareError as e:
        print_error(str(e))
        raise SystemExit(1)

    if firmware and not list_only:
        # Fase B (docs/FLASH_PLAN.md §4) — the UF2 write itself.
        print_error("flashing is not implemented yet.")
        print_info(
            "`bombercat flash --list` shows what is published; flash it for "
            "now with flash_bombercat.sh from the firmware repo."
        )
        raise SystemExit(1)

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
