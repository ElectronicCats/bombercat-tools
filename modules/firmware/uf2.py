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

    Walks every block rather than just the first (docs/AUDIT_ERROR_HANDLING.md
    M6): a local `flash ./mine.uf2` gets no other check, so a file that is
    truncated-but-block-aligned or corrupted past the first 512 bytes must not
    pass. Checks, per block: UF2 magic, a numBlocks that matches the file's
    actual block count, in-order blockNo, and — cheap insurance, since the
    bootloader silently ignores blocks whose family does not match, so a
    wrong-chip image would otherwise "flash" successfully and leave a board
    that never comes back — a consistent RP2040 family id.
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

    total_blocks = size // BLOCK_SIZE
    expected_family: Optional[int] = None
    with path.open("rb") as fh:
        for index in range(total_blocks):
            block = fh.read(BLOCK_SIZE)
            (
                start0,
                start1,
                flags,
                _target_addr,
                _payload_size,
                block_no,
                num_blocks,
                family,
            ) = struct.unpack("<8I", block[:32])
            (end,) = struct.unpack("<I", block[-4:])

            if (start0, start1, end) != (MAGIC_START0, MAGIC_START1, MAGIC_END):
                raise FirmwareError(
                    f"{path.name} is not a UF2 image (block {index} has no "
                    "UF2 magic)."
                )
            if num_blocks != total_blocks:
                raise FirmwareError(
                    f"{path.name} is corrupt: block {index} claims "
                    f"{num_blocks} total blocks, but the file has "
                    f"{total_blocks}."
                )
            if block_no != index:
                raise FirmwareError(
                    f"{path.name} is corrupt: block {index} is out of order "
                    f"(it claims to be block {block_no})."
                )
            if flags & FLAG_FAMILY_ID_PRESENT:
                if expected_family is None:
                    expected_family = family
                    if family != RP2040_FAMILY_ID:
                        raise FirmwareError(
                            f"{path.name} is built for chip family "
                            f"0x{family:08X}, not the RP2040 "
                            f"(0x{RP2040_FAMILY_ID:08X}) the BomberCat uses. "
                            "The bootloader would ignore every block."
                        )
                elif family != expected_family:
                    raise FirmwareError(
                        f"{path.name} is corrupt: block {index} declares "
                        f"chip family 0x{family:08X}, different from block "
                        f"0's 0x{expected_family:08X}."
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


DRIVE_REMOTE = 4  # winapi.h DRIVE_REMOTE — a mapped network drive


def _windows_candidates() -> List[Path]:
    """Drive letters actually in use, minus mapped network drives.

    `GetDriveTypeW` answers from local OS state without touching the drive;
    a subsequent `is_file()` probe against an unreachable mapped network
    share, by contrast, can hang for the OS's full network timeout per
    letter (docs/AUDIT_ERROR_HANDLING.md L11). No pywin32 needed — these are
    plain kernel32 calls via ctypes (stdlib).
    """
    import ctypes

    kernel32 = ctypes.windll.kernel32
    in_use = kernel32.GetLogicalDrives()
    candidates = []
    for i, letter in enumerate("CDEFGHIJKLMNOPQRSTUVWXYZ", start=2):
        if not (in_use & (1 << i)):
            continue
        root = f"{letter}:\\"
        if kernel32.GetDriveTypeW(root) == DRIVE_REMOTE:
            continue
        candidates.append(Path(f"{letter}:/"))
    return candidates


def _candidates() -> List[Path]:
    system = platform.system()
    if system == "Darwin":
        return _macos_candidates()
    if system == "Windows":
        return _windows_candidates()
    return _linux_candidates()


def find_uf2_drive() -> Optional[Path]:
    """The drive actually named RPI-RP2, or None if it is not mounted.

    Family-id validation in `validate_uf2` does not save a Pico or a
    CatSniffer sitting in its own UF2 bootloader on the same bench — they
    share the RP2040 family id. Picking any other mounted UF2 drive as a
    fallback would risk writing the wrong board's firmware to it, so an
    unlabelled/mislabelled drive is treated the same as "board not in
    BOOTSEL" (docs/AUDIT_ERROR_HANDLING.md M5): the caller's existing
    "waiting for the RPI-RP2 drive" timeout / help panel covers it.
    """
    found: List[Path] = []
    for candidate in _candidates():
        try:
            if (candidate / INFO_FILE).is_file():
                found.append(candidate)
        except OSError:
            # An unreadable or disconnected mount point: not our drive.
            continue
    return next((p for p in found if p.name.upper() == DRIVE_LABEL), None)


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
