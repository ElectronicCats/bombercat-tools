# Packaging — progress log

Bitácora del trabajo de empaquetado multiplataforma (`.deb`, `.pkg.tar.zst`,
`.pkg`, `.exe`). El plan completo está en
[`PACKAGING_PLAN.md`](PACKAGING_PLAN.md); **este archivo es el punto de
entrada para retomar el trabajo**: qué está hecho, qué falta y por qué se
decidió lo que se decidió.

**Estado global: Fases 0, 1, 2, 3 y 5 completas. Fase 4 parcial (deb/arch
automatizados en CI; macOS/Windows siguen pendientes de validar de verdad).**
Siguiente: correr `build-mac.yml`/`build-windows.yml` vía `workflow_dispatch`
sobre una rama de prueba (necesita GitHub, no se puede hacer en este entorno),
y un release de prueba (`v1.2.0.1`) end-to-end para cerrar la Fase 4/5 del
todo.

| Fase | Estado | Notas |
|---|---|---|
| 0. Análisis de CatSniffer-Tools | ✅ Completa | 2026-09-10 |
| 1. Configuración base | ✅ Completa | 2026-09-10 · F1.1–F1.7 |
| 2. Workflows CI | ✅ Completa | 2026-09-10 · los 4 `build-*.yml` ya invocan los scripts de la Fase 3 |
| 3. Scripts locales | ✅ Completa | 2026-09-10 · `.deb`/`.pkg.tar.zst` validados en Docker; `.pkg`/`.exe` sin validar (sin macOS/Windows aquí) |
| 4. Testing y validación | 🔜 Parcial | Debian/Arch: checks estáticos + `verify-install` en CI, validados en Docker. macOS/Windows: sin validar, requiere `workflow_dispatch` en GitHub |
| 5. Documentación y release | ✅ Completa | 2026-09-10 · `docs/packaging.md`, `docs/release.md`, sección Install del README. Falta solo el release de prueba real (depende de la Fase 4 de mac/Windows) |

---

## Sesión 2026-09-10 — análisis

### Completado

- Clonado y leído `ElectronicCats/CatSniffer-Tools` completo: los 4 workflows
  de build (`build-deb`, `build-arch`, `build-mac`, `build-windows`), más
  `tests.yml` y `.pre-commit.yml`.
- Leídos `catnip/packaging/` (control, postinst, lanzador `usr/bin/catnip`,
  `.desktop`, reglas udev, postinstall de macOS), `build_mac.sh`,
  `build_windows.bat`, `Makefile`, `compile.sh`, `scripts/install.sh`,
  `setup.py`, los tres `.spec` de PyInstaller,
  `installer/catsniffer_installer.iss` y `install_windows_drivers.ps1`.
- Documentado el sistema completo en `PACKAGING_PLAN.md` §1, incluida la deuda
  técnica que **no** se debe replicar (§1.8).
- Auditado bombercat-tools contra ese modelo: dependencias, imports, rutas
  dependientes del checkout, datos empaquetables, USB IDs.
- Escrito el plan de 5 fases con rutas, snippets y comandos concretos.

### Hallazgos del análisis del repo propio

1. **`python-magic` / `python-magic-bin` no se usan.** Cero `import magic` en
   todo el repo. Quitarlos deja el proyecto **sin ninguna dependencia nativa**
   (click/rich/pyserial/Pygments son Python puro) y simplifica los tres
   instaladores. Es el hallazgo con más impacto sobre el plan.
2. **`pywin32` sí se usa** — `modules/core/pipes.py:39`, import perezoso para
   los named pipes de Wireshark en Windows. Se queda, y necesita
   `hiddenimports` en el spec de Windows.
3. **`modules/utils/_version.py` rompe bajo PyInstaller**: hace
   `Path(__file__).parents[2] / "VERSION"`, ruta que no existe en un bundle
   congelado. Hay que darle cascada de fallbacks (catnip lo esquiva
   reescribiendo el archivo desde CI, que es un parche, no una solución).
4. **`modules/core/firmwares.py:22` usa un import absoluto**
   (`from modules.firmware.releases import …`), que obliga a tener la raíz del
   proyecto en `sys.path`. Funciona con el truco del lanzador de catnip, pero
   pasarlo a relativo (`from ..firmware.releases import …`) es una línea y
   hace el paquete correcto por construcción.
5. **`proto` y `testserver` son comandos de desarrollo**: resuelven
   `parents[2]` esperando `gen_proto.sh`, `testserver/` y `firmware/` del
   checkout. En un paquete instalado no existen → hay que ocultarlos.
6. **Dato a empaquetar**: `modules/tags/mifare/data/mifare_default_keys.keys`
   (único no-`.py` bajo `modules/`). Debe ir en `package_data` y en los
   `datas` del spec.
7. **USB IDs propios**: `1209:005e` y variantes `805e`/`015e`/`025e`,
   `2341:005e`, y BOOTSEL `2e8a:0003` para el flasheo por mass-storage.
8. **El repo NO es un monorepo**: la herramienta está en la raíz, no en un
   subdirectorio tipo `catnip/`. Todas las rutas de los workflows de catnip
   pierden ese prefijo.

### Decisiones técnicas tomadas

| Decisión | Justificación |
|---|---|
| Vendorizar deps en `.deb`/`.pkg.tar.zst` (patrón catnip) | El paquete solo depende de `python3`; sin venv del usuario ni pelea con PEP 668 |
| PyInstaller `--onedir` en macOS/Windows | Mismo patrón que catnip; `--onefile` arranca lento y complica el `.pkg` |
| `packaging/build_deb.sh` y `build_arch.sh` como scripts, no como YAML | Corrige la deuda de catnip: la receta se puede reproducir en local |
| `VERSION` como fuente única + sustitución de `@VERSION@` en `control` y `.iss` | catnip mantiene la versión en 3 sitios a mano y se desincroniza |
| No replicar Nuitka (`compile.sh`) ni `scripts/install.sh` | Cuarto camino de instalación sin usuarios; `make install` + `pip install .` cubre el caso |
| `softprops/action-gh-release@**v2**` | catnip usa v1, sobre runtime de Node obsoleto |
| Triggers con filtro de ramas + `paths` | `build-deb.yml` de catnip dispara en cada push de cualquier rama |
| Sin drivers `.inf` ni Zadig en Windows | La placa enumera como CDC estándar; `usbser` de Win10/11 la reconoce sin driver |
| Sin libusb ni OpenOCD en ningún instalador | No se usa pyusb y el flasheo es UF2 por mass-storage |
| `setup.py` en vez de `pyproject.toml` | Paridad con catnip para el mismo equipo; decisión barata de revertir |
| Quitar `python-magic`, mover `pytest*` a `requirements-dev.txt` | Ver hallazgo 1; evita que pytest acabe vendorizado dentro del `.deb` |

### Bloqueadores identificados

- **Sin firma de código.** `.pkg` y `.exe` saldrán sin firmar (igual que
  catnip): Gatekeeper y SmartScreen avisarán. Requiere Apple Developer ID
  ($99/año) + notarización y un certificado Authenticode. Decisión de negocio,
  fuera del alcance técnico. → documentar el workaround.
- **`macos-15-intel`** es un runner con retirada anunciada; cuando desaparezca
  se pierde el build x86_64 salvo que se pase a universal2.
- **Sin hardware ni macOS/Windows en el entorno de desarrollo actual**: la
  validación de Fase 4 para `.pkg` y `.exe` será por artefactos de CI + prueba
  manual de alguien con esas máquinas.
- **`AppId` de Inno Setup**: hay que generar un GUID nuevo y **fijarlo para
  siempre** (reutilizar el de catnip rompería la desinstalación de ambos).

### Próximos pasos recomendados (en orden)

1. **F1.6 — cambios de código primero** (son prerrequisito y son testables sin
   CI): arreglar `_version.py`, pasar el import de `firmwares.py:22` a
   relativo, ocultar `proto`/`testserver` fuera del checkout, limpiar
   `requirements.txt`. Con `pytest` en verde después de cada uno.
2. **F1.1–F1.5** — crear `packaging/` con la plantilla de `control`, el
   lanzador, `postinst`, `.desktop` y las reglas udev; generar el GUID del
   `.iss`.
3. **F3 parcial** — `packaging/build_deb.sh` + `make deb`, y probar el `.deb`
   en `docker run debian:12` (Fase 4 para Debian) **antes** de escribir ningún
   workflow. Es el ciclo de iteración más rápido y valida el patrón entero.
4. **F2** — `build-deb.yml` primero (ya solo invoca el script), luego
   `build-arch.yml` con el mismo enfoque.
5. **F1.7 + specs + `build_mac.sh` / `build_windows.bat`**, y sus dos
   workflows. Es la parte que no se puede validar en local aquí: apoyarse en
   los artefactos de `workflow_dispatch` sobre una rama de prueba.
6. **F5** — `docs/packaging.md`, `docs/release.md` y la sección Install del
   README; hacer un release de prueba (`v1.2.0.1`) end-to-end antes de anunciar.

### Notas para la próxima sesión

- El clon de CatSniffer-Tools estaba en el scratchpad de la sesión (efímero):
  si hace falta volver a mirarlo,
  `git clone --depth 1 https://github.com/ElectronicCats/CatSniffer-Tools.git`.
- La versión de catnip analizada es `3.3.2.1`; la de bombercat-tools, `1.2.0.0`
  (tags publicados: `v1.0.0`, `v1.2.0`).
- Recordar el entorno: activar el venv existente con
  `source ~/BomberCat/bin/activate`; **no** crear venvs nuevos en el repo.

---

## Sesión 2026-09-10 (2) — Fase 1: configuración base

Suite de tests: **815 → 818 pasando** antes y después (3 tests nuevos para el
gate de comandos dev). `pre-commit run --all-files` limpio.

### F1.6 — cambios de código (hechos primero, son el prerrequisito)

| # | Cambio | Archivo |
|---|---|---|
| 1 | Versión con cascada de fallbacks: `sys._MEIPASS/VERSION` → `parents[2]/VERSION` → `importlib.metadata("bombercat")` → `"unknown"` | `modules/utils/_version.py` |
| 2 | Import absoluto → relativo (`from ..firmware.releases import …`) | `modules/core/firmwares.py:22` |
| 3 | `proto` y `testserver` se registran solo si `_dev_checkout()`: existe `gen_proto.sh` dos niveles arriba, o `BOMBERCAT_DEV=1` | `modules/core/cli.py` |
| 4 | `python-magic` / `python-magic-bin` fuera; `pytest*` movidos a `requirements-dev.txt` | `requirements.txt`, `requirements-dev.txt` |

**Efecto colateral obligado en CI**: `tests.yml` instalaba `requirements.txt` y
ejecutaba `pytest`. Al sacar pytest de ahí, el workflow pasa a instalar
`requirements-dev.txt`; además se le quitó `libmagic1` de las dependencias de
sistema (§4.1 del plan) y se añadió `requirements-dev.txt` a los `paths` y a la
clave de caché de pip.

Verificado sobre el **paquete instalado**, no solo en el checkout: wheel
construido con `pip wheel --no-deps`, instalado en un venv desechable del
scratchpad, y comprobado que

- `__version__` sale `1.2.0.0` vía `importlib.metadata` (no hay `VERSION` al
  lado de `_version.py` en `site-packages`),
- `bombercat --help` **no** lista `proto` ni `testserver`, y `BOMBERCAT_DEV=1`
  los devuelve,
- `bombercat tags mifare keys` lee las default keys empaquetadas,
- todo importa sin la raíz del repo en `sys.path` (cambio 2 validado).

### F1.1–F1.5 — árbol `packaging/`

```
packaging/
  debian/
    DEBIAN/control                          # plantilla, Version: @VERSION@
    DEBIAN/postinst                         # 755, dialout+plugdev, udevadm, exit 0
    usr/bin/bombercat                       # 755, lanzador (vendor/ + pkg_dir en sys.path)
    usr/share/applications/bombercat.desktop
    lib/udev/rules.d/99-bombercat.rules
  windows/
    bombercat_installer.iss                 # AppVersion=@VERSION@
```

- `postinst` sin `bluetooth` (no hay VHCI en BomberCat); crea `dialout` y
  `plugdev` si faltan, recarga udev y **nunca** falla la instalación.
- `.desktop` validado con `desktop-file-validate` (limpio, sin avisos:
  `Categories=Development;` — una sola categoría principal).
- Reglas udev: las cuatro personalidades CDC de `1209`, la identidad Arduino
  `2341:005e` y el BOOTSEL `2e8a:0003` para `bombercat flash`.

### AppId de Inno Setup — generado, **no cambiar nunca**

```
{B2123304-531C-477C-B378-46EE726970B3}
```

Va literal en `packaging/windows/bombercat_installer.iss` como
`AppId={{B2123304-…}` (el `{{` es el escape de Inno Setup para una llave
literal). Cambiarlo haría que Windows tratase una actualización como un
producto nuevo e independiente. El `.iss` está escrito entero (tareas
`desktopicon`/`addtopath`, append al `Path` de HKLM con guardia
`NeedsAddPath()`, `LicenseFile`, español + inglés) y espera el árbol
`dist\bombercat\` que producirá el spec de PyInstaller en la Fase 3.

### F1.7 — `setup.py`

`name=bombercat`, versión leída de `VERSION`, `install_requires` parseado de
`requirements.txt` (una sola lista: lo que se vendoriza en el `.deb` y lo que
instala `pip install .` son lo mismo), `package_data` con
`modules/tags/mifare/data/*.keys`, `py_modules=["bombercat"]`,
`console_scripts: bombercat=bombercat:main_cli`, `python_requires=">=3.12"`.

Wheel verificado: entra `bombercat.py`, `modules/**` y el `.keys`; **no** entran
`tests/`, el `testserver/` de la raíz, `__pycache__` ni los `MK1Keys*`.

### Deuda abierta que la Fase 1 destapa

1. **No existe `bombercat --version`.** El plan lo usa como smoke test
   obligatorio de los cuatro artefactos (§Fase 4) y de `docs/release.md`. Hoy
   la versión solo aparece en el banner ASCII. Hay que añadir la opción al
   grupo raíz antes de la Fase 4, o cambiar el smoke test a `--help`.
2. **`modules` es un nombre de primer nivel muy genérico.** El `.deb` no tiene
   el problema (todo cuelga de `dist-packages/bombercat/`), pero
   `pip install .` sí deja un paquete `modules` en `site-packages`, donde puede
   chocar con otro proyecto. Arreglarlo de verdad es renombrar
   `modules/` → `bombercat_tools/` (o meterlo bajo un paquete `bombercat/`):
   refactor grande, todos los tests importan `modules.…`. Decisión consciente
   de dejarlo como está; documentarlo en `docs/packaging.md`.
3. **Falta verificar que `tests.yml` sigue verde sin `libmagic1`** en CI real
   (en local está verde, y no hay ni un `import magic` en el repo).

### Próximos pasos

1. **`packaging/build_deb.sh` + `make deb`** y probar el `.deb` en
   `docker run debian:12` (Fase 4 para Debian) **antes** de escribir workflows.
   Es el ciclo de iteración más rápido y valida el patrón de vendorizado entero.
2. `packaging/build_arch.sh` y su prueba en `docker run archlinux`.
3. `build-deb.yml` y `build-arch.yml` (ya solo invocan los scripts).
4. Specs de PyInstaller + `build_mac.sh` / `build_windows.bat` y sus workflows;
   validación por artefactos de `workflow_dispatch` sobre una rama de prueba.
5. Fase 5: `docs/packaging.md`, `docs/release.md`, sección Install del README.

---

## Sesión 2026-09-10 (3) — Fase 3: scripts locales

Al retomar la sesión, los 4 workflows de la Fase 2 (`build-deb.yml`,
`build-arch.yml`, `build-mac.yml`, `build-windows.yml`) ya estaban commiteados
(`8a04461`, `90c55d9`) e invocan scripts que todavía no existían — esta
bitácora no se había actualizado tras esa sesión. Se escribieron los scripts
que faltaban y se validaron dos de los cuatro artefactos de punta a punta.

### Archivos creados

| Archivo | Rol |
|---|---|
| `packaging/build_deb.sh` | Receta completa del `.deb` (monta el árbol, vendoriza, `dpkg-deb --build`) |
| `packaging/build_arch.sh` | Equivalente para `makepkg`: detecta la versión de Python, genera el `PKGBUILD`, crea el usuario `builder` |
| `build_mac.sh` | PyInstaller `--onedir` + `pkgbuild`, sin `.spec` (flags directos, como pide el plan) |
| `bombercat_windows.spec` | Faltaba — lo referencia `build-windows.yml` desde que se escribió, y no existía. `--onedir`, `hiddenimports` de pywin32 |
| `build_windows.bat` | Espejo local del workflow; construye el `.exe` con Inno Setup solo si `ISCC.exe` está en el PATH |
| `Makefile` | Targets `help`/`deb`/`arch`/`mac`/`install`/`uninstall`/`version`/`clean` |

### Bug encontrado y arreglado: `preflight.py` no compilaba en Python 3.11

Al validar el `.deb` en `docker run debian:12` (que trae Python 3.11, no 3.12),
`bombercat --help` reventaba con
`SyntaxError: f-string expression part cannot include a backslash` en
`modules/testserver/preflight.py:246`. Es sintaxis de f-strings de PEP 701
(Python 3.12, PEP 701) usada sin querer — `grep`/`compileall` bajo
`python:3.11-slim` confirmó que era la **única** ocurrencia en todo el repo.
Arreglado extrayendo el string a una variable (`docker_group_cmd`) antes del
f-string, sin cambiar el mensaje mostrado al usuario. `pytest` sigue en verde
(818 pasando) y `compileall` bajo 3.11 ya no falla.

Esto importa porque Debian 12 (bookworm, estable) trae Python 3.11, y es
justo la distro que el plan usa en la tabla de la Fase 4 — sin este fix el
`.deb` se instalaba pero el binario no arrancaba en ningún Debian estable
actual. `setup.py` sigue declarando `python_requires=">=3.12"` (afecta solo a
`pip install .`, no al `.deb`, que vendoriza); no se tocó a propósito, es una
decisión de política aparte.

### Validado en Docker (extremo a extremo)

- **`.deb`**: `dpkg-deb -I` / `-c` limpios (sin `tests/`, `.venv*`,
  `__pycache__`, `MK1Keys*`); instalado con `apt install` en `debian:12`;
  `bombercat --help`, `bombercat flash --help` y
  `bombercat tags mifare keys` (default keys empaquetadas) funcionan;
  `proto`/`testserver` ocultos por defecto y visibles con `BOMBERCAT_DEV=1`.
- **`.pkg.tar.zst`**: `packaging/build_arch.sh` corrido dentro de
  `archlinux:latest` (mismas dependencias que instala `build-arch.yml`);
  `makepkg` como usuario `builder` sin privilegios; instalado con
  `pacman -U` en un contenedor limpio; mismos smokes en verde.
- **`.pkg` (macOS) y `.exe` (Windows)**: **sin validar** — no hay macOS ni
  Windows en este entorno. `build_mac.sh` y `build_windows.bat` pasan
  `bash -n` / se revisaron a mano contra sus workflows, pero necesitan
  correrse de verdad (o vía `workflow_dispatch` en una rama de prueba) antes
  de darlos por buenos.

### Próximos pasos

1. **Fase 4** — correr `build-mac.yml` y `build-windows.yml` vía
   `workflow_dispatch` sobre una rama de prueba y revisar los artefactos
   (no hay forma de validarlos en local aquí).
2. Añadir el job `verify-install` (Fase 4) a `build-deb.yml`/`build-arch.yml`
   que consuma el artefacto del build con `actions/download-artifact@v4` y
   repita los smokes de esta sesión dentro del propio workflow.
3. **Fase 5** — `docs/packaging.md`, `docs/release.md`, sección Install del
   README, y un release de prueba (`v1.2.0.1`) end-to-end.

---

## Sesión 2026-09-10 (4) — Fase 4: testing y validación (deb/arch)

Alcance de esta sesión: automatizar en CI lo que la sesión anterior había
validado a mano en Docker, y cerrar el hueco que esa sesión había marcado como
deuda ("No existe `bombercat --version`"). macOS/Windows quedan fuera —
requieren disparar workflows en GitHub, que no se puede hacer desde este
entorno sin confirmación explícita del usuario para gastar minutos de CI.

### `bombercat --version`

`@click.version_option(version=VERSION_NUMBER, prog_name="bombercat")` en el
grupo raíz (`modules/core/cli.py`). Imprime el header ASCII (como `--help`,
vía `main_cli()`) y luego `bombercat, version 1.2.0.0`. Test nuevo en
`tests/test_cli_root.py::test_version_flag_reports_the_package_version`.
819 tests en verde (818 → 819).

### `verify-install` en CI — `build-deb.yml` / `build-arch.yml`

Cada workflow gana:

- **`Static checks`** en el job `build`: `dpkg-deb -I`/`-c` (deb) o
  `bsdtar -tf` (arch) sobre el artefacto recién construido, con `grep` que
  falla el job si aparece `tests/`, `.venv`, `__pycache__/`, `MK1Keys` o
  `vendor/pytest` (el checklist de la Fase 4 del plan, antes solo verificado a
  mano).
- **`Lintian (informational)`** (solo deb): `continue-on-error: true`, no
  bloquea el build — es el mismo criterio que el plan describe para lintian.
- **Job nuevo `verify-install`**, `needs: build`, corre dentro de
  `container: debian:12` / `container: archlinux:latest`: descarga el
  artefacto con `actions/download-artifact@v4`, lo instala
  (`apt-get install -y ./*.deb` / `pacman -U --noconfirm`) y repite los smokes
  obligatorios del plan (`bombercat --version`, `bombercat --help`,
  `bombercat flash --help`), más una comprobación de que `proto`/`testserver`
  no aparecen en el `--help` del paquete instalado.

### Validado en Docker (réplica exacta de los pasos del CI, no solo revisión del YAML)

- **`.deb`**: reconstruido con `packaging/build_deb.sh 1.2.0.0`; los mismos
  `grep` del step `Static checks` en verde; instalado con
  `apt-get install -y ./bombercat-*.deb` en `debian:12` limpio;
  `bombercat --version`/`--help`/`flash --help` funcionan; `proto`/`testserver`
  siguen ocultos.
- **`.pkg.tar.zst`**: reconstruido dentro de `archlinux:latest` (mismas
  dependencias que instala el workflow); mismos `grep` en verde; instalado con
  `pacman -U --noconfirm` en un contenedor Arch limpio; mismos smokes en
  verde. (La versión de Python del container pasó a 3.14 — Arch es rolling —
  y el build siguió funcionando sin tocar nada.)
- Build artifacts locales (`bombercat-*.deb`, `bombercat-*.pkg.tar.zst`,
  `packaging/build_deb/`, `packaging/build_arch/`) limpiados tras validar;
  no quedan en el working tree.

### Sigue pendiente

- **macOS (`.pkg`) y Windows (`.exe`)**: sin validar de verdad. Requiere
  disparar `build-mac.yml`/`build-windows.yml` con `workflow_dispatch` sobre
  una rama de prueba en GitHub — usa minutos de CI y necesita push a remoto,
  así que se dejó fuera de esta sesión a la espera de que el usuario lo pida
  explícitamente.
- Un release de prueba (`v1.2.0.1`) end-to-end, siguiendo `docs/release.md`,
  para verificar los 5 assets de verdad (bloqueado por el punto anterior).

---

## Sesión 2026-09-10 (5) — Fase 5: documentación

### Archivos nuevos

| Archivo | Contenido |
|---|---|
| `docs/packaging.md` | Cómo construir cada uno de los 4 formatos en local: prerrequisitos por OS, el comando/`make target` exacto, dónde queda la salida, cómo instalarlo para probarlo a mano. Incluye la sección "Known gaps and quirks" (nombre genérico de `modules`, `proto`/`testserver` ocultos, `flash` necesita red, `VERSION` de 4 componentes vs. tag semver). |
| `docs/release.md` | El procedimiento de release del plan (§Fase 5), como pasos ejecutables: `pre-commit`+`pytest` en verde → bump de `VERSION` → commit `chore(release):` + tag + push → `gh release create --generate-notes` → verificar los 5 assets (el `verify-install` de la Fase 4 ya gatea deb/arch) → `gh workflow run <build>.yml -f release_tag=…` para re-subir un asset suelto. Cierra con el checklist de mantenimiento: pins de `actions/*`, `softprops/action-gh-release` en **v2**, el riesgo de `macos-15-intel`, y que el `AppId` del `.iss` no se toca nunca. |

### README

Sección **Install** reescrita: tabla de las 5 descargas (deb, arch,
macOS×2, exe) con su comando de instalación, antes del flujo de venv que
ahora es "From source". Se linkearon `docs/packaging.md` y `docs/release.md`
desde la tabla de Documentation.

### Nota

No se hizo el release de prueba real (`gh release create`) — es una acción
visible en GitHub (crea un release público, dispara los 4 workflows, gasta
minutos de CI) y no estaba pedida explícitamente; queda como el último paso
antes de cerrar la Fase 4 del todo, una vez se valide `.pkg`/`.exe`.
