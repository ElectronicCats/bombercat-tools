# Packaging plan — multiplataforma para `bombercat`

Replica en **bombercat-tools** el sistema de compilación y empaquetado de
[`CatSniffer-Tools`](https://github.com/ElectronicCats/CatSniffer-Tools)
(`catnip`): `.deb`, `.pkg.tar.zst`, `.pkg` (macOS Intel + arm64) y `.exe`
(Inno Setup), construidos por GitHub Actions y adjuntados al release.

- [1. Análisis de CatSniffer-Tools](#1-análisis-de-catsniffer-tools)
- [2. Diferencias BomberCat ↔ CatSniffer](#2-diferencias-bombercat--catsniffer)
- [3. Plan de implementación](#3-plan-de-implementación)
- [4. Decisiones y riesgos](#4-decisiones-y-riesgos)

---

## 1. Análisis de CatSniffer-Tools

### 1.1 Mapa del sistema

`catnip/` es un subdirectorio del monorepo; **todo** el empaquetado cuelga de ahí.

| Formato | Quién lo construye | Herramienta | Salida |
|---|---|---|---|
| `.deb` | `.github/workflows/build-deb.yml` | `dpkg-deb --build --root-owner-group` | `catnip-<VERSION>.deb` |
| `.pkg.tar.zst` | `.github/workflows/build-arch.yml` | `makepkg` (contenedor `archlinux:latest`) | `catnip-<VERSION>.pkg.tar.zst` |
| `.pkg` (macOS) | `.github/workflows/build-mac.yml` → `catnip/build_mac.sh` | PyInstaller + `pkgbuild` | `catnip-<VERSION>-{x86_64,arm64}.pkg` |
| `.exe` (Windows) | `.github/workflows/build-windows.yml` | PyInstaller (`catnip_windows.spec`) + Inno Setup `ISCC.exe` | `catnip-<VERSION>.exe` |
| wheel / `pip install .` | `catnip/setup.py` + `catnip/Makefile` | setuptools | `console_scripts: catnip=catnip:main_cli` |
| binario Nuitka | `catnip/compile.sh` (`make compile-install`) | Nuitka `--standalone` | `dist/catnip.dist/catnip` |

### 1.2 Dos estrategias de empaquetado, no una

Es la observación central del análisis:

- **Linux (deb/arch): no se congela nada.** Se copia el código fuente a
  `usr/lib/python3/dist-packages/catnip/` (Debian) o
  `usr/lib/python<X.Y>/site-packages/catnip/` (Arch), se **vendorizan** las
  dependencias con `pip install --target <pkg>/vendor -r requirements.txt`, y
  un lanzador `/usr/bin/catnip` (script Python) mete `vendor/` y el propio
  `pkg_dir` en `sys.path` antes de importar. Resultado: el paquete solo depende
  de `python3` del sistema y funciona sin venv del usuario.
- **macOS/Windows: PyInstaller `--onedir`.** Se congela un árbol
  `dist/catnip/` y encima se pone el instalador nativo (`pkgbuild` /
  Inno Setup).

### 1.3 Debian — `catnip/packaging/debian/`

Árbol que se copia tal cual a `build_deb/`:

```
DEBIAN/control            Package/Version/Depends: python3, libusb-1.0-0, openocd
DEBIAN/postinst           crea grupos dialout/bluetooth + udevadm control --reload-rules
usr/bin/catnip            lanzador Python (sys.path: vendor/ + pkg_dir)
usr/share/applications/catnip.desktop
lib/udev/rules.d/99-catsniffer.rules   VID/PID 2e8a:00c0 → GROUP dialout, TAG+="uaccess"
```

Pasos del workflow: extraer `VERSION` → copiar metadata → copiar `modules/`,
`catnip.py`, `VERSION`, `touch __init__.py` → `pip install --target …/vendor`
→ copiar `protocol/` como paquete hermano → `chmod 644` en `DEBIAN/*` y `755`
en `postinst`/`usr/bin/catnip` → `dpkg-deb --build --root-owner-group`.

### 1.4 Arch — PKGBUILD generado en el propio workflow

Corre dentro de `container: archlinux:latest`. Instala dependencias con
`pacman -S` (incluye los `python-*` del repo oficial), replica el mismo árbol
que Debian pero bajo `site-packages` con la versión de Python **detectada en
tiempo de build** (`python -c 'import sys; …'`), y genera un `PKGBUILD` mínimo
por heredoc cuyo `package()` es un `cp -r "$srcdir"/../usr "$pkgdir/"`.
`options=('!debug')` evita el segundo `.pkg.tar.zst` de depuración. `makepkg`
no corre como root: crea el usuario `builder` y usa `sudo -u builder`.

### 1.5 macOS — `build_mac.sh`

Prerrequisitos nativos vía `brew install libusb libmagic` (los instala el
workflow, no el script). Dos binarios PyInstaller `--onedir --noupx`
(`catnip`, `lora_extcap`) con `--collect-all` de las librerías con datos
(scapy, textual, meshtastic, rich, matplotlib, cryptography, serial) y
`--hidden-import` de las que se importan dinámicamente (click, usb,
`usb.backend.libusb1`, magic). Después:

```
pkg_root/usr/local/opt/catnip/{catnip,lora_extcap}/   ← árboles --onedir
pkg_root/usr/local/bin/{catnip,lora_extcap}           ← symlinks
pkgbuild --root pkg_root --identifier com.electroniccats.catsniffer \
         --version "$VERSION" --install-location / \
         --scripts packaging/macos/scripts   catnip-$VERSION.pkg
```

El `postinstall` del `.pkg` instala **openocd** en la máquina del usuario vía
el Homebrew del usuario real (`stat -f '%Su' /dev/console`, prueba
`/opt/homebrew/bin/brew` y luego `/usr/local/bin/brew`). Nunca falla el
install: todo termina en `exit 0`.

El workflow usa matriz `macos-15-intel` (x86_64) + `macos-latest` (arm64) y
renombra el `.pkg` con sufijo de arquitectura; no hay universal2 ni firma /
notarización.

### 1.6 Windows — spec + Inno Setup

`catnip_windows.spec` es el archivo más complejo del repo: `collect_all` de las
librerías grandes, `hiddenimports` de pywin32 (`win32file`, `win32pipe`,
`win32event`, `pywintypes`…), copia explícita de `modules/` y `protocol/` como
`datas`, y **descubrimiento de binarios nativos** — busca `libusb-1.0.dll`
(descargado del release de libusb y descomprimido con 7-Zip) y empaqueta
`openocd_dist/bin/*.{exe,dll}` + sus `scripts/` (xPack OpenOCD descargado por
el workflow). Un solo `Analysis` produce **dos** `EXE` (`catnip`,
`lora_extcap`) y un `COLLECT` común.

`installer/catsniffer_installer.iss`: `AppId` GUID fijo, `PrivilegesRequired=admin`,
tareas opcionales `desktopicon` / `addtopath` / `installdrivers`, entrada de
`[Registry]` que hace append al `Path` de HKLM con guardia `NeedsAddPath()`, y
`[Run]` que ejecuta `install_windows_drivers.ps1 -Silent` (genera un `.inf`
usbser y lo instala con `pnputil`). Salida `dist/Catnip-Setup.exe`, renombrada
después a `catnip-<version>.exe`.

### 1.7 Patrones reutilizables (lo que hay que copiar)

1. **`VERSION` como fuente única**, leído con `tr -d '[:space:]'` y exportado a
   `$GITHUB_ENV` (`(Get-Content VERSION -Raw).Trim()` en PowerShell), con
   fallback `"latest"`.
2. **Inyección de versión en el código** antes de congelar:
   `echo "__version__ = \"$VERSION\"" > modules/utils/_version.py`. Necesario
   porque bajo PyInstaller no existe el `VERSION` relativo al `__file__`.
3. **Triggers uniformes**: `release: types: [created]` + `workflow_dispatch`
   con input `release_tag` (default `v3.3.2.1`) + `push` a ramas concretas.
4. **Subida condicional**:
   `if: github.event_name == 'release' || github.event_name == 'workflow_dispatch'`
   con
   `tag_name: ${{ github.event_name == 'workflow_dispatch' && inputs.release_tag || github.ref_name }}`.
5. **Doble salida**: siempre `actions/upload-artifact@v4` (retención 30 días)
   y, solo en release, `softprops/action-gh-release@v1`.
6. **Naming**: `<tool>-<VERSION>[-<arch>].<ext>`.
7. **Vendorizado** en Linux en vez de `Depends:` sobre paquetes Python del
   sistema (salvo Arch, que además instala los `python-*` nativos).

### 1.8 Deuda técnica observada (no replicar)

- La versión está **triplicada**: `VERSION`, `Version:` en `DEBIAN/control`
  (`3.3.2.1` a mano) y `AppVersion` en el `.iss` (a mano). Se desincroniza sola.
- La receta del `.deb` **solo existe dentro del YAML**: no se puede reproducir
  el build en local (el `Makefile` no tiene target `deb`).
- `build-deb.yml` dispara con `on: push` **sin filtro de ramas ni paths**.
- `build_windows.bat` está desincronizado del workflow (no descarga OpenOCD).
- `Makefile` y `compile.sh` (Nuitka) son un tercer camino que nadie usa en CI.
- `softprops/action-gh-release@v1` está sobre un runtime de Node obsoleto.

---

## 2. Diferencias BomberCat ↔ CatSniffer

| Aspecto | catnip | bombercat | Consecuencia |
|---|---|---|---|
| Layout | tool en `catnip/` del monorepo | **tool en la raíz del repo** | Se cae el prefijo `catnip/` en todas las rutas de los workflows |
| Entry point | `catnip.py` → `main_cli` | `bombercat.py` → `modules.core.cli:main_cli` | Igual de simple |
| Dependencias | scapy, numpy, matplotlib, meshtastic, textual, pyusb, cryptography… | **click, rich, pyserial, Pygments, markdown-it-py** | Spec de PyInstaller ~10× más simple; sin `--collect-all` masivo |
| Nativas | libusb, libmagic, openocd | **ninguna** (ver §4.1) | Sin `brew install`, sin descargar libusb/OpenOCD, sin drivers `.inf` |
| Flasheo | openocd / cc2538-bsl | **UF2 por mass-storage** (`RPI-RP2`) | No hay que empaquetar toolchain |
| USB IDs | `2e8a:00c0` | `1209:005e` (+`805e`,`015e`,`025e`), `2341:005e`, BOOTSEL `2e8a:0003` | Reglas udev propias |
| Datos empaquetados | — | `modules/tags/mifare/data/mifare_default_keys.keys` | Debe ir como `package_data` / `datas` |
| Windows | pywin32 usado a fondo | pywin32 **usado** (named pipes → Wireshark, `modules/core/pipes.py:39`) | Se mantiene el `hiddenimport` |
| Empaquetado actual | maduro | **inexistente** (solo `requirements.txt` + venv) | Todo por construir |

---

## 3. Plan de implementación

### Fase 1 — Configuración base

**F1.1 Estructura de directorios** (rutas relativas a la raíz del repo):

```
packaging/
  debian/
    DEBIAN/control                          # plantilla, Version: @VERSION@
    DEBIAN/postinst
    usr/bin/bombercat                       # lanzador Python
    usr/share/applications/bombercat.desktop
    lib/udev/rules.d/99-bombercat.rules
  windows/
    bombercat_installer.iss                 # AppVersion=@VERSION@
  build_deb.sh                              # receta .deb reutilizable (CI + local)
  build_arch.sh                             # receta PKGBUILD + makepkg
bombercat.spec                               # PyInstaller POSIX (macOS)
bombercat_windows.spec                       # PyInstaller Windows
build_mac.sh
build_windows.bat
setup.py                                     # o pyproject.toml (ver §4.2)
Makefile
```

**F1.2 Metadatos** (constantes del proyecto):

| Campo | Valor |
|---|---|
| Nombre de paquete / binario | `bombercat` |
| Versión | `VERSION` (hoy `1.2.0.0`) |
| Maintainer | `Electronic Cats <support@electroniccats.com>` |
| Homepage | `https://github.com/ElectronicCats/bombercat-tools` |
| Licencia | GPL-3.0 |
| Bundle id macOS | `com.electroniccats.bombercat` |
| `AppId` Inno Setup | GUID nuevo (generar una vez, **no** reutilizar el de catnip) |
| Depends (deb) | `python3` (todo lo demás va vendorizado) |
| Recommends (deb) | `udisks2` (automonta la unidad `RPI-RP2` de `flash`; `apt` lo instala solo salvo `--no-install-recommends`) |
| Suggests (deb) | `wireshark`, `docker.io` |
| optdepends (Arch) | `udisks2` (mismo motivo; pacman no instala `optdepends` solo, solo los anuncia) |

**F1.3 `DEBIAN/control`** (plantilla; `@VERSION@` lo sustituye el build):

```
Package: bombercat
Version: @VERSION@
Section: utils
Priority: optional
Architecture: all
Maintainer: Electronic Cats <support@electroniccats.com>
Depends: python3
Recommends: udisks2
Suggests: wireshark, docker.io
Homepage: https://github.com/ElectronicCats/bombercat-tools
Description: BomberCat CLI — NFC relay, tag/reader detection and magspoof
 Command line tools for the BomberCat board: NFCGate relay control, APDU
 capture to pcap, Mifare Classic operations, NFC tag and reader detection,
 magstripe emulation and UF2 firmware flashing.
```

**F1.4 `usr/bin/bombercat`** (lanzador, calcado del de catnip):

```python
#!/usr/bin/env python3
import os, sys, bombercat
pkg_dir = os.path.dirname(bombercat.__file__)
vendor = os.path.join(pkg_dir, "vendor")
if os.path.isdir(vendor):
    sys.path.insert(0, vendor)
if pkg_dir not in sys.path:
    sys.path.insert(0, pkg_dir)     # hace importable `modules.…`
from bombercat.modules.core.cli import main_cli
if __name__ == "__main__":
    main_cli()
```

**F1.5 `99-bombercat.rules`**:

```
# BomberCat CDC (pid.codes VID 1209) — todas las personalidades
SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="005e", MODE="0660", GROUP="dialout", TAG+="uaccess"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="805e", MODE="0660", GROUP="dialout", TAG+="uaccess"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="015e", MODE="0660", GROUP="dialout", TAG+="uaccess"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="025e", MODE="0660", GROUP="dialout", TAG+="uaccess"
# Identidad Arduino (core mbed_rp2040)
SUBSYSTEM=="tty", ATTRS{idVendor}=="2341", ATTRS{idProduct}=="005e", MODE="0660", GROUP="dialout", TAG+="uaccess"
# BOOTSEL (RP2040 mass-storage) — `bombercat flash`
SUBSYSTEM=="block", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="0003", MODE="0660", GROUP="plugdev", TAG+="uaccess"
```

`postinst`: mismo esqueleto que catnip pero sin `bluetooth` (no hay VHCI);
solo asegura `dialout`/`plugdev` y recarga udev.

**F1.6 Cambios en el código (prerrequisito real del empaquetado)**

Son cuatro y hay que hacerlos **antes** de tocar CI:

1. `modules/utils/_version.py` — hoy es
   `(Path(__file__).parents[2] / "VERSION").read_text()`, que **revienta** en
   un bundle PyInstaller. Reescribir con cascada:
   `sys._MEIPASS/VERSION` → `parents[2]/VERSION` → `importlib.metadata` →
   `"unknown"`, y mantener la inyección de `__version__` en CI como atajo.
2. `modules/core/firmwares.py:22` — `from modules.firmware.releases import …`
   es un import **absoluto** que exige tener la raíz en `sys.path`. Cambiar a
   `from ..firmware.releases import …` y el paquete funciona instalado en
   cualquier prefijo (el lanzador deja de ser un requisito de corrección).
3. `modules/proto/cli.py` y `modules/testserver/cli.py` resuelven
   `TOOLS_DIR = Path(__file__).resolve().parents[2]` y esperan `gen_proto.sh`,
   `testserver/` y `firmware/` del checkout. En un `.deb`/`.pkg` no existen.
   Registrar `proto` y `testserver` en `main_cli()` **solo si** el checkout
   está presente (p. ej. `if (TOOLS_DIR / "gen_proto.sh").exists()`), o tras
   `BOMBERCAT_DEV=1`.
4. `requirements.txt` — quitar `python-magic` / `python-magic-bin`
   (ver §4.1) y mover `pytest`/`pytest-cov` a `requirements-dev.txt`, para que
   el vendorizado del `.deb` no arrastre pytest.

**F1.7 `setup.py`** — nombre `bombercat`, versión leída de `VERSION`,
`packages=find_packages(include=["modules", "modules.*"])`,
`package_data={"modules.tags.mifare": ["data/*.keys"]}`,
`py_modules=["bombercat"]`,
`entry_points={"console_scripts": ["bombercat=bombercat:main_cli"]}`,
`python_requires=">=3.12"` (lo que prueba CI hoy).

---

### Fase 2 — Workflows

Cuatro archivos nuevos en `.github/workflows/`, sin prefijo `catnip/` en las
rutas. Trigger común (corrigiendo la deuda §1.8):

```yaml
on:
  push:
    branches: [main]
    paths: ["modules/**", "bombercat.py", "packaging/**", "*.spec",
            "requirements.txt", "VERSION", ".github/workflows/build-*.yml"]
  release:
    types: [created]
  workflow_dispatch:
    inputs:
      release_tag:
        description: 'Tag del release al que subir los assets (ej: v1.2.0)'
        required: true
        default: 'v1.2.0'
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
```

- **`build-deb.yml`** (`ubuntu-latest`): extraer VERSION → `bash packaging/build_deb.sh "$VERSION"`
  → `upload-artifact` → `action-gh-release` condicional. Toda la lógica vive en
  el script, no en el YAML.
- **`build-arch.yml`** (`ubuntu-latest` + `container: archlinux:latest`):
  `pacman -Syu --noconfirm` + `base-devel git python python-pip python-click
  python-rich python-pyserial python-pygments sudo` → `bash packaging/build_arch.sh "$VERSION"`
  (detecta `PY_VER`, genera PKGBUILD con `options=('!debug')`, crea usuario
  `builder`, `makepkg -f --noconfirm`) → mover el `.pkg.tar.zst` a la raíz.
- **`build-mac.yml`**: matriz `{macos-15-intel: x86_64, macos-latest: arm64}`,
  `setup-python@v5` 3.12, `pip install -r requirements.txt pyinstaller`,
  inyectar versión, `./build_mac.sh`, renombrar a `bombercat-<VERSION>-<arch>.pkg`,
  `file dist/bombercat/bombercat` como verificación de arquitectura.
  **Sin `brew install`** (§4.1).
- **`build-windows.yml`** (`windows-latest`, Python 3.12): `pip install -r
  requirements.txt pyinstaller pywin32` → inyectar versión → `pyinstaller
  bombercat_windows.spec` → `choco install innosetup -y` → sustituir
  `@VERSION@` en el `.iss` → `ISCC.exe packaging\windows\bombercat_installer.iss`
  → renombrar a `bombercat-<VERSION>.exe`. **Sin 7-Zip, sin libusb, sin OpenOCD,
  sin script de drivers** (la placa enumera como CDC estándar; `usbser` de
  Windows 10/11 la toma sin `.inf`).

`tests.yml` se queda como está; añadir `packaging/**` a sus `paths` **no** es
necesario.

---

### Fase 3 — Scripts locales

- **`packaging/build_deb.sh <version>`** — la receta completa (montar
  `build_deb/`, sustituir `@VERSION@` en `control`, copiar `modules/`,
  `bombercat.py`, `VERSION`, `touch __init__.py`, `pip install --target
  .../vendor -r requirements.txt`, permisos, `dpkg-deb --build --root-owner-group`).
  Reproducible en local; el workflow solo lo invoca.
- **`packaging/build_arch.sh <version>`** — equivalente para `makepkg`.
- **`build_mac.sh`** — PyInstaller `--onedir --noupx --name bombercat
  --collect-all rich --hidden-import click --hidden-import serial.tools.list_ports
  --add-data VERSION:. --add-data modules/tags/mifare/data:modules/tags/mifare/data`
  → `pkg_root/usr/local/opt/bombercat/` + symlink en `/usr/local/bin` →
  `pkgbuild --identifier com.electroniccats.bombercat`. **Sin `--scripts`**
  salvo que se decida instalar las reglas de permisos (macOS no las necesita).
- **`build_windows.bat`** — espejo exacto del workflow (dependencias →
  `pyinstaller bombercat_windows.spec` → verificar `dist\bombercat\bombercat.exe`),
  para no repetir la desincronización de catnip.
- **`Makefile`** — targets: `help`, `deb`, `arch`, `mac`, `install`
  (`pip install .`), `uninstall`, `version` (echo VERSION), `clean`.
  **No** replicar `compile.sh`/Nuitka: es un cuarto camino sin usuarios.

---

### Fase 4 — Testing y validación

| Artefacto | Verificación estática | Prueba de instalación |
|---|---|---|
| `.deb` | `dpkg-deb -I`, `dpkg-deb -c`, `lintian --no-tag-display-limit` (informativo) | `docker run debian:12` → `apt install ./bombercat-*.deb` → `bombercat --version`, `bombercat --help`, `bombercat flash --help` |
| `.pkg.tar.zst` | `bsdtar -tf` | `docker run archlinux` → `pacman -U --noconfirm` → mismos smokes |
| `.pkg` macOS | `pkgutil --expand`, `file dist/bombercat/bombercat` (arch correcta) | `sudo installer -pkg … -target /` → `bombercat --help` |
| `.exe` | — | `bombercat-*.exe /VERYSILENT /SUPPRESSMSGBOXES` → nueva shell → `bombercat --help` |

Smoke test obligatorio en los cuatro: `bombercat --help` y `bombercat --version`
(no tocan hardware ni red). Añadir un job `verify-install` que consuma el
artefacto del job de build vía `actions/download-artifact@v4` en deb y arch
(gratis en `ubuntu-latest`); macOS/Windows quedan como verificación manual
documentada en la Fase 5 hasta que haya presupuesto de minutos.

Checklist adicional: que el `.deb` **no** contenga `tests/`, `.venv*`,
`__pycache__` ni los `MK1Keys*.txt` del working tree, y que `vendor/` no traiga
pytest.

---

### Fase 5 — Documentación y mantenimiento

- **`docs/packaging.md`** — cómo construir cada formato en local
  (prerrequisitos por OS, comandos `make`, dónde queda la salida).
- **`docs/release.md`** — procedimiento:
  1. `pre-commit run --all-files` y `pytest` en verde.
  2. Bump de `VERSION` (única fuente; `control` y el `.iss` se rellenan solos).
  3. Commit `chore(release): vX.Y.Z`, tag `vX.Y.Z`, push del tag.
  4. Crear el GitHub Release (`gh release create vX.Y.Z --generate-notes`);
     los cuatro workflows disparan con `release: created` y adjuntan assets.
  5. Verificar los 5 assets (`deb`, `pkg.tar.zst`, 2× `pkg`, `exe`).
  6. Si un build falla, `workflow_dispatch` con `release_tag=vX.Y.Z` re-sube
     solo ese asset.
- **README** — sección Install con las cuatro descargas, dejando el venv como
  vía "desde fuentes".
- Mantenimiento: revisar `actions/*` y `softprops/action-gh-release` (usar
  **v2**, no la v1 de catnip) cada release; el `AppId` del `.iss` **nunca**
  cambia.

---

## 4. Decisiones y riesgos

### 4.1 `python-magic` no se usa — quitarlo

`grep -rn "^\s*(import|from)\s+magic"` sobre todo el repo: **cero coincidencias**
(los aciertos de "magic" son el magic number de UF2/pcap y las tarjetas "magic"
de Mifare). Está en `requirements.txt` por herencia de catnip. Quitarlo elimina
la única dependencia nativa del proyecto: sin `brew install libmagic`, sin
`python-magic-bin` en Windows, sin `libmagic1` en el `tests.yml`. Es lo que
convierte el empaquetado de BomberCat en un ejercicio mucho más simple que el
de catnip. **Verificar antes de borrar** que `tests.yml` sigue verde sin
`libmagic1`.

### 4.2 `setup.py` vs `pyproject.toml`

Se propone `setup.py` por **paridad con catnip** (mismo patrón de mantenimiento
para el mismo equipo). `pyproject.toml` sería lo moderno, pero leer `VERSION`
desde ahí exige `dynamic = ["version"]` + backend, y nada en el pipeline lo
necesita. Decisión reversible y de bajo coste.

### 4.3 Riesgos / bloqueadores conocidos

- **Firma de código**: ni el `.pkg` ni el `.exe` van firmados (igual que catnip).
  Gatekeeper pedirá botón derecho → Abrir; SmartScreen mostrará aviso. Firmar
  exige Apple Developer ID ($99/año) + notarización y un cert Authenticode.
  **Fuera de alcance**, documentar el workaround en el README.
- **`macos-15-intel`**: runner de retirada anunciada por GitHub. Si desaparece,
  la salida x86_64 se pierde; alternativa es un build universal2 en arm64.
- **Comandos dev en el paquete** (`proto`, `testserver`): sin la corrección
  F1.6-3, aparecen en `--help` del paquete instalado y fallan al invocarse.
- **`bombercat flash` necesita red** (descarga UF2 de los releases del firmware)
  y montaje del volumen `RPI-RP2`. Ninguna de las dos cosas la resuelve el
  empaquetado; mencionarlo en `docs/packaging.md`.
- **`VERSION` = `1.2.0.0`** (4 componentes). Debian y Arch lo aceptan; Inno
  Setup también. No hace falta normalizar a semver, pero sí **no** mezclar
  `1.2.0.0` con el tag `v1.2.0`.
