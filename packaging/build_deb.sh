#!/usr/bin/env bash
set -euo pipefail

# Electronic Cats
# build_deb.sh — builds bombercat-<version>.deb at the repo root.
#
# Vendors runtime dependencies into the package instead of depending on
# system python3-* packages (see docs/PACKAGING_PLAN.md §1.2): the result
# only needs `python3`, no venv on the target machine. Reproducible in CI
# and locally — build-deb.yml only invokes this script.
#
# Usage: packaging/build_deb.sh [version]
#   version defaults to the contents of VERSION.
# Distributed as-is; no warranty is given.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VERSION="${1:-$(tr -d '[:space:]' < VERSION)}"
PKG_NAME="bombercat"
BUILD_DIR="$REPO_ROOT/packaging/build_deb"
PKG_DIR="$BUILD_DIR/usr/lib/python3/dist-packages/$PKG_NAME"
OUT_FILE="$REPO_ROOT/${PKG_NAME}-${VERSION}.deb"

echo "Building ${PKG_NAME} ${VERSION} (.deb) ..."

rm -rf "$BUILD_DIR" "$OUT_FILE"
mkdir -p "$BUILD_DIR"
cp -r packaging/debian/. "$BUILD_DIR/"
find "$BUILD_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

sed "s/@VERSION@/${VERSION}/" packaging/debian/DEBIAN/control > "$BUILD_DIR/DEBIAN/control"

# `bombercat` must be an importable package (not just a top-level module) so
# usr/bin/bombercat can do `import bombercat` and reach `bombercat.modules...`.
mkdir -p "$PKG_DIR"
cp -r modules "$PKG_DIR/modules"
cp VERSION "$PKG_DIR/VERSION"
touch "$PKG_DIR/__init__.py"
find "$PKG_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

echo "Vendoring runtime dependencies into ${PKG_DIR#$BUILD_DIR/}/vendor ..."
pip install --target "$PKG_DIR/vendor" --no-compile --break-system-packages \
    -r requirements.txt

chmod 644 "$BUILD_DIR"/DEBIAN/*
chmod 755 "$BUILD_DIR/DEBIAN/postinst"
chmod 755 "$BUILD_DIR/usr/bin/$PKG_NAME"

dpkg-deb --build --root-owner-group "$BUILD_DIR" "$OUT_FILE"

echo "Built $OUT_FILE"
