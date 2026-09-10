#!/usr/bin/env python3

# Electronic Cats
# _version.py — the version string, resolved wherever the CLI happens to run
# from: a source checkout, a PyInstaller bundle (macOS/Windows), or an
# installed package (.deb/.pkg.tar.zst/pip). `VERSION` at the repo root is the
# single source of truth; everything here is about *finding* it.
# Distributed as-is; no warranty is given.

from __future__ import annotations

import sys
from pathlib import Path


def _read(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _resolve_version() -> str:
    # 1. PyInstaller bundle: VERSION is shipped at the root of the extracted
    #    tree (`--add-data VERSION:.`), where no repo layout exists.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        version = _read(Path(meipass) / "VERSION")
        if version:
            return version

    # 2. Source checkout, and installed Linux packages: VERSION sits two
    #    levels up from modules/utils/ (repo root, or the package dir the
    #    .deb/.pkg.tar.zst recipe copies it into).
    version = _read(Path(__file__).resolve().parents[2] / "VERSION")
    if version:
        return version

    # 3. `pip install .`: setup.py stamped the version into the dist metadata.
    try:
        from importlib.metadata import PackageNotFoundError, version as _dist_version

        return _dist_version("bombercat")
    except (ImportError, PackageNotFoundError):
        pass

    return "unknown"


__version__ = _resolve_version()
