#!/usr/bin/env bash
set -euo pipefail

# Electronic Cats
# build_arch.sh — builds a bombercat pkg.tar.zst via makepkg, dropped in
# packaging/ (the caller — build-arch.yml, or a human — moves/renames it).
#
# Same vendoring pattern as build_deb.sh, under site-packages/<pyver> instead
# of dist-packages, with a minimal PKGBUILD generated on the fly (see
# docs/PACKAGING_PLAN.md §1.4). Meant to run as root inside an Arch container
# with base-devel + python already installed; makepkg itself refuses to run
# as root, so this script creates an unprivileged `builder` user for it.
#
# Usage: packaging/build_arch.sh [version]
#   version defaults to the contents of VERSION.
# Distributed as-is; no warranty is given.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VERSION="${1:-$(tr -d '[:space:]' < VERSION)}"
PKG_NAME="bombercat"
BUILD_ROOT="$REPO_ROOT/packaging/build_arch"
PY_VER="$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
SITE_PKGS="usr/lib/python${PY_VER}/site-packages/${PKG_NAME}"
ROOT_TREE="$BUILD_ROOT/usr"

echo "Building ${PKG_NAME} ${VERSION} (.pkg.tar.zst, python ${PY_VER}) ..."

rm -rf "$BUILD_ROOT"
mkdir -p "$BUILD_ROOT/$SITE_PKGS"

cp -r modules "$BUILD_ROOT/$SITE_PKGS/modules"
cp VERSION "$BUILD_ROOT/$SITE_PKGS/VERSION"
touch "$BUILD_ROOT/$SITE_PKGS/__init__.py"
find "$BUILD_ROOT/$SITE_PKGS" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

echo "Vendoring runtime dependencies into ${SITE_PKGS}/vendor ..."
pip install --target "$BUILD_ROOT/$SITE_PKGS/vendor" --no-compile --break-system-packages \
    -r requirements.txt

install -Dm755 packaging/debian/usr/bin/bombercat "$ROOT_TREE/bin/bombercat"
install -Dm644 packaging/debian/usr/share/applications/bombercat.desktop \
    "$ROOT_TREE/share/applications/bombercat.desktop"
install -Dm644 packaging/debian/lib/udev/rules.d/99-bombercat.rules \
    "$ROOT_TREE/lib/udev/rules.d/99-bombercat.rules"

# options=('!debug') skips the second, debug-symbols .pkg.tar.zst makepkg
# would otherwise produce; package() just relocates the tree built above.
cat > "$BUILD_ROOT/PKGBUILD" <<EOF
pkgname=$PKG_NAME
pkgver=$VERSION
pkgrel=1
pkgdesc="BomberCat CLI — NFC relay, tag/reader detection and magspoof"
arch=('any')
url="https://github.com/ElectronicCats/bombercat-tools"
license=('GPL3')
depends=('python')
optdepends=('udisks2: auto-mount the RPI-RP2 bootloader drive for bombercat flash')
options=('!debug')

package() {
    cp -r "\$srcdir"/../usr "\$pkgdir/"
}
EOF

id -u builder &>/dev/null || useradd -m builder
chown -R builder:builder "$BUILD_ROOT"

(cd "$BUILD_ROOT" && sudo -u builder makepkg -f --noconfirm)

mv "$BUILD_ROOT"/${PKG_NAME}-${VERSION}-1-any.pkg.tar.zst "$REPO_ROOT/packaging/"

echo "Built packaging/${PKG_NAME}-${VERSION}-1-any.pkg.tar.zst"
