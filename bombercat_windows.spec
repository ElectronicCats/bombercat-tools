# -*- mode: python ; coding: utf-8 -*-
# Electronic Cats
# bombercat_windows.spec — PyInstaller spec for the Windows build
# (build-windows.yml and build_windows.bat both just run
# `pyinstaller bombercat_windows.spec`).
#
# --onedir (not --onefile): matches build_mac.sh, and it is what
# packaging/windows/bombercat_installer.iss expects under its
# `dist\bombercat\*` [Files] entry. pywin32's hiddenimports are needed for
# the named-pipes-to-Wireshark path (modules/core/pipes.py); nothing else in
# this project touches Windows-only APIs. Distributed as-is; no warranty is
# given.

from PyInstaller.utils.hooks import collect_data_files

a = Analysis(
    ["bombercat.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("VERSION", "."),
        ("modules/tags/mifare/data", "modules/tags/mifare/data"),
        *collect_data_files("certifi"),
    ],
    hiddenimports=[
        "click",
        "serial.tools.list_ports",
        "win32file",
        "win32pipe",
        "win32event",
        "win32api",
        "pywintypes",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="bombercat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="bombercat",
)
