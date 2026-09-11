#!/usr/bin/env bash
set -euo pipefail

# Electronic Cats
# build_mac.sh — builds bombercat-<version>.pkg for whichever macOS
# architecture this script runs on (Intel or arm64; build-mac.yml runs it
# once per arch and renames the result).
#
# No native dependencies to bundle here (see docs/PACKAGING_PLAN.md §4.1):
# PyInstaller freezes a plain --onedir tree and pkgbuild wraps it, no
# `brew install` needed. Reproducible locally on a Mac; the workflow only
# calls this script.
#
# Usage: ./build_mac.sh [version]
#   version defaults to the contents of VERSION.
# Distributed as-is; no warranty is given.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

VERSION="${1:-$(tr -d '[:space:]' < VERSION)}"
PKG_NAME="bombercat"
IDENTIFIER="com.electroniccats.$PKG_NAME"
PKG_ROOT="$REPO_ROOT/pkg_root"
OUT_FILE="$REPO_ROOT/${PKG_NAME}-${VERSION}.pkg"

echo "Building ${PKG_NAME} ${VERSION} (.pkg) ..."

rm -rf build dist "$PKG_ROOT" "$OUT_FILE"

pyinstaller --onedir --noupx --noconfirm --name "$PKG_NAME" \
    --collect-all rich \
    --collect-data certifi \
    --hidden-import click \
    --hidden-import serial.tools.list_ports \
    --add-data "VERSION:." \
    --add-data "modules/tags/mifare/data:modules/tags/mifare/data" \
    bombercat.py

mkdir -p "$PKG_ROOT/usr/local/opt/$PKG_NAME" "$PKG_ROOT/usr/local/bin"
cp -r "dist/$PKG_NAME"/. "$PKG_ROOT/usr/local/opt/$PKG_NAME/"
ln -sf "/usr/local/opt/$PKG_NAME/$PKG_NAME" "$PKG_ROOT/usr/local/bin/$PKG_NAME"

pkgbuild --root "$PKG_ROOT" \
    --identifier "$IDENTIFIER" \
    --version "$VERSION" \
    --install-location / \
    "$OUT_FILE"

echo "Built $OUT_FILE"
