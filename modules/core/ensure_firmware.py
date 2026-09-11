#!/usr/bin/env python3

# Electronic Cats
# ensure_firmware.py — the auto-flash orchestrator (docs/AUTOFLASH_PLAN.md F3).
# Turns "I know what firmware is on the board" (firmwares.detect_firmware) +
# "I know how to flash one" (firmware.flasher.flash) into "this command needs
# a capability, and I'll fix the board if it's missing and the user allows it".
# Distributed as-is; no warranty is given.

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

import click

from ..firmware.flasher import FlashOutcome
from ..firmware.flasher import flash as write_image
from ..firmware.releases import FirmwareError, ReleaseCache
from ..firmware.uf2 import BootloaderTimeout, bootloader_help
from ..utils.output import print_dim, print_warning
from .bombercat import DeviceError
from .exceptions import EXIT_FIRMWARE, FirmwareMismatch
from .firmwares import BANNER, INFERRED, NONE, USB, DetectionResult, detect_firmware
from .requirements import Requirement, satisfied_by

AUTO_FLASH_ENV = "BOMBERCAT_AUTO_FLASH"


class AutoFlashPolicy(str, Enum):
    NEVER = "never"
    ASK = "ask"
    ALWAYS = "always"


def resolve_policy(flag: Optional[bool] = None) -> AutoFlashPolicy:
    """Flag > BOMBERCAT_AUTO_FLASH > (ASK on a TTY, NEVER otherwise).

    See docs/AUTOFLASH_PLAN.md D-3.4: a script piping stdin never gets
    flashed without an explicit --auto-flash/env opt-in.
    """
    if flag is not None:
        return AutoFlashPolicy.ALWAYS if flag else AutoFlashPolicy.NEVER
    env = os.environ.get(AUTO_FLASH_ENV)
    if env:
        return AutoFlashPolicy(env.strip().lower())
    return AutoFlashPolicy.ASK if sys.stdin.isatty() else AutoFlashPolicy.NEVER


@dataclass
class EnsureOutcome:
    port: str
    detection: DetectionResult
    flashed: bool


def _flash_provider(req: Requirement, port: str) -> FlashOutcome:
    cache = ReleaseCache()
    cache.refresh_or_warn(force=False)
    image = cache.find(req.image_name)
    if image is None:
        raise FirmwareError(
            f"the release has no image named '{req.image_name}'.",
            hint=["bombercat flash --list", "bombercat flash --refresh"],
        )
    return write_image(image.path, port, progress=print_dim)


def _mismatch_message(req: Requirement, detection: DetectionResult) -> str:
    # States what's on the board now, what the command needs flashed instead,
    # and — since flashing rewrites the whole image, not just a firmware
    # component (docs/AUTOFLASH_PLAN.md D2/D3) — what leaving nfcgate costs
    # specifically: its saved WiFi/relay config (F6, risk R3).
    message = (
        f"`{req.command}` needs {req.provider.display}; this board is "
        f"running {detection.firmware.display}."
    )
    if detection.confidence == INFERRED:
        message += " (inferred from an old REPL reply, not certain)"
    if detection.firmware.id == "nfcgate":
        message += (
            " Flashing over it erases its saved WiFi/relay config — check it "
            "first with `bombercat config show` if you'll need it again."
        )
    return message


def ensure_firmware(
    port: str,
    usb_tagged: bool,
    req: Requirement,
    *,
    policy: AutoFlashPolicy,
    sniff: bool = True,
    detect_fn: Callable[..., DetectionResult] = detect_firmware,
    flash_fn: Optional[Callable[[Requirement, str], FlashOutcome]] = None,
    confirm_fn: Callable[[str], bool] = click.confirm,
    verify_retries: int = 2,
    verify_delay: float = 1.0,
) -> EnsureOutcome:
    """Make sure `port` can do `req.capability`, flashing it if allowed to.

    Follows the confidence table in docs/AUTOFLASH_PLAN.md D-3.6 and the
    consent policy in D-3.4/D-3.5. Never flashes twice in one call (R4): one
    attempt, and a failed re-verification afterwards warns instead of
    retrying the flash (D-3.7).
    """
    flash_fn = flash_fn or _flash_provider

    detection = detect_fn(port, sniff=sniff, usb_present=usb_tagged)

    if detection.confidence == NONE:
        raise DeviceError(f"nothing responded on {port}.")

    if satisfied_by(detection.firmware, req.capability):
        if detection.confidence == BANNER:
            print_warning(
                f"{detection.firmware.display} identified from its boot "
                "banner, not confirmed by a handshake — continuing."
            )
        return EnsureOutcome(port=port, detection=detection, flashed=False)

    message = _mismatch_message(req, detection)
    hint = [f"bombercat flash {req.image_name}"]
    if policy == AutoFlashPolicy.NEVER:
        raise FirmwareMismatch(message, hint=hint)

    must_confirm = policy == AutoFlashPolicy.ASK or detection.confidence == USB
    if must_confirm and not confirm_fn(f"{message} Flash it now?"):
        raise FirmwareMismatch(message, hint=hint)

    try:
        outcome = flash_fn(req, port)
    except BootloaderTimeout:
        # Same panel `bombercat flash` shows for this failure (F5): a bare
        # BootloaderTimeout message reaching main_cli()'s generic
        # `except BomberCatError` would read as "no RPI-RP2 drive appeared"
        # with no next step, hiding the udisks2 / manual-mount fix.
        bootloader_help(req.image_name)
        raise SystemExit(EXIT_FIRMWARE)
    if outcome.port is None:
        raise FirmwareError(
            "the board did not come back as a serial port after flashing. "
            "Unplug and replug it, then run `bombercat device list`."
        )
    new_port = outcome.port

    final = detect_fn(new_port, sniff=sniff, usb_present=True)
    for _ in range(verify_retries):
        if satisfied_by(final.firmware, req.capability):
            break
        time.sleep(verify_delay)
        final = detect_fn(new_port, sniff=sniff, usb_present=True)
    if not satisfied_by(final.firmware, req.capability):
        print_warning(
            f"flashed {req.provider.display} but could not confirm it "
            "afterwards — the device may still work."
        )

    return EnsureOutcome(port=new_port, detection=final, flashed=True)
