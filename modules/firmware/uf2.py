#!/usr/bin/env python3

# Electronic Cats
# uf2.py — the UF2 container format and the bootloader drive it gets copied to
# (docs/FLASH_PLAN.md §3.3).
#
# The RP2040 has no serial bootloader protocol to speak: held in BOOTSEL it
# enumerates as a USB mass-storage device named RPI-RP2, and "flashing" is
# copying a file onto it. So this module only has to answer two questions —
# is this file a UF2 for *this* chip, and where is the drive mounted.
# Distributed as-is; no warranty is given.

from __future__ import annotations

import glob
import platform
import re
import struct
import time
from pathlib import Path
from typing import List, Optional

from .releases import FirmwareError

# UF2 block layout (github.com/microsoft/uf2). Every block is 512 bytes: a
# 32-byte header, 476 bytes of payload area and a 4-byte trailing magic.
BLOCK_SIZE = 512
MAGIC_START0 = 0x0A324655  # b"UF2\n"
MAGIC_START1 = 0x9E5D5157
MAGIC_END = 0x0AB16F30
FLAG_FAMILY_ID_PRESENT = 0x00002000
RP2040_FAMILY_ID = 0xE48BFF56

INFO_FILE = "INFO_UF2.TXT"  # every UF2 bootloader drive carries one
DRIVE_LABEL = "RPI-RP2"

DRIVE_TIMEOUT = 15.0  # the drive shows up in ~1 s; udisks can be slower
POLL_INTERVAL = 0.25


class BootloaderTimeout(FirmwareError):
    """No RPI-RP2 drive showed up in time.

    Its own type because the fix is a physical one — double-tap RESET — and the
    command layer answers it with instructions rather than a one-line error.
    """


def validate_uf2(path: Path) -> None:
    """Raise unless `path` is a UF2 image this board's bootloader will take.

    Cheap insurance: the bootloader silently ignores blocks whose family does
    not match, so a wrong-chip image would "flash" successfully and leave a
    board that never comes back.
    """
    try:
        size = path.stat().st_size
    except OSError as e:
        raise FirmwareError(f"cannot read {path}: {e}") from e

    if size == 0 or size % BLOCK_SIZE:
        raise FirmwareError(
            f"{path.name} is not a UF2 image: {size} bytes is not a multiple "
            f"of the {BLOCK_SIZE}-byte UF2 block."
        )

    with path.open("rb") as fh:
        block = fh.read(BLOCK_SIZE)

    start0, start1, flags = struct.unpack("<3I", block[:12])
    (family,) = struct.unpack("<I", block[28:32])
    (end,) = struct.unpack("<I", block[-4:])

    if (start0, start1, end) != (MAGIC_START0, MAGIC_START1, MAGIC_END):
        raise FirmwareError(
            f"{path.name} is not a UF2 image (its first block has no UF2 magic)."
        )

    if flags & FLAG_FAMILY_ID_PRESENT and family != RP2040_FAMILY_ID:
        raise FirmwareError(
            f"{path.name} is built for chip family 0x{family:08X}, not the "
            f"RP2040 (0x{RP2040_FAMILY_ID:08X}) the BomberCat uses. The "
            "bootloader would ignore every block."
        )


# ── Finding the bootloader drive ─────────────────────────────────────────────


def _unescape_mount(path: str) -> str:
    """/proc/mounts octal-escapes spaces and friends (`\\040`)."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), path)


def _linux_candidates() -> List[Path]:
    mounts: List[Path] = []
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            fields = line.split()
            # device, mountpoint, fstype, ...
            if len(fields) >= 3 and fields[2] == "vfat":
                mounts.append(Path(_unescape_mount(fields[1])))
    except OSError:
        pass
    # Fallback for setups whose /proc/mounts we cannot read or that mount the
    # drive somewhere unusual.
    for pattern in (
        f"/media/*/{DRIVE_LABEL}",
        f"/media/*/*/{DRIVE_LABEL}",
        f"/run/media/*/*/{DRIVE_LABEL}",
        f"/mnt/*/{DRIVE_LABEL}",
        f"/mnt/{DRIVE_LABEL}",
    ):
        mounts.extend(Path(p) for p in glob.glob(pattern))
    return mounts


def _macos_candidates() -> List[Path]:
    return [Path(p) for p in glob.glob("/Volumes/*")]


def _windows_candidates() -> List[Path]:
    return [Path(f"{letter}:/") for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ"]


def _candidates() -> List[Path]:
    system = platform.system()
    if system == "Darwin":
        return _macos_candidates()
    if system == "Windows":
        return _windows_candidates()
    return _linux_candidates()


def find_uf2_drive() -> Optional[Path]:
    """The mounted UF2 bootloader drive, or None if the board is not in BOOTSEL.

    A drive actually named RPI-RP2 wins over any other mounted UF2 board (a
    Pico, a CatSniffer) so that a second board on the bench cannot quietly
    become the flash target.
    """
    found: List[Path] = []
    for candidate in _candidates():
        try:
            if (candidate / INFO_FILE).is_file():
                found.append(candidate)
        except OSError:
            # An unreadable or disconnected mount point: not our drive.
            continue
    if not found:
        return None
    return next((p for p in found if p.name.upper() == DRIVE_LABEL), found[0])


def wait_for_uf2_drive(timeout: float = DRIVE_TIMEOUT) -> Path:
    """Poll until the bootloader drive is mounted. Raises BootloaderTimeout."""
    deadline = time.monotonic() + timeout
    while True:
        drive = find_uf2_drive()
        if drive is not None:
            return drive
        if time.monotonic() >= deadline:
            raise BootloaderTimeout(
                f"no {DRIVE_LABEL} drive appeared within {timeout:.0f} s."
            )
        time.sleep(POLL_INTERVAL)


def wait_for_drive_gone(drive: Path, timeout: float = DRIVE_TIMEOUT) -> bool:
    """Wait for `drive` to unmount — the board rebooting after a write."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not (drive / INFO_FILE).exists():
            return True
        time.sleep(POLL_INTERVAL)
    return False


def unmounted_rp2_device() -> Optional[str]:
    """The RPI-RP2 block device when it exists but nothing mounted it.

    The headless / no-udisks case (FLASH_PLAN §3.4, risk #3): the board *is* in
    bootloader, but mounting it needs privileges we should not take, so the
    command layer turns this into "run this mount command".
    """
    if platform.system() != "Linux":
        return None
    link = Path("/dev/disk/by-label") / DRIVE_LABEL
    if not link.exists():
        return None
    if find_uf2_drive() is not None:
        return None
    try:
        return str(link.resolve())
    except OSError:
        return str(link)
