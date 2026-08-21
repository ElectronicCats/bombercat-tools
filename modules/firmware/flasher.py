#!/usr/bin/env python3

# Electronic Cats
# flasher.py — putting a .uf2 on the board (docs/FLASH_PLAN.md §3.4).
#
# The whole sequence: reboot the board into its UF2 bootloader with a 1200-bps
# touch, wait for the RPI-RP2 drive, copy the image onto it, and wait for the
# board to come back as a serial port. If the drive is already mounted when we
# start (the user double-tapped RESET) everything before the copy is skipped.
#
# No printing here — the caller passes a `progress` callback and owns the
# output, the same split the rest of the repo uses.
# Distributed as-is; no warranty is given.

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Set

import serial

from ..core.usb_connection import list_ports_info
from .releases import FirmwareError
from .uf2 import find_uf2_drive, validate_uf2, wait_for_uf2_drive

# The RP2040 Arduino core reboots into BOOTSEL when the host opens its CDC at
# 1200 baud and drops DTR — the same trick `rp2040load` uses.
BOOTSEL_BAUD = 1200
TOUCH_SETTLE = 0.05

PORT_GONE_TIMEOUT = 5.0  # the CDC disappears as the board resets
DRIVE_TIMEOUT = 15.0  # ... and comes back as a mass-storage device
PORT_BACK_TIMEOUT = 15.0  # ... then as a CDC again once flashed
POLL_INTERVAL = 0.2

COPY_CHUNK = 64 * 1024


@dataclass
class FlashOutcome:
    """What actually happened, for the command layer to report."""

    image: Path
    drive: Path
    touched: bool  # did we reboot the board ourselves?
    port: Optional[str]  # the serial port it came back on, if it did


def _ports() -> Set[str]:
    return {p.device for p in list_ports_info(include_all=True)}


def enter_bootloader(port: str) -> None:
    """The 1200-bps touch that reboots the board into BOOTSEL."""
    link = serial.Serial()
    link.port = port
    link.baudrate = BOOTSEL_BAUD
    try:
        link.open()
        link.dtr = False
        time.sleep(TOUCH_SETTLE)
    except (serial.SerialException, OSError) as e:
        raise FirmwareError(f"could not open {port} to reboot the board: {e}") from e
    finally:
        try:
            link.close()
        except (serial.SerialException, OSError):
            # The board may already be gone by the time we close — that is the
            # touch working, not a failure.
            pass


def wait_for_port_gone(port: str, timeout: float = PORT_GONE_TIMEOUT) -> bool:
    """Wait for `port` to disappear, i.e. for the reset to have taken."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port not in _ports():
            return True
        time.sleep(POLL_INTERVAL)
    return False


def wait_for_new_port(
    known: Set[str], timeout: float = PORT_BACK_TIMEOUT
) -> Optional[str]:
    """Wait for a serial port that was not in `known` — the board, reflashed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        new = _ports() - known
        if new:
            return sorted(new)[0]
        time.sleep(POLL_INTERVAL)
    return None


def copy_uf2(image: Path, drive: Path) -> None:
    """Copy the image onto the bootloader drive.

    The bootloader reboots the board the moment it has the last block, often
    before the kernel has finished the write — so an OSError *after* every byte
    was handed over is the flash succeeding, not failing (FLASH_PLAN §3.4).
    Only a short write is a real error.
    """
    data = image.read_bytes()
    target = drive / image.name
    written = 0
    try:
        with target.open("wb") as fh:
            for offset in range(0, len(data), COPY_CHUNK):
                chunk = data[offset : offset + COPY_CHUNK]
                fh.write(chunk)
                written += len(chunk)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as e:
        if written < len(data):
            raise FirmwareError(
                f"copying {image.name} to {drive} failed after "
                f"{written} of {len(data)} bytes: {e}"
            ) from e
        # Every byte was written; the drive vanishing underneath us is the
        # board restarting into the firmware we just gave it.
        return

    sync = getattr(os, "sync", None)  # not on Windows
    if sync is not None:
        try:
            sync()
        except OSError:
            pass


def flash(
    image: Path,
    port: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
    drive_timeout: float = DRIVE_TIMEOUT,
    port_timeout: float = PORT_BACK_TIMEOUT,
) -> FlashOutcome:
    """Validate `image` and write it to the board. Raises FirmwareError.

    `port` is only needed when the board is still running its firmware; if the
    RPI-RP2 drive is already mounted the board is in BOOTSEL and there is no
    port to talk to.
    """
    say = progress or (lambda message: None)

    # Before anything reboots: a bad image should not cost the user a board
    # sitting in bootloader wondering what happened.
    validate_uf2(image)

    drive = find_uf2_drive()
    touched = False

    if drive is None:
        if not port:
            raise FirmwareError(
                "no board to flash: nothing is in bootloader mode and no "
                "serial port was given."
            )
        say(f"rebooting {port} into the UF2 bootloader (1200-bps touch)")
        enter_bootloader(port)
        touched = True
        wait_for_port_gone(port)
        say("waiting for the RPI-RP2 drive")
        drive = wait_for_uf2_drive(drive_timeout)

    say(f"copying {image.name} ({image.stat().st_size} bytes) to {drive}")
    known = _ports()
    copy_uf2(image, drive)

    say("waiting for the board to come back")
    back = wait_for_new_port(known, port_timeout)

    return FlashOutcome(image=image, drive=drive, touched=touched, port=back)
