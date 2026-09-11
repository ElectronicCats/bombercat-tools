#!/usr/bin/env python3

# Electronic Cats
# exceptions.py — typed CLI errors with exit codes, ported from catnip's
# core/exceptions.py and reduced to what BomberCat needs
# (docs/AUTOFLASH_PLAN.md F2). Distinguishing "wrong firmware" (exit 3) from
# everything else (exit 1) lets a script branch on *why* a command failed
# instead of parsing stderr — the same value `DeviceError` already gives for
# "no device".
# Distributed as-is; no warranty is given.

from __future__ import annotations

from typing import List, Optional

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_FIRMWARE = 3
EXIT_CONNECTION = 4
EXIT_INTERRUPT = 130


class BomberCatError(Exception):
    """Base for CLI errors carrying an exit code and an actionable hint.

    Caught in `core.cli.main_cli()`, which prints the message plus each hint
    line and exits with `exit_code`.
    """

    exit_code = EXIT_ERROR

    def __init__(self, message: str, *, hint: Optional[List[str]] = None) -> None:
        super().__init__(message)
        self.hint = hint


class FirmwareMismatch(BomberCatError):
    """The firmware flashed on the board cannot do what was asked of it."""

    exit_code = EXIT_FIRMWARE
