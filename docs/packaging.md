# Building the packages locally

`bombercat` ships as four platform-native artifacts, all built from the same
`VERSION` file: `.deb`, `.pkg.tar.zst` (Arch), `.pkg` (macOS) and `.exe`
(Windows). CI builds and uploads all four on every push to `main` and on each
release (see [`.github/workflows/build-*.yml`](../.github/workflows/build-deb.yml));
every recipe also runs as a local script or `make` target, so you don't need
CI to reproduce a build. Full design rationale lives in
[`PACKAGING_PLAN.md`](PACKAGING_PLAN.md); this page is just the "how do I run
it" reference.

No native dependencies to install anywhere — `click`/`rich`/`pyserial`/
`Pygments`/`markdown-it-py` are pure Python, and flashing is UF2 mass-storage,
not OpenOCD/libusb. That's what keeps every recipe below this short.

## `.deb` (Debian/Ubuntu)

Prerequisites: `python3`, `pip`, `dpkg-deb` (already on any Debian-family
system).

```sh
make deb          # or: bash packaging/build_deb.sh [version]
```

Produces `bombercat-<version>.deb` at the repo root. The package **vendors**
its dependencies into `.../dist-packages/bombercat/vendor/` — it only depends
on `python3` on the target machine, no venv needed there.

```sh
sudo apt install ./bombercat-<version>.deb
bombercat --version
```

## `.pkg.tar.zst` (Arch)

Prerequisites: `base-devel`, `python`, `sudo` (the script creates an
unprivileged `builder` user — `makepkg` refuses to run as root).

```sh
make arch          # or: bash packaging/build_arch.sh [version]
```

Produces `packaging/bombercat-<version>-1-any.pkg.tar.zst`. Same vendoring
pattern as the `.deb`, under `site-packages/<python-version>/` instead of
`dist-packages/`.

```sh
sudo pacman -U packaging/bombercat-<version>-1-any.pkg.tar.zst
bombercat --version
```

## `.pkg` (macOS)

Prerequisites: Xcode command line tools (for `pkgbuild`, preinstalled on any
dev machine), `pip install pyinstaller`.

```sh
python3 -m pip install -r requirements.txt pyinstaller
./build_mac.sh [version]
```

PyInstaller freezes a `--onedir` tree at `dist/bombercat/`; `pkgbuild` wraps
it into `bombercat-<version>.pkg`, installing to `/usr/local/opt/bombercat/`
with a `/usr/local/bin/bombercat` symlink. Run it on the architecture you want
to ship — CI builds `x86_64` and `arm64` separately (`build-mac.yml`'s
matrix) and renames each with an `-<arch>` suffix; there is no universal2
build.

```sh
sudo installer -pkg bombercat-<version>.pkg -target /
bombercat --version
```

**Unsigned.** Gatekeeper will refuse to open it on first run — right-click
→ Open, or `xattr -d com.apple.quarantine bombercat-<version>.pkg` before
installing. Signing needs an Apple Developer ID ($99/yr) plus notarization;
out of scope for now (see [`PACKAGING_PLAN.md` §4.3](PACKAGING_PLAN.md#43-riesgos--bloqueadores-conocidos)).

## `.exe` (Windows)

Prerequisites: `pip install pyinstaller pywin32`, and
[Inno Setup](https://jrsoftware.org/isinfo.php) (`choco install innosetup`)
if you want the installer, not just the raw `.exe`.

```bat
build_windows.bat [version]
```

Runs PyInstaller against `bombercat_windows.spec` (`--onedir`, with the
`pywin32` hidden-imports the Wireshark named-pipe support needs — see
`modules/core/pipes.py`), producing `dist\bombercat\bombercat.exe`. If
`ISCC.exe` is on `PATH` it also renders `@VERSION@` into
`packaging/windows/bombercat_installer.iss` and builds
`bombercat-<version>.exe`; otherwise it stops after the PyInstaller step and
tells you so.

```
bombercat-<version>.exe /VERYSILENT /SUPPRESSMSGBOXES
```

**Unsigned.** SmartScreen will warn on first run ("Windows protected your
PC" → More info → Run anyway). No native drivers involved: the board
enumerates as a standard CDC device and Windows 10/11's built-in `usbser`
picks it up without an `.inf`.

The installer's `AppId` (`{B2123304-531C-477C-B378-46EE726970B3}`) is fixed
forever — see the comment at the top of the `.iss`. Never regenerate it; a
new GUID makes Windows treat every future upgrade as an unrelated product.

## `pip install .` — from source

The venv-based path documented in the [README](../README.md#install) works
anywhere Python 3.12+ runs and needs none of the above:

```sh
python3 -m pip install .
bombercat --version
```

## Other `make` targets

```sh
make help       # list every target, with the resolved VERSION
make install    # pip install . into the active environment
make uninstall  # pip uninstall bombercat
make version    # print the current VERSION
make clean      # remove build/dist/pkg_root and every built artifact
```

## Known gaps and quirks

- **`modules` is a generic top-level package name** under `pip install .` (not
  under the `.deb`/`.pkg.tar.zst`, which nest everything under
  `.../bombercat/`). It can collide with another project's `site-packages`
  entry. Fixing it for real means renaming `modules/` — a large refactor
  every test imports through — left as a conscious tradeoff for now.
- **`proto` and `testserver` are dev-only commands.** They need
  `gen_proto.sh`/`testserver/`/`firmware/` from a source checkout and are
  hidden from `--help` in every packaged build. Set `BOMBERCAT_DEV=1` to force
  them on (only useful if you also have the checkout around).
- **`bombercat flash` needs network access** (it downloads the UF2 image from
  the firmware repo's releases) and a mounted `RPI-RP2` mass-storage volume.
  No packaging format changes either requirement.
- **`VERSION` has 4 components** (e.g. `1.2.0.0`) while git tags use semver
  (`v1.2.0`) — both `dpkg`/`makepkg`/Inno Setup accept the 4-component form,
  just don't conflate it with the tag name. See
  [`docs/release.md`](release.md) for how the two stay in sync through a
  release.
