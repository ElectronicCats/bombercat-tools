# Plan de integración — EMVyBomberCat en `bombercat` CLI

> **Estado del documento:** vivo · multi-sesión · autocontenido
> **Firmware objetivo:** `bombercat-firmware/EMVyBomberCat/EMVyBomberCat.ino` (v1.1.0.1, sketch multi-archivo)
> **CLI destino:** `bombercat-tools/modules/` (patrón de referencia: `readers/`, `tags/`, `magspoof/`)
> **Cómo usar este doc:** lee primero el [Resumen ejecutivo](#1-resumen-ejecutivo) y la [Decisión arquitectónica](#4-decisión-arquitectónica). Para reanudar el trabajo, salta directo a la **última entrada** del [Progress Log](#8-progress-log): contiene el estado exacto y los próximos pasos. No hace falta releer el resto.

> Entorno virtual de prueba `source ~/Bombercat/bin/activate`
---

## Índice

1. [Resumen ejecutivo](#1-resumen-ejecutivo)
2. [Análisis técnico del firmware](#2-análisis-técnico-del-firmware)
3. [Patrón de integración en la CLI](#3-patrón-de-integración-en-la-cli)
4. [Decisión arquitectónica](#4-decisión-arquitectónica)
5. [Divergencias respecto al patrón CLI](#5-divergencias-respecto-al-patrón-cli)
6. [Fases de implementación](#6-fases-de-implementación)
7. [Cambios requeridos en el firmware](#7-cambios-requeridos-en-el-firmware)
8. [Progress Log](#8-progress-log)

---

## 1. Resumen ejecutivo

EMVyBomberCat ya está **parcialmente integrado**: aparece en el registro de firmwares
(`modules/core/firmwares.py`, id `emvybombercat`) y responde el *Discovery Contract*
(`ping`/`info`/`identify`), por lo que `bombercat status`, `bombercat device list` y
`bombercat identify` lo reconocen **hoy** con confianza *handshake*. Declara honestamente
solo las capacidades `monitor`, `identify` y `passthrough`.

Lo que **falta** es exponer su superficie **operativa** (lectura EMV, passthrough de APDU,
lectura de tag, magspoof, emulación NDEF y emulación de tarjeta EMV) como subcomandos
`bombercat emvy …`, siguiendo el patrón de `readers`/`tags`/`magspoof`.

**El obstáculo central** es que esa superficie operativa usa un **dialecto serial propio**
(`WAIT`/`APDU:`/`RESP:`/`SCAN`+`JSON_START/END`/`TAG:`/`EMU:…`) que **no** usa el framing
canónico `+OK` / `-ERR` / `:key value`. El cliente `DeviceLink.command()`
(`modules/core/bombercat.py`) solo entiende ese framing: trataría `RESP:…`, `READY:`,
`OK` o `EMU:RX …` como *ruido de log* y agotaría el timeout esperando un terminador
`+`/`-`. Por tanto, integrar los verbos operativos requiere **una de dos rutas**
(ver §4): un cliente serial adicional que hable el dialecto histórico (bajo riesgo,
firmware intacto), o migrar el firmware al framing canónico (mayor alcance, alineación
total con el patrón CLI).

**Recomendación:** ruta híbrida — entregar valor pronto con el dialecto tal cual
(Fases 1–4) y dejar la migración de framing como fase estratégica opcional (Fase 5),
que el propio header del firmware ya anticipa como el estado final deseado
(«…conservan el dialecto histórico por retrocompatibilidad … hasta que el host migre
al framing +OK/-ERR»).

---

## 2. Análisis técnico del firmware

> **Créditos:** el firmware EMVyBomberCat fue una contribución de
> [**Glitchboi-sudo**](https://github.com/Glitchboi-sudo) en el PR
> [ElectronicCats/bombercat-firmware#4](https://github.com/ElectronicCats/bombercat-firmware/pull/4).
> Este plan integra ese firmware en la CLI `bombercat`; no lo reescribe.

### 2.1 Arquitectura

- **Placa:** BomberCat (RP2040 mbed + NINA-W102 WiFi). NFC: **PN7150** (I2C `0x28`,
  IRQ 11, VEN 13) vía `NfcController` (único punto de acceso; primitivas crudas por
  `nfc.raw()`).
- **Dos superficies de control simultáneas sobre el mismo puerto serie @115200:**
  1. **Discovery Contract** — servido por `BomberCatControl control(Serial, "1.1.0.1", "emvybombercat")` (`.ino:81`). Es el REPL canónico `+OK`/`-ERR`.
  2. **Dialecto operativo** — hook `emvyCommand(verb, args)` (`.ino:3462`), registrado como `cb.command` (`.ino:2689`). Atiende los verbos que el REPL base no reconoce; responde con `Serial.println` de texto libre.
- **Acceso alterno:** WiFi AP `EMVyBomberCat`/`bombercat` + panel web en `http://192.168.4.1` (fuera del alcance de este plan; la CLI habla por USB serie).
- **Build:** sketch **multi-archivo** (`emv_emu.h`, depende de libs `core/src/*`:
  `BomberCatControl`, `EmvKernel`, `NfcController`, `HexUtils`, `MagStripe`, `TagReader`).
  Se compila con **arduino-cli**; **no** es una imagen prebuilt del release feed
  (implicación clave para auto-flash, §5).

### 2.2 Discovery Contract (ya consumido por la CLI)

| Verbo | Respuesta | Sirve |
|-------|-----------|-------|
| `ping` (case-insensitive) | `+OK bombercat` | `BomberCatControl` (§5) |
| `info` | `:fw_name emvybombercat` · `:fw 1.1.0.1` · `:state <idle\|scanning\|emulating\|hw-error>` · `+OK` | core (`§6.1`) |
| `identify` | `+OK` + parpadeo LED asíncrono | core (`§6.2`) |
| *(desconocido)* | `-ERR unknown command <verb>` | core (`§6.3.3`) |

Estado (`controlState()`, `.ino:2722`): `emulating` (gEmuActive) · `scanning`
(gScanningCard) · `hw-error` (gNfcFault/gWifiFault) · `idle`.

Esto **ya encaja** con `DeviceLink.ping()`/`.info()`/`.identify()` y con
`detect_firmware()` (identificación con certeza por `fw_name`).

### 2.3 Dialecto operativo (a integrar) — `emvyCommand()`

Fuente: `.ino:3462-3772`. Respuestas por `Serial.println`, **sin** framing `+OK/-ERR`.

| Verbo (entrada) | Respuesta / stream | Semántica | Bloqueante |
|-----------------|--------------------|-----------|:---:|
| `WAIT [ms]` | `READY:` \| `ERR:NOCARD` | Arma discovery y espera tarjeta ISO-DEP (def. 30 s). Deja `gPassthroughActive`. | Sí |
| `APDU:<hex>` | `RESP:<hex>` \| `ERR:NOCARD` \| `ERR:BADAPDU` \| `ERR:TXFAIL` | Passthrough de un APDU a la tarjeta enganchada (requiere `WAIT` previo). | Sí (corto) |
| `RELEASE` | `OK` | Suelta la tarjeta / detiene emulación. | Sí |
| `TAG` / `TAGS` | `TAG:<proto> UID:<hex>` (vía `emvyTagsRead`) | Lee UID de cualquier tag. | Sí |
| `MAG:<t1>\|<t2>` | `OK` | Emula swipe de banda (magspoof). | Sí |
| `SCAN [centavos]` | `# …` logs → `JSON_START` / `<json>` / `JSON_END` (o `# ERROR: …`) | Flujo EMV completo; JSON con PAN/expiry/track2/AID/ARQC/… (`buildWebJson`, `.ino:769`). Def. 500. | Sí (≤30 s) |
| `CARDSCAN` | `EMU:SCANNED aid=… pan=… exp=… t2=…` \| `ERR:NOCARD` \| `ERR:SCANFAIL` | Lee EMV y la guarda en RAM para reemular. | Sí (≤20 s) |
| `EMU:<hex>` | `EMU:START …` → stream `EMU:RX …`/`EMU:TX …`/`EMU:MSG-SENT …` → `EMU:DONE sent=…` | Emula tag NFC Forum Type 4 sirviendo `<hex>` como NDEF; observable APDU-a-APDU. **Asíncrono** (lo bombea `loop()`/`emuPump`). | No (arranca y vuelve) |
| `EMUEMV[:<aid>\|<pan>\|<exp>\|<track2>\|RAM]` | `EMU:START mode=emv` → stream `EMU:RX …` → `EMU:DONE` | Emula **tarjeta EMV** (perfila/fuzz terminal). Sin args = Visa de prueba; `:RAM` = la de `CARDSCAN`. | No |
| `STOP` | `OK` (o corte de emulación) | Detiene emulación NDEF/EMV en curso. | Sí |
| `NFCINFO` | `NFCINFO: fwver=<n> (chip vivo)` \| `NFCINFO: ERR connectNCI …` | Diagnóstico I2C del PN7150 + re-arma discovery. | Sí |
| `REBOOT` / `RESET` | `# REBOOT` y `NVIC_SystemReset()` | Reinicia el MCU (**re-enumera USB**; el host debe reconectar). | — |

**Notas de comportamiento clave para el cliente host:**
- `isTagDetected()` es de **flanco** (avisa una vez por notificación NCI). Por eso el
  firmware recuerda el enganche en `gPassthroughActive`: el flujo correcto es
  `WAIT` → uno o más `APDU:` → `RELEASE`. No se puede mandar `APDU:` sin `WAIT` previo.
- `EMU:`/`EMUEMV:` son **asíncronos y cancelables**: tras arrancar, el firmware sigue
  emitiendo líneas `EMU:*` hasta `STOP`/`RELEASE`/`REBOOT` o el auto-stop de seguridad
  (`EMU_MAX_MS` = 180 s). El cliente debe **stremear** hasta el sentinela `EMU:DONE`.
- `emvyCommand` reconstruye `verb`+`args` en una línea y hace `toUpperCase()` para el
  matching, pero conserva el original para payloads con espacios (p.ej. track1). Los
  verbos son case-insensitive.
- Verbo no reconocido → `emvyCommand` retorna `false` → `BomberCatControl` emite
  `-ERR unknown command <verb>` (útil: el host puede feature-detectar).

---

## 3. Patrón de integración en la CLI

Referencias canónicas leídas:

- **Registro:** `modules/core/firmwares.py` — `Firmware(id, display, uf2, has_repl,
  capabilities, banners, description)`. EMVyBomberCat **ya está** registrado
  (`.py:148-159`).
- **Capacidades → proveedor de auto-flash:** `modules/core/requirements.py`
  (`CAPABILITY_PROVIDER`). `monitor`/`identify`/`passthrough` **no** tienen proveedor
  a propósito (demasiado genéricas para justificar reflash).
- **Cliente de control:** `modules/core/bombercat.py` — `DeviceLink` (protocolo de
  líneas `+OK`/`-ERR`/`:key value`), `resolve_port()`, `discover_devices()`.
- **Sesión verificada estándar:** `modules/utils/detection_cli.py::device_session()`
  — resuelve puerto, opcional `requires=<CAP>` (auto-flash vía `ensure_firmware`),
  abre link, verifica `ping`, hace `yield (target, link)`, cierra siempre.
- **Módulos de comando de referencia:**
  - `modules/readers/cli.py` y `modules/tags/cli.py` — *observadores* (streaming de
    eventos estructurados), construidos con `build_detection_group(DetectionSpec(…))`.
  - `modules/magspoof/cli.py` — *actuador* (comandos que **manejan** la placa:
    `play`/`card`), usa `device_session(..., requires=CAP_MAGSPOOF)` + `DeviceLink.command()`.
- **Opciones compartidas:** `modules/utils/cli_options.py` (`target_options`,
  `device_options`).
- **Salida:** `modules/utils/output.py` (`console`, `print_success/error/info/dim`,
  `make_tracer`).
- **Registro del grupo:** `modules/core/cli.py::main_cli()` → `cli.add_command(_emvy)`
  (junto a `_tags`, `_readers`, `_magspoof`, `.py:586-595`).
- **Convención de docs:** `docs/commands/<grupo>.md` + fila en `docs/reference.md`.
- **Convención de tests:** `tests/test_cli_<grupo>.py` con fake serial (sin hardware).

El molde `magspoof` es el más cercano a EMVyBomberCat (actuador que maneja la placa),
**salvo** que magspoof habla el framing canónico y EMVyBomberCat no (§4).

---

## 4. Decisión arquitectónica

### El problema

`DeviceLink.command(line)` (bombercat.py:100) lee líneas hasta un terminador
`+OK`/`-ERR`, acumulando `:key value`. Cualquier otra línea es **ruido ignorado**.
Las respuestas operativas de EMVyBomberCat (`RESP:`, `READY:`, `OK`, `JSON_START`,
`TAG:`, `EMU:RX`) **no** empiezan por `+`/`-`/`:`, así que `command()` las descarta y
**agota el timeout**. Conclusión: los verbos operativos **no** pueden manejarse con
`DeviceLink.command()` tal como está.

### Opciones

| | **Opción A — Cliente del dialecto (firmware intacto)** | **Opción B — Migrar firmware al framing canónico** |
|---|---|---|
| **Qué** | Nuevo cliente serial `modules/emvy/link.py` (`EmvyLink`) que envía una línea y lee hasta un sentinela conocido (`RESP:`/`ERR:`/`JSON_END`/`EMU:DONE`). Reusa `DeviceLink` solo para `ping`/`info`/`identify`. | Reescribir las respuestas de `emvyCommand()` a `:key value` + `+OK`/`-ERR`. El módulo host usa `DeviceLink.command()` y `device_session` estándar. |
| **Firmware** | Sin cambios (o mínimos, §7 menores). | Cambios sustanciales (§7 FW-1…FW-3), con bump de versión y feature-detect. |
| **Riesgo** | Bajo. No toca hardware validado. | Medio/alto: reflashear placas, re-probar cada verbo, mantener retrocompat con `emvy/readers/bombercat.py` (consumidor existente del dialecto). |
| **Alineación patrón CLI** | Parcial (un cliente serial paralelo). | Total (un solo protocolo, reusa helpers de detección/sesión/auto-flash). |
| **Auto-flash** | N/A (built-from-source, §5). | N/A igual, pero desbloquea `requires=` si algún día hay uf2 prebuilt. |

### Recomendación — híbrida por fases

1. **Fases 1–4 con Opción A:** entregan `bombercat emvy {info,read,apdu,tag,mag,emu}`
   funcionando, sin tocar el firmware. Valor inmediato, riesgo bajo.
2. **Fase 5 con Opción B (estratégica, opcional):** migrar el framing cuando el equipo
   decida hacer de EMVyBomberCat un ciudadano de primera clase del contrato. El header
   del firmware ya lo declara como el estado final buscado.

Esta ruta permite parar tras la Fase 4 con un producto completo y usable, y retomar la
Fase 5 como deuda técnica planificada.

> **Decisión tomada (2026-09-21):** el equipo resuelve el conflicto a favor de la
> **Opción B — Migrar el firmware al framing canónico**. Se descarta la ruta híbrida
> recomendada arriba (Opción A + Fase 5 opcional): el objetivo es que EMVyBomberCat sea
> un ciudadano de primera clase del contrato `+OK`/`-ERR`/`:key value` y que el módulo
> host reuse `DeviceLink.command()`/`device_session` sin un cliente serial paralelo.
> Esto **resuelve P-3** (la migración de framing sí se ejecuta) y reordena la hoja de
> ruta: el trabajo antes numerado como **Fase 5 + §7 (FW-1…FW-3)** pasa a ser el
> workstream fundacional. `EmvyLink` se mantiene solo como puente transitorio para
> retrocompat (D7), no como destino. Ver la entrada **Sesión 2** del Progress Log.

---

## 5. Divergencias respecto al patrón CLI

Documentadas para que ninguna sesión futura las «descubra» y rompa algo:

- **D1 — Framing operativo distinto.** El núcleo del §4. Es la razón de existir del
  cliente `EmvyLink` (Opción A) o de la Fase 5 (Opción B).
- **D2 — Sin uf2 prebuilt → sin auto-flash.** ⚠️ **SUPERADA (Sesión 9).** El supuesto
  —«el release feed no tiene `EMVyBomberCat.uf2`»— dejó de ser cierto cuando
  `build-firmware.yml` empezó a compilar el sketch junto a los de Electronic Cats y a
  adjuntarlo a cada release. Hoy `emvy` **sí** usa `device_session(requires=CAP_EMVY)` y
  auto-flashea bajo la política habitual; el gate por identidad
  (`info.fw_name == "emvybombercat"`) se conserva como **segunda** verja, porque
  `ensure_firmware` puede dar por buena una placa por *banner* y estos comandos manejan
  hardware. Texto original, para contexto: EMVyBomberCat se compila con arduino-cli y el
  release feed (`ReleaseCache`) no lo tiene, así que añadirlo a `CAPABILITY_PROVIDER`
  haría que `ensure_firmware` intente flashear una imagen inexistente.
- **D3 — Capacidades honestas.** ⚠️ **RESUELTA (Sesión 9), junto con P-1.** Declara
  `{monitor, identify, passthrough}` **+ `CAP_EMVY`**, la capacidad que cubre toda su
  superficie operativa y que **sí** tiene entrada en `CAPABILITY_PROVIDER` (ver D2
  superada). Se prefirió una sola capacidad de grupo a varias finas (`CAP_EMV`,
  `CAP_EMU`, …): todas las serviría la misma imagen, así que separarlas no distinguiría
  nada que `ensure_firmware` pudiera usar.
- **D4 — Estado con flanco / secuencia obligatoria.** El passthrough exige
  `WAIT → APDU: … → RELEASE`. El módulo `emvy apdu` debe gestionar esa sesión, no
  exponer `APDU:` suelto.
- **D5 — Comandos asíncronos que stremean.** `EMU:`/`EMUEMV:` no devuelven una sola
  respuesta: stremean hasta `EMU:DONE`/`STOP`. Requiere un lazo de lectura tipo
  `DeviceLink.stream()`, no `command()`. Ctrl-C debe enviar `STOP` antes de salir.
- **D6 — `REBOOT` re-enumera USB.** Cualquier comando que reinicie deja el puerto
  muerto; el módulo debe avisar «reconecta la placa», no intentar reusar el link.
- **D7 — Consumidor existente del dialecto.** `emvy/readers/bombercat.py` (proyecto
  EMVy Controller, fuera de este repo) ya habla este dialecto. La Opción A convive con
  él sin fricción; la Opción B (Fase 5) debe mantener retrocompatibilidad o versionar.

---

## 6. Fases de implementación

> Cada fase es un incremento entregable y probable en aislamiento. Complejidad:
> **bajo** = mecánico · **medio** = diseño local + tests · **alto** = toca hardware/firmware o contrato.

### Fase 0 — Baseline y captura de transcripciones · complejidad: **bajo**

**Objetivos:** confirmar el estado actual y capturar la *verdad de campo* (respuestas
seriales reales) que servirán de fixtures para los tests sin hardware.

**Prerrequisitos:** una BomberCat con EMVyBomberCat flasheado; una tarjeta EMV contactless
de prueba; `arduino-cli` si hay que compilar.

**Tareas:**
1. Verificar reconocimiento actual: `bombercat status`, `bombercat device list`,
   `bombercat identify` contra la placa. Anotar salida real.
2. Con un monitor serie (`screen`/`minicom` @115200) capturar transcripción **literal**
   de cada verbo del §2.3 con y sin tarjeta: `WAIT`, `APDU:00A4040007…`, `RELEASE`,
   `TAG`, `SCAN 500`, `CARDSCAN`, `EMU:D101…`, `EMUEMV`, `STOP`, `NFCINFO`.
3. Guardar las transcripciones en `tests/fixtures/emvy/*.txt` (crear carpeta).
4. Crear el esqueleto de módulo vacío `modules/emvy/{__init__.py,cli.py}`.

**Criterios de éxito:** `status` reporta `EMVyBomberCat` con `detected: handshake
(certain)`; existen fixtures de todos los verbos; el paquete `modules/emvy` importa.

---

### Fase 1 — Cliente serial del dialecto (`EmvyLink`) · complejidad: **medio**

**Objetivos:** una capa de transporte que hable el dialecto operativo, aislada y testeable
sin hardware.

**Prerrequisitos:** Fase 0 (fixtures).

**Tareas:**
1. Crear `modules/emvy/link.py` con clase `EmvyLink` (composición sobre
   `serial.Serial`, reusando `open_serial`/`DEFAULT_BAUDRATE` de `core/usb_connection`).
2. Métodos:
   - `exchange(line, terminators, timeout) -> list[str]` — envía `line\n`, lee líneas
     hasta que una haga match con un terminador (regex/prefijo: `RESP:`, `ERR:`, `OK`,
     `READY:`, `NFCINFO:`) o timeout. Devuelve todas las líneas (incluidas `# …` logs).
   - `stream_until(sentinel, on_line, stop_cmd)` — para `SCAN`/`EMU:`: itera líneas,
     llama `on_line`, corta en `JSON_END`/`EMU:DONE`; en Ctrl-C envía `stop_cmd`.
3. Parsers en `modules/emvy/parser.py`:
   - `parse_apdu_resp(lines) -> bytes|Error`
   - `parse_scan_json(lines) -> dict` (extrae el bloque entre `JSON_START`/`JSON_END`).
   - `parse_tag(line) -> {proto, uid}`
   - `parse_cardscan(line) -> {aid, pan, exp, t2}`
   - `parse_emu_event(line) -> {kind, …}` (RX/TX/MSG-SENT/DONE).
4. Tests host `tests/test_emvy_link.py` / `tests/test_emvy_parser.py` con un
   `FakeSerial` que reproduce los fixtures de Fase 0 (patrón de `tests/conftest.py`).

**Criterios de éxito:** unit tests verdes; `EmvyLink.exchange("APDU:…")` devuelve el
`RESP:` parseado; `stream_until` corta correctamente en `EMU:DONE`; cobertura de las
ramas de error (`ERR:NOCARD`/`BADAPDU`/`TXFAIL`).

---

### Fase 2 — Grupo `bombercat emvy` + gating por identidad · complejidad: **medio**

**Objetivos:** el grupo de comandos existe, se registra, y gate-a honestamente por
firmware sin auto-flash (D2).

**Prerrequisitos:** Fase 1.

**Tareas:**
1. En `modules/emvy/cli.py`: `@click.group("emvy")` con help
   («EMVy Controller swiss-army commands (requires the EMVyBomberCat firmware)»).
2. Helper `_emvy_session(port, device_id, trace)`:
   - `resolve_port()` → `DeviceLink(...).open()` → `ping()`.
   - `info()` y verificar `fw_name == "emvybombercat"`; si no, `print_error` +
     hint «compila/flashea EMVyBomberCat desde `bombercat-firmware/EMVyBomberCat`»
     (NO auto-flash). Cerrar `DeviceLink` y abrir `EmvyLink` sobre el mismo puerto para
     los verbos operativos (o mantener ambos; decidir en P-2).
3. Subcomando `emvy info`: muestra `fw`, `state`, y la superficie operativa disponible
   (tabla, reusando `rich.table.Table` como `firmware_status_cmd`).
4. Registrar en `modules/core/cli.py`: `from ..emvy.cli import emvy as _emvy` +
   `cli.add_command(_emvy)` en `main_cli()`.
5. Test `tests/test_cli_emvy.py`: `emvy info` corre contra fake; y **refuta** con exit≠0
   y mensaje claro cuando el fake reporta otro `fw_name`.

**Criterios de éxito:** `bombercat emvy --help` lista el grupo; `bombercat emvy info`
funciona contra la placa y **rechaza** limpiamente contra NFCGate/otro firmware.

**Decisión pendiente P-1:** ¿capacidades nuevas (`CAP_EMV`/`CAP_EMU`) o gate por id?
→ recomendación: **gate por id** (más honesto para un firmware único; evita tocar
`CAPABILITY_PROVIDER`). **P-2:** ¿un `EmvyLink` que también haga `ping`, o dos objetos
(`DeviceLink` para discovery + `EmvyLink` para operativo)? → recomendación: `EmvyLink`
único con un `ping()` propio para no abrir el puerto dos veces.

---

### Fase 3 — Lectura EMV y passthrough de APDU · complejidad: **medio/alto**

**Objetivos:** los dos comandos de mayor valor: leer una tarjeta y tunelizar APDUs.

**Prerrequisitos:** Fase 2.

**Tareas:**
1. `emvy read [--amount CENTS] [--json] [--raw]`:
   - Envía `SCAN <cents>`, usa `stream_until("JSON_END")`, parsea el JSON, imprime tabla
     PAN/expiry/label/AID/ARQC/ATC/AIP/track2 (reusa `print_field`); `--json` vuelca crudo.
   - Maneja `# ERROR: Timeout` → exit 1 con mensaje.
2. `emvy apdu`:
   - Modo one-shot: `emvy apdu 00A4040007A0000000031010` → hace `WAIT` → `APDU:` →
     imprime `RESP:` → `RELEASE`.
   - Modo interactivo/stdin: `--stdin` lee un APDU por línea dentro de una sola sesión
     `WAIT … RELEASE` (útil para scripts EMV). Ctrl-C envía `RELEASE`.
   - Gestiona la secuencia obligatoria (D4) y los errores `ERR:NOCARD/BADAPDU/TXFAIL`.
3. Salida CSV/JSON opcional consistente con `readers`/`tags` (`write_json`).
4. Tests con fixtures de `SCAN` (éxito y timeout) y de una sesión `WAIT/APDU/RELEASE`.

**Criterios de éxito:** con tarjeta real, `emvy read` imprime PAN+expiry+AID; `emvy apdu
<select ppse>` devuelve el FCI; sin tarjeta, ambos fallan con mensaje claro y exit≠0.

---

### Fase 4 — Tag · Magspoof · Emulación · complejidad: **medio**

**Objetivos:** completar la «navaja suiza»: tag UID, banda, y emulación observable.

**Prerrequisitos:** Fase 3.

**Tareas:**
1. `emvy tag` → `TAG`, parsea `TAG:<proto> UID:<hex>`.
2. `emvy mag --t1 <t1> --t2 <t2>` (tracks nombrados, al menos uno; opción
   independiente para swipe de un solo track) → `MAG:t1|t2`, espera `OK`.
   Validación local laxa `_validate_mag_track` (el firmware `emvyMagPlay`
   acepta tracks con o sin centinelas) — **no** reusa la validación estricta de
   `modules/core/track2.py`, que exige sentinelas ISO. El decode ISO de
   `core/track2.py`/`core/track_parser.py` sí se reusa en `emvy read` para
   mostrar estándar + service code del track2 escaneado (equivalente EMV tag 57).
3. `emvy emu ndef <hex>` → arranca `EMU:<hex>`, **stremea** `EMU:RX/TX/MSG-SENT` en vivo
   (formato legible), corta con Ctrl-C → `STOP` (D5). `--timeout` opcional.
4. `emvy emu card [--from-ram | --pan … --exp … --track2 …]` → `EMUEMV[:…|RAM]`, mismo
   streaming observable.
5. `emvy cardscan` → `CARDSCAN`, parsea `EMU:SCANNED …` (alimenta `emu card --from-ram`).
6. `emvy reboot` → `REBOOT`, avisa que el USB se re-enumera (D6); no reusar el link.
7. `emvy nfcinfo` → diagnóstico I2C del PN7150.
8. Tests de streaming (fake que emite N líneas `EMU:*` y luego `EMU:DONE`).

**Criterios de éxito:** cada verbo del §2.3 tiene subcomando; la emulación muestra los
APDUs del lector en vivo y se cancela limpiamente; `reboot` no deja el link colgado.

---

### Fase 5 — Alineación del contrato (migración de framing) · complejidad: **alto** · *opcional/estratégica*

**Objetivos:** convertir el dialecto operativo en `+OK`/`-ERR`/`:key value` para reusar
`DeviceLink.command()`/`device_session` y retirar (o adelgazar) `EmvyLink`.

**Prerrequisitos:** Fases 1–4 estables; consenso del equipo (toca firmware y un consumidor
externo, D7).

**Tareas:** ver [§7](#7-cambios-requeridos-en-el-firmware) (FW-1…FW-3). Alto nivel:
1. Envolver cada respuesta operativa en el framing canónico detrás de un bump de versión
   y feature-detect (el host detecta la variante por `info`).
2. Migrar el módulo host a `DeviceLink.command()`; mantener `EmvyLink` como *fallback*
   para firmwares viejos hasta deprecarlo.
3. Introducir capacidades (D3) sólo si el equipo quiere `status` más rico; **sin**
   `CAPABILITY_PROVIDER` (D2).
4. Re-probar los 12 verbos en hardware; verificar retrocompat con `emvy/readers/bombercat.py`.

**Criterios de éxito:** el módulo host opera sin `EmvyLink`; firmwares viejos siguen
funcionando por fallback; suite verde en ambas ramas de framing.

---

### Fase 6 — Docs, tests de CLI y empaquetado · complejidad: **bajo/medio**

**Objetivos:** dejar la integración lista para release.

**Prerrequisitos:** hasta Fase 4 (o 5).

**Tareas:**
1. `docs/commands/emvy.md` (patrón `docs/commands/magspoof.md`) + fila en
   `docs/reference.md` y `docs/usage.md`.
2. Completar `tests/test_cli_emvy.py` (todos los subcomandos, ramas de error, refutación).
3. Verificar tab-completion (el grupo se registra igual que los demás → sale gratis).
4. Nota en `README.md`/`docs/limitations.md`: EMVyBomberCat es built-from-source, sin
   auto-flash.

**Criterios de éxito:** `pytest` completo verde; `bombercat emvy --help` documentado;
docs coherentes con el resto.

---

## 7. Cambios requeridos en el firmware

**Fases 1–4 (Opción A): CERO cambios obligatorios.** El firmware queda intacto.
Cambios **menores opcionales** que ayudarían al host sin romper nada:

- **FW-0a (opcional, bajo):** en `info`, añadir una línea `:ops emv,passthrough,tag,mag,emu`
  para que `emvy info` descubra la superficie sin hard-codearla. No rompe el contrato
  (líneas `:key value` extra son ignoradas por clientes que no las esperan).
- **FW-0b (opcional, bajo):** que `TAG`/`CARDSCAN` emitan también una variante
  `:tag proto=… uid=…` / `:card aid=… pan=…` además del texto libre, para parseo robusto.

**Fase 5 (Opción B): cambios sustanciales, documentados uno a uno.** Todos detrás de un
**bump de `BOMBERCAT_FW_VERSION`** y feature-detect por `info` (retrocompat, D7):

- **FW-1 — Framing de respuestas puntuales.** En `emvyCommand()` (`.ino:3462+`):
  - `WAIT`: `READY:` → `+OK ready` ; `ERR:NOCARD` → `-ERR nocard`.
  - `APDU:`: `RESP:<hex>` → `:resp <hex>` + `+OK` ; `ERR:*` → `-ERR <reason>`.
  - `RELEASE`/`MAG:`/`STOP`: `OK` → `+OK`.
  - `TAG`: `TAG:<proto> UID:<hex>` → `:proto <p>` `:uid <hex>` `+OK`.
  - `CARDSCAN`: `EMU:SCANNED …` → `:aid` `:pan` `:exp` `:t2` `+OK` (errores `-ERR`).
  - `NFCINFO`: `NFCINFO: fwver=<n>` → `:fwver <n>` `+OK` / `-ERR chip`.
- **FW-2 — `SCAN` estructurado.** Mantener el JSON pero terminar con `+OK`; o emitir el
  JSON en una línea `:json <…>` + `+OK`. Los `# …` logs siguen siendo ruido ignorado.
- **FW-3 — Streaming de emulación.** `EMU:*` puede quedarse como stream de líneas de
  progreso (son «log noise» tolerado por `DeviceLink.stream()`), pero el **fin** debe
  ser un terminador claro: `EMU:DONE …` → `:sent <n>` `+OK`. Así `command()` puede
  cerrar la operación de arranque y `stream()` observar el resto.
- **Riesgos:** re-flashear placas de campo; el consumidor externo
  `emvy/readers/bombercat.py` debe migrar o usar el fallback. Mitigación: feature-detect
  por versión + `EmvyLink` como puente durante la transición.

> **Regla de oro:** nunca tocar los pines fijos (`IRQ=11 VEN=13 ADDR=0x28`) ni el
> Discovery Contract ya consumido (`ping`/`info`/`identify`). Solo se reencuadra la
> **capa operativa**.

---

## 8. Progress Log

> **Formato de entrada** (la más reciente arriba). Cada sesión añade una entrada nueva;
> no se borran las anteriores. Leer solo la entrada superior basta para reanudar.

### Sesión 9 — 2026-09-22

- **Fase en curso:** fuera de las fases originales — **D2 queda superada** por un cambio
  en el repo de firmware, y con ella la decisión P-1 («gate por id, sin
  `CAPABILITY_PROVIDER`»).
- **Qué cambió el supuesto:** `bombercat-firmware/.github/workflows/build-firmware.yml`
  descubre los sketches por `<nombre>/<nombre>.ino` y compila **todos** los del root, así
  que `EMVyBomberCat/` entra en el build igual que los sketches de Electronic Cats:
  `EMVyBomberCat.uf2` se adjunta a cada release y `ReleaseCache` puede encontrarlo. El
  «no hay imagen que flashear» que sostenía D2 ya no es cierto.
- **Entregado en esta sesión (auto-flash para `emvy`):**
  - ✅ **`CAP_EMVY`** en `modules/core/firmwares.py`, declarada por `emvybombercat`
    (junto a monitor/identify/passthrough) y **exclusiva** de esa imagen.
  - ✅ **`CAPABILITY_PROVIDER[CAP_EMVY] = "emvybombercat"`** en
    `modules/core/requirements.py` → `requirement_for` resuelve `image_name`
    `EMVyBomberCat`, que es el stem que `ReleaseCache.find()` espera.
  - ✅ **`_emvy_session` usa `device_session(..., requires=CAP_EMVY)`**: detección sin
    handshake + `ensure_firmware` bajo la política habitual (`--auto-flash` /
    `BOMBERCAT_AUTO_FLASH` / ask-on-TTY), exactamente como `magspoof`/`tags mifare`.
  - ✅ **El gate por identidad se mantiene, pero como segunda verja**: auto-flash puede
    dar por buena una placa por *banner* (probable, no seguro), y estos comandos manejan
    hardware — así que `info.fw_name == "emvybombercat"` se sigue comprobando antes del
    primer verbo operativo. El mensaje de refutación ahora apunta a
    `bombercat flash EMVyBomberCat` / `--auto-flash`, no a compilar con arduino-cli.
  - ✅ **`status` sugiere `emvy`**: rama `CAP_EMVY` en `_next_steps` (`modules/core/cli.py`)
    **antes** que la de `CAP_PASSTHROUGH`, que describía el bridge del ESP32 y no esto.
  - ✅ **Tests:** `test_emvy_requires_cap_emvy` en `tests/test_autoflash_wiring.py` (ya
    son seis call points, no cinco), exclusividad de `CAP_EMVY` en `tests/test_firmwares.py`,
    y el assert del hint en `tests/test_cli_emvy.py` actualizado. Suite completa en verde
    (1008 tests) — `use_link` ya stubbea `resolve_status_port`/`ensure_firmware`, así que
    el resto de tests de `emvy` no necesitaron cambios.
  - ✅ **Docs:** `docs/commands/emvy.md` (§«Auto-flash, then a firmware-identity check»,
    Quick Start y Notes), `docs/limitations.md`, `docs/reference.md` (tabla de grupos +
    lista de §Auto-flash) y `README.md`.
- **Bloqueadores / decisiones pendientes:** sin cambios respecto a la Sesión 8 — siguen
  pendientes la captura de fixtures de campo (Fase 0, necesita placa) y P-3 (Fase 5 /
  migración de framing).
- **Notas adicionales:**
  - D2 y D3 quedan marcadas como superadas *in situ* (§5) en lugar de borradas: el
    razonamiento sigue siendo correcto para el supuesto en que se escribió.
  - Verificar en el primer release tras este cambio que `EMVyBomberCat.uf2` aparece de
    verdad entre los assets; si el build fallara por librerías, `bombercat flash --list`
    lo delataría antes que un auto-flash a medias.

### Sesión 8 — 2026-09-22

- **Fase en curso:** **Fase 6 — Docs, tests de CLI y empaquetado → COMPLETADA** (sin
  hardware). Cierra el tramo entregable de la ruta: Fases 1–4 (código) ya estaban
  completas y la Fase 6 era el único trabajo restante que no requería placa. Quedan
  pendientes, a propósito, solo las tareas que **sí** necesitan hardware o consenso: la
  captura de fixtures de campo (Fase 0) y la migración de framing del firmware (Opción B /
  §7 FW-1…FW-3), que el equipo dejó como workstream fundacional aparte.
- **Entregado en esta sesión:**
  - ✅ **`docs/commands/emvy.md`** (nuevo, patrón `docs/commands/magspoof.md`): Quick
    Start; sección de subcomandos con dos apartados propios de este grupo — **gating por
    identidad de firmware sin auto-flash** (por qué `emvy` no puede reflashear: built-from-source
    con arduino-cli, no hay `.uf2` en el release feed → refuta con hint de compilar/flashear,
    D2/§5) y **la secuencia de enganche + streaming** (WAIT→APDU→RELEASE siempre suelta la
    tarjeta; `emu` stremea y Ctrl-C manda STOP y sale 0). Un apartado por verbo: `info`,
    `read` (incluye la nota tag-57 / analysis solo-presentación / `--json` passthrough
    exacto), `apdu` (one-shot y `--stdin`, ramas `nocard/badapdu/txfail`, Ctrl-C = abort
    exit 1), `tag` (formato `TAG:<proto> TECH:<tech> UID:<hex>`), `mag` (`--t1`/`--t2`,
    validación laxa deliberada vs magspoof), `cardscan`, subgrupo `emu` (`ndef`/`card`,
    ambos streaming, `--from-ram`, mutuamente excluyentes, "not a working card"), `nfcinfo`,
    `reboot` (re-enumera USB, D6). Todos los ejemplos y textos verificados **contra el
    código** (`modules/emvy/cli.py`) y contra el `--help` real, no inventados.
  - ✅ **Wiring de la doc en el resto del árbol de docs:** fila `emvy` en la tabla de
    Commands de `docs/reference.md` + entrada en el índice de arriba; fila en la tabla de
    quick-links de `docs/usage.md`; **nueva entrada con ancla** en `docs/limitations.md`
    (`#emvybombercat-is-built-from-source`) explicando el gating por identidad y el porqué de
    no auto-flashear; menciones en `README.md` (intro, bloque de quick-start con 4 comandos,
    y fila de la tabla de documentación). Todos los enlaces internos usados por `emvy.md`
    resuelven a anclas existentes (`reference.md#device-selection`/`#global-options`/`#auto-flash`,
    `magspoof.md#magspoof-show`, la nueva de `limitations.md`, y el propio plan).
  - ✅ **Tab-completion (tarea 3): verificada, sale gratis.** `emvy` se registra como
    cualquier otro grupo (`modules/core/cli.py:42` import + `:597` `cli.add_command(_emvy)`),
    y aparece en el help raíz entre los Commands — click deriva la completion de esa misma
    lista de subcomandos, así que no hay nada específico que añadir.
  - ✅ **Tests (tarea 2): ya completos, sin cambios necesarios.** `tests/test_cli_emvy.py`
    ya cubre todos los subcomandos, sus ramas de error y la refutación por firmware
    equivocado / sin `fw_name` / sin handshake (incl. que el gate **cierra el link** al
    refutar). No se añadieron tests porque la superficie no cambió: la Fase 6 es
    documentación y empaquetado, no código nuevo.
- **Estado de la suite completa:** `pytest` (venv `~/BomberCat`) → **1005 passed** (igual
  que Sesión 7 — esta sesión no toca código de producción ni de test, solo docs). Subconjunto
  `emvy` (`test_cli_emvy.py`/`test_emvy_link.py`/`test_emvy_parser.py`) → 82 passed.
- **Decisiones tomadas en esta sesión:**
  - **La doc se escribió contra el código, no contra la tabla §2.3 del plan.** Donde el plan
    y el firmware divergen (p.ej. `TAG:` lleva `TECH:`, hallazgo de la Sesión 6), la doc
    sigue al código. Los formatos de wire que aún no tienen fixture de campo se marcan como
    tales en la doc (bloque «Field names are not yet fixed by a captured fixture») en lugar
    de presentarlos como verdad confirmada.
  - **No se añadieron anclas al bloque «Old anchor links (pre-restructure)» de `reference.md`.**
    Ese bloque es solo para comandos que *antes* vivían en `reference.md` y se movieron;
    `emvy` es nuevo y nunca tuvo un ancla antigua, así que meterlo ahí sería incorrecto.
- **Pendiente / no hecho a propósito** (sin cambios respecto a Sesión 7, salvo que Fase 6
  ya no está pendiente):
  - **Captura de campo (Fase 0)** — sigue necesitando placa + tarjeta. Al haber hardware,
    `emvy read --raw` / `emvy tag --json` fijan los fixtures que confirmen el JSON de `SCAN`,
    el formato exacto de `track2` (tag-57, ¿padding `F` siempre?) y `TAG:`/`TECH:`.
  - **Migración FW (Opción B, §7 FW-1…FW-3)** — no iniciada; es el workstream fundacional
    que el equipo decidió (nota de Decisión 2026-09-21 en §4). Con Fase 4 completa hay 12
    verbos con cliente host maduro contra los que validar la migración verbo a verbo.
  - **Vendor trees** (`EMVy_Controller/vendor/`, `EMVY_Controller_Glitchboi/vendor/`) — no
    tocados; si se sincronizan, replicar también los archivos de docs de esta sesión.
- **Próximos pasos exactos para la siguiente sesión:** (1) captura de fixtures de campo con
  hardware; (2) arrancar la migración FW Opción B si se decide (bump de
  `BOMBERCAT_FW_VERSION` + feature-detect por `info`, luego FW-1…FW-3). El tramo sin
  hardware de la hoja de ruta queda cerrado con esta sesión.

### Sesión 7 — 2026-09-22

- **Fase en curso:** **Fase 4 completada — refinamientos post-Fase-4** (reutilización de
  lógica de tracks entre `magspoof` y `emvy`). No es una fase nueva del roadmap: es
  consolidación de la superficie ya entregada. Fase 6 (docs/empaquetado) sigue pendiente.
- **Origen:** análisis pedido sobre qué lógica de tracks de `magspoof/cli.py` es segura
  de reutilizar en `emvy/cli.py` **sin cruzar la frontera de firmware** — en concreto,
  poder seleccionar qué pista se reproduce cuando la tarjeta trae una sola. Conclusión
  rectora, ya aplicada abajo: **se comparte lo que *decodifica/entiende* un track (puro,
  sin serial); nunca lo que lo *valida contra un firmware concreto* ni lo que habla con
  el dispositivo.** Esta regla debe guiar futuras features que toquen tracks.
- **Entregado en esta sesión:**
  - ✅ **`emvy mag` migrado de posicionales a `--t1`/`--t2`** (reemplaza la firma
    `emvy mag TRACK1 TRACK2` de la Sesión 6). Pistas **nombradas e independientes**, al
    menos una obligatoria → un swipe de una sola pista deja de exigir un `""` posicional y
    pasa a ser de primera clase (`emvy mag --t2 ";…?"`). Verificado contra firmware
    (fuente de verdad): `modes_mag.ino::emvyMagPlay()` considera una pista **presente solo
    si es no vacía** (`track && track[0]`), así que `--t1 ""` se lee como "sin pista 1";
    el parser `MAG:` (`EMVyBomberCat.ino:3626`, `rest.indexOf('|')`) separa por `|` y
    tolera cualquier lado vacío; el buffer es `char[128]` con `strncpy(...,127)` → tope
    **127** chars. La validación laxa `_validate_mag_track` queda **intacta** (sin salto
    de línea, sin `|`, ≤127) — se mantiene la frontera: **no** se reusa el
    `_validate_track_data` estricto (que exige centinelas ISO que este firmware no pide).
    Sin ruta de compat porque el comando se incorporó en Fase 4 esta misma semana y aún
    no es público → ningún script dependía de la firma posicional. El mensaje de éxito
    ahora distingue `1-track`/`2-track`.
  - ✅ **Promoción de `track2.py` + `track_parser.py` a `modules/core/`** (`git mv`,
    historial preservado). Son módulos **puros y firmware-agnósticos** (nunca tocan el
    link serie): parseo ISO 7813, detección de estándar, análisis de Service Code. Vivían
    bajo `magspoof/` por accidente histórico; ahora son *core* y los comparten ambos
    grupos desde un solo lugar (evita que `emvy` importe desde `..magspoof`, lo que habría
    acoplado dos paquetes de firmware). `magspoof/cli.py` reengancha a `..core.track2` /
    `..core.track_parser` (uso real, **sin cambio de comportamiento**). El import interno
    `track_parser → track2` sigue válido (mismo paquete). Tests renombrados
    `test_magspoof_track2.py` → **`test_core_track2.py`** y `test_magspoof_track_parser.py`
    → **`test_core_track_parser.py`** (imports + cabeceras actualizados).
  - ✅ **`emvy read` enriquecido con decode del track2 escaneado** (primer consumidor de
    los módulos core desde `emvy`). **Hallazgo de campo (leyendo el `.ino`, no hardware):**
    el `track2` que devuelve un `SCAN` **no** es magstripe `;PAN=YYMMSCdisc?` sino el
    **EMV Track 2 Equivalent (tag 57)**: `PAN 'D' YYMM SC disc ['F'…]`, hex compacto con
    `'D'` de separador y padding `'F'` — confirmado en `EMVyBomberCat.ino:676-690`
    (`HexUtils::toCompact(t57,…,card.track2)` + `strchr(card.track2,'D')`). El
    `parse_track2` existente (que espera `;…=…?`) habría devuelto `None` contra ese
    formato. Solución: **dos helpers puros nuevos en `core/track2.py`** —
    `parse_track2_equivalent()` (parsea el tag-57 al mismo `Track2Data`, quita el padding
    `F`) y `to_iso_track2()` (acepta *cualquiera* de las dos formas y devuelve la ISO, o
    `None`). `emvy read` llama `to_iso_track2()` → `analyze_card()` y muestra `standard`
    + veredicto de `service code` (chip/PIN/fallback), con el mismo criterio de solo
    lectura que `magspoof show`. **`--json` se mantiene passthrough verbatim** del objeto
    del firmware — el análisis es capa de presentación humana, no sale en el JSON (respeta
    el contrato de igualdad exacta que ya asertaban los tests).
  - ✅ **Etiquetas/veredicto de presentación locales a `emvy`** (`_STANDARD_LABEL`,
    `_SC_VERDICT`): las cadenas de color/wording quedan en cada CLI (magspoof ya tiene las
    suyas, más largas); lo compartido es el **decode**, no la presentación — evita
    sobre-promover strings cosméticos a core.
  - ✅ **Tests** (+9 netos sobre la Sesión 6): en `test_core_track2.py`, cobertura de
    `parse_track2_equivalent` (split por `D`, strip de `F`, rechazo de magstripe) y
    `to_iso_track2` (equivalente→ISO, ISO passthrough, `None` en basura); en
    `test_cli_emvy.py`, `emvy read` decodifica un track2-equivalente (estándar + "chip
    required") y `--json` sigue siendo passthrough exacto aun con track2 decodificable;
    tests de `emvy mag` migrados a `--t1`/`--t2` (+ pista 1 sola, + `--t1 ""` tratado como
    ausente).
  - ✅ **Doc §6 Fase 4 actualizada** (tarea 2): refleja la firma `--t1`/`--t2`, la razón
    de la validación laxa, y que el decode ISO de core se reusa en `emvy read`.
- **Estado de la suite completa:** `pytest` (venv `~/BomberCat`) → **1005 passed**
  (996 en Sesión 6 → 998 tras la migración de `emvy mag` → 1005 tras core + `emvy read`).
  Sin referencias colgantes a las rutas antiguas (`modules/magspoof/track2*`) dentro de
  `bombercat-tools`.
- **Decisiones tomadas en esta sesión:**
  - **Frontera de firmware = validación, no decode.** Queda anotado en código (comentarios
    en `emvy/cli.py::mag_cmd` y en los headers de `core/track2.py`/`track_parser.py`) para
    que una sesión futura **no** unifique `_validate_mag_track` con `_validate_track_data`
    creyendo que son el mismo contrato: `emvy`/EMVyBomberCat acepta tracks sin centinelas,
    `magspoof`/CardDatabase los exige.
  - **`--json` de `emvy read` es passthrough del firmware, no del análisis.** El decode es
    conveniencia humana; meterlo en el JSON habría roto el contrato máquina y obligado a
    tocar tests de igualdad exacta. Si una feature futura necesita el análisis en JSON,
    que añada una sub-clave `analysis` explícita (como hace `magspoof show --json`), no que
    contamine las claves crudas del firmware.
  - **Los módulos de tracks son core, no de magspoof.** Cualquier grupo que necesite
    entender un track (PAN/expiry/Service Code/estándar) debe importar de
    `modules/core/track2.py` / `modules/core/track_parser.py`, no duplicar regex ni tablas
    de IIN.
- **Pendiente / no hecho a propósito:**
  - **Vendor trees.** Existen copias de `bombercat-tools` bajo `EMVy_Controller/vendor/` y
    `EMVY_Controller_Glitchboi/vendor/` (árboles separados); **no** se tocaron. Si se
    sincronizan, replicar el `git mv` a `core/` y los reenganches de import.
  - **Captura de campo (Fase 0)** sigue pendiente desde la Sesión 1: el formato tag-57 de
    `track2` se confirmó **por lectura del `.ino`**, no por captura con placa. Cuando haya
    hardware, `emvy read --raw` sobre una tarjeta real debe fijar un fixture que confirme
    tanto el JSON de `SCAN` como el formato exacto de `track2` (¿siempre con padding `F`?,
    ¿discretionary presente?).
- **Próximos pasos exactos para la siguiente sesión:** sin cambios respecto a la Sesión 6
  (captura de fixtures de campo; **Fase 6** docs/empaquetado; migración FW Opción B si se
  arranca). Añadido a la lista: al escribir `docs/commands/emvy.md` (Fase 6), documentar
  la firma `emvy mag --t1/--t2` y el bloque de análisis de `emvy read`.

### Sesión 6 — 2026-09-22

- **Fase en curso:** **Fase 4 — Tag · Magspoof · Emulación → COMPLETADA** (código +
  tests host verdes, sin hardware).
- **Entregado en esta sesión:**
  - ✅ **Hallazgo de campo (leyendo el firmware, no hardware):** antes de tocar
    `emvy tag`, se releyó `modes_tags.ino::emvyTagsRead()` línea a línea para
    confirmar el formato exacto de `TAG:`. El wire format real es
    `TAG:<proto> TECH:<tech> UID:<hex>` — un campo **`TECH:`** entre proto y UID
    que ni la tabla §2.3 del plan ni el `_TAG_RE` de Fase 1 contemplaban (ese
    regex esperaba `TAG:<proto> UID:<hex>` y habría fallado a parsear —
    `unrecognized TAG reply` — contra un board real). Corregido en
    `modules/emvy/parser.py`: `_TAG_RE` ahora acepta `TECH:` opcionalmente
    (para no romper si algún build lo omite) y `parse_tag()` devuelve
    `{proto, uid}` + `tech` cuando está presente. Ya no queda bloqueado por la
    captura de campo pendiente para *este* verbo — la lectura del `.ino` fue
    suficiente para confirmarlo con certeza (es el propio `Serial.print`
    literal), aunque la captura real con placa sigue siendo la tarea pendiente
    de Fase 0 para blindarlo con un fixture.
  - ✅ **`emvy tag [--json]`** — `TAG` vía `exchange(terminators=("TAG:","ERR:"),
    timeout=12s)` (margen sobre el propio budget de 8s del firmware) +
    `parse_tag`. Imprime protocolo/tech/UID o refuta con `ERR:NOTAG`.
  - ✅ **`emvy mag TRACK1 TRACK2`** — `MAG:<t1>|<t2>` vía `exchange()` +
    `raise_for_err`. **Validación local deliberadamente más laxa que
    `magspoof/cli.py::_validate_track_data`**: se leyó `modes_mag.ino::
    emvyMagPlay()` y el firmware acepta tracks *con o sin* sus centinelas ISO
    (`%..?`/`;..?`) y tolera cualquiera de los dos vacío (swipe de una sola
    pista) — reusar la validación estricta de magspoof habría rechazado
    entradas que el firmware sí sirve. La validación propia (`_validate_mag_track`)
    solo aplica lo que el firmware realmente exige: buffer de 127 chars, sin
    salto de línea, sin `|` embebido (rompería el separador de campos de
    `MAG:`). Exige al menos una pista no vacía (si no, no hay nada que
    reproducir).
  - ✅ **`emvy cardscan [--json]`** — `CARDSCAN` vía `exchange(terminators=
    ("EMU:SCANNED","ERR:"), timeout=25s)` (margen sobre el `pollCard(20000)`
    del firmware) + `parse_cardscan` (sin cambios — el wire format real
    coincide con lo documentado). Apunta a `emvy emu card --from-ram` en su
    mensaje de salida, cerrando el flujo on-device "escanear → RAM → reemular"
    que describe §2.3.
  - ✅ **`emvy emu ndef HEX [--raw] [-t/--timeout]`** y
    **`emvy emu card [--from-ram | --aid/--pan/--exp/--track2] [--raw]
    [-t/--timeout]`** (subgrupo `emvy emu`) — primer uso de
    `EmvyLink.stream_until` en la CLI (D5): `_emu_stream()` (helper compartido)
    manda el trigger (`EMU:<hex>` / `EMUEMV[...]`) con `send()` y luego
    stremea hasta `EMU:DONE`, formateando cada evento (`parse_emu_event`) como
    `EMU:<kind> campo=valor…` o cayendo a la línea cruda si no matchea. En
    **Ctrl-C**, `stream_until` ya manda `STOP` por su cuenta (contrato de
    Fase 1) y re-lanza `KeyboardInterrupt`; `_emu_stream` lo **atrapa** (a
    diferencia de `apdu_cmd`, que lo trata como abort con exit 1) porque
    cortar una emulación potencialmente larga con Ctrl-C es el modo normal de
    terminarla, no un error — imprime "stopped" y sale con código 0.
    `emu card` valida que `--from-ram` y los campos hex (`--aid/--pan/--exp/
    --track2`) sean mutuamente excluyentes; sin ninguno, manda `EMUEMV` a
    secas (tarjeta Visa de prueba *canned* del firmware).
  - ✅ **`emvy nfcinfo`** — `NFCINFO` vía `exchange(terminators=("NFCINFO:",))`
    + nuevo **`parse_nfcinfo()`** en `parser.py` (no existía en Fase 1).
    Nota: el firmware en éxito imprime **dos** líneas `NFCINFO:` (`fwver=…`
    y luego, tras re-armar el discovery, `discovery re-armado`);
    `exchange()` corta en la **primera** por diseño (basta para el
    diagnóstico) — la segunda línea queda sin leer en el buffer, descartada
    al cerrar el link, sin efecto práctico.
  - ✅ **`emvy reboot`** — `elink.send("REBOOT")` sin esperar respuesta (D6: el
    firmware resetea el MCU ~80ms después con `Serial.flush()` de por medio;
    esperar una réplica agotaría el timeout contra un puerto que va a
    desaparecer). Avisa que el USB se re-enumera y que hace falta reconectar.
  - ✅ **`_OPERATIONS`** en `emvy info` ampliada con `cardscan`/`nfcinfo`/
    `reboot` (antes solo listaba read/apdu/tag/mag/emu) para que la tabla
    siga siendo honesta sobre toda la superficie ya expuesta.
  - ✅ **`tests/test_cli_emvy.py`** (+23, total 49) — extendido `FakeEmvyLink`
    con `interrupt_after` (simula Ctrl-C a mitad de un stream, incluyendo el
    envío de `stop_cmd`, para poder probar el manejo de `_emu_stream` sin
    hardware). Cobertura: `tag` (proto/tech/uid, `--json`, `ERR:NOTAG`); `mag`
    (dos pistas, una sola pista, sin pistas → rechazo local, `|` embebido →
    rechazo local, error de firmware); `cardscan` (tabla, `--json`, `ERR:
    NOCARD`); `emu ndef` (stream formateado, `--raw`, hex inválido rechazado
    sin sesión, corte limpio en Ctrl-C con exit 0 y `STOP` enviado); `emu card`
    (trigger por defecto, `--from-ram`, campos custom, exclusión mutua, hex
    inválido); `nfcinfo` (fwver, chip muerto); `reboot` (envía `REBOOT`, avisa
    reconexión, cierra el link).
- **Estado de la suite completa:** `pytest` (venv `~/BomberCat`, el único con
  pytest instalado en esta sesión — `.venv-smoke`/`.venv-proto` del repo no lo
  tienen) → **996 passed** (antes 973 en Sesión 5; +23 tests nuevos). Smoke
  test manual: `python bombercat.py emvy --help` y `--help` de cada
  subcomando nuevo (`tag`, `mag`, `cardscan`, `emu`, `emu ndef`, `emu card`,
  `nfcinfo`, `reboot`) renderizan correctamente.
- **Decisiones tomadas en esta sesión:**
  - El hallazgo de `TECH:` en `TAG:` es la primera vez que este plan corrige
    una entrada de la tabla §2.3 sin haber tenido hardware en mano — vale la
    pena que la próxima sesión con placa física confirme el resto de la tabla
    (proto/tech como enteros exactos, formato de `UID:` vacío) contra un
    fixture real, no solo contra la lectura del `.ino`.
  - `_emu_stream` decide tratar Ctrl-C como salida limpia (exit 0), a
    diferencia de `apdu_cmd` (exit 1 + "aborted"): la diferencia de criterio
    es que un passthrough de APDU interrumpido a mitad de lote es un fallo
    parcial de la tarea pedida, mientras que Ctrl-C en una emulación
    potencialmente abierta (hasta 180s) es la forma prevista de terminarla.
  - Validación de `emvy mag` deliberadamente **no** reusa
    `magspoof/cli.py::_validate_track_data` — ver el punto de "Entregado" de
    arriba; están anotadas ambas funciones para que una sesión futura no las
    unifique por error asumiendo que son el mismo contrato.
- **Próximos pasos exactos para la siguiente sesión:**
  1. **(Campo, cuando haya placa + tarjeta)** — sigue pendiente desde la
     Sesión 1: capturar `tests/fixtures/emvy/*.txt` de los 12 verbos,
     confirmando en particular el formato real de `TAG:`/`TECH:` (recién
     corregido por lectura de código, no por captura) y los campos exactos
     del JSON de `SCAN` (`_READ_FIELDS` en `cli.py` sigue siendo alias +
     fallback, no verdad de campo).
  2. **Fase 6** — docs (`docs/commands/emvy.md` + fila en `docs/reference.md`/
     `docs/usage.md`), verificar tab-completion, nota en README/limitations
     sobre built-from-source sin auto-flash. Fases 1-4 ya están completas en
     código; falta el empaquetado para release.
  3. Si se arranca la migración FW (Opción B, aún no iniciada): bump de
     `BOMBERCAT_FW_VERSION` + feature-detect por `info`, luego FW-1…FW-3 (§7).
     Con Fase 4 completa, ahora hay 12 verbos con cliente host maduro contra
     los que validar la migración verbo a verbo.

### Sesión 5 — 2026-09-22

- **Fase en curso:** **Fase 3 — Lectura EMV y passthrough de APDU → COMPLETADA**
  (código + tests host verdes, sin hardware).
- **Entregado en esta sesión:**
  - ✅ **`modules/emvy/cli.py` — `_emvy_operative_session(port, device_id, trace)`**
    — nuevo context manager que resuelve P-2 (obsoleto bajo Opción B, pero seguía
    abierto para Fase 3 bajo el dialecto histórico): reusa `_emvy_session` para el
    gate de identidad (abre un `DeviceLink`, confirma `fw_name == emvybombercat`),
    **cierra** ese `DeviceLink` una vez pasado el gate, y reabre el mismo puerto
    como un `EmvyLink` (import nuevo `from .link import EmvyLink`). `EmvyLink` es
    global del módulo, igual que `resolve_port`/`DeviceLink`, para que los tests lo
    monkeypatcheen. `DeviceLink.close()` es idempotente (`_ser=None`), así que el
    `finally` de `device_session` sobre el `DeviceLink` ya cerrado no rompe nada.
  - ✅ **`emvy read [--amount CENTS] [--json] [--raw] [-t/--timeout SECS]`** —
    `elink.send(f"SCAN {cents}")` + `elink.stream_until("JSON_END", on_line=…)`.
    **Desvío deliberado del plan:** el firmware nunca manda `JSON_END` en el
    camino de error — solo dos un log `# ERROR: …` (p.ej. `# ERROR: Timeout`) y
    corta ahí (§2.3) — así que esperar el sentinela habría agotado el `--timeout`
    completo en cada fallo. Se resuelve con un `on_line` (`_abort_on_scan_error`)
    que lanza `EmvyError` en cuanto ve una línea `# ERROR`; como `EmvyLink.
    stream_until` no atrapa excepciones que no sean `KeyboardInterrupt`, propaga
    directo al `except EmvyError` del comando. `--raw` sustituye ese callback por
    `console.print` (streamea toda la transcripción cruda en vivo — pensado para
    que, cuando haya hardware, sirva para capturar los fixtures de Fase 0
    pendientes). La tabla no-`--json`/no-`--raw` usa una lista de alias por campo
    (`_READ_FIELDS`: PAN/expiry/label/AID/track2/ARQC/ATC/AIP con sinónimos como
    `exp`/`expiration`) porque **el esquema JSON real de `buildWebJson` sigue sin
    confirmarse** (la captura de campo de Fase 0 sigue bloqueada por hardware);
    cualquier clave no cubierta por los alias se imprime igual al final
    (fallback), para no ocultar datos de un JSON con forma distinta a la
    esperada.
  - ✅ **`emvy apdu [APDU] [--stdin] [--json] [-w/--wait MS]`** — implementa la
    secuencia obligatoria D4 (`WAIT` → uno o más `APDU:` → `RELEASE`) como una
    sola función, para que ningún subcomando exponga `APDU:` suelto:
    - `WAIT <wait_ms>` con un timeout de cliente `wait_ms/1000 + 5s` (margen
      sobre el propio presupuesto del firmware) — nuevo helper de parser
      **`raise_for_err(lines)`** (parser.py) convierte un `ERR:NOCARD` en
      `EmvyError` para los verbos "solo OK/ERR, sin payload" (WAIT/RELEASE/
      MAG/STOP); si `WAIT` falla, sale con exit 1 **sin** mandar `RELEASE`
      (`gPassthroughActive` nunca se armó, no hay nada que soltar).
    - Un `APDU:<hex>` por línea (una del argumento, o una por línea de stdin
      con `--stdin`) vía `parse_apdu_resp`; un `ERR:*` en una línea de stdin
      **no aborta el lote** — se reporta y se sigue con la siguiente,
      acumulando exit 1 al final (útil para un script que manda varios APDUs
      y quiere ver todos los resultados, no solo el primer fallo).
    - `RELEASE` se manda siempre en un `finally` una vez que `WAIT` tuvo
      éxito — incluido en Ctrl-C (D5/D4: nunca dejar la tarjeta enganchada).
    - Validación local de hex (`_validate_apdu_hex`) antes de abrir sesión
      para el argumento one-shot (falla rápido, sin tocar el puerto); para
      `--stdin` se valida línea a línea dentro del lazo (no se puede saber de
      antemano).
  - ✅ **`tests/test_cli_emvy.py`** (+18, total 26) — `FakeEmvyLink` local (no se
    tocó `conftest.py`: es específico de `emvy`, análogo a `FakeLink` pero habla
    `exchange()`/`stream_until()` en vez de `command()`) + fixture `use_emvy_link`
    que monkeypatchea `emvycli.EmvyLink`. Cobertura: tabla parseada, `--json`,
    `--raw` (sin la palabra "PAN" en la salida), corte temprano en `# ERROR`
    (sin esperar el timeout), `--amount`; en `apdu`: exclusión mutua
    argumento/`--stdin`, hex inválido rechazado **sin abrir sesión** (ninguna
    fixture de link se instala en ese test), one-shot + `--json`, `WAIT`
    fallido → **sin** `RELEASE`, `APDU:` fallido → **sí** `RELEASE`, `--stdin`
    con 2 líneas en una sola sesión, `--stdin` con un fallo que no corta el
    lote.
- **Estado de la suite completa:** `pytest` → **973 passed** (antes 960 en Sesión
  4; +18 tests nuevos de esta sesión, más los que ya sumaban las Fases 0-2).
  Smoke test manual: `python bombercat.py emvy --help|read --help|apdu --help`
  todos renderizan y listan `apdu`/`info`/`read`.
- **Decisiones tomadas en esta sesión:**
  - El helper P-2 pendiente ("¿un modo operativo en `_emvy_session` o cada
    subcommand abre su propio link?") se resuelve con
    `_emvy_operative_session`: un único punto que hace el gate + swap
    DeviceLink→EmvyLink, reutilizado por `read` y `apdu` (y listo para
    `tag`/`mag`/`emu` en Fase 4).
  - Los campos exactos del JSON de `SCAN` siguen siendo una suposición
    informada (alias + fallback), no una verdad de campo — la captura de
    fixtures de Fase 0 (tarea pendiente desde la Sesión 1) es lo único que
    puede confirmarlos. `emvy read --raw` queda como la herramienta para
    hacer esa captura en cuanto haya placa + tarjeta.
- **Próximos pasos exactos para la siguiente sesión:**
  1. **(Campo, cuando haya placa + tarjeta)** correr `emvy read --raw` y
     `emvy apdu <hex>` reales para (a) confirmar/corregir los alias de
     `_READ_FIELDS` contra el JSON real de `buildWebJson`, y (b) por fin
     capturar `tests/fixtures/emvy/*.txt` de los 12 verbos (Fase 0, tareas
     1-2, pendiente desde la Sesión 1).
  2. **Fase 4** — `emvy tag`, `emvy mag <t1> <t2>`, `emvy emu ndef/card`,
     `emvy cardscan`, `emvy reboot`, `emvy nfcinfo`. `tag`/`mag` son cortos
     (`exchange()` + `parse_tag`/`raise_for_err`); `emu`/`cardscan` necesitan
     el patrón de streaming con `on_line` en vivo que `read` ya estableció
     (formato legible de `EMU:RX/TX/MSG-SENT`, corte en Ctrl-C → `STOP`, D5).
  3. Si se arranca la migración FW (Opción B, aún no iniciada): bump de
     `BOMBERCAT_FW_VERSION` + feature-detect por `info`, luego FW-1…FW-3 (§7).

### Sesión 4 — 2026-09-21

- **Fase en curso:** **Fase 2 — Grupo `bombercat emvy` + gating por identidad → COMPLETADA**
  (código + tests host verdes, sin hardware).
- **Entregado en esta sesión:**
  - ✅ **`modules/emvy/cli.py`** — grupo `@click.group("emvy")` con help puesto, más:
    - **`_emvy_session(port, device_id, trace)`** — context manager que reusa
      `device_session(resolve_port, DeviceLink, "emvy", "EMVyBomberCat", …)` (con
      `requires=None`: resuelve puerto, abre `DeviceLink`, verifica `ping()`) y **añade el
      gate por identidad**: `info().data["fw_name"]` debe ser `emvybombercat`. Si no coincide
      (o `info` falla), `print_error` + `print_info` con hint de **compilar/flashear desde
      `bombercat-firmware/EMVyBomberCat` con arduino-cli** y `SystemExit(1)` — **nunca**
      auto-flash (D2/§5, **P-1 resuelto = gate por id**). Cede `(target, link, info)`.
      `resolve_port`/`DeviceLink` se referencian como globals del módulo para que los tests
      los monkeypatcheen (fixture `use_link`), igual que `_magspoof_session`.
    - **`emvy info`** — gate + tabla `rich` (version, state) + lista estática de la superficie
      operativa (`read/apdu/tag/mag/emu`, cada uno futura subcomando de Fases 3-4). Superficie
      **hard-codeada** a propósito: el firmware no ofrece query de capacidades y FW-0a
      (`:ops …` en `info`) aún no existe.
  - ✅ **Registro en `modules/core/cli.py`** — `from ..emvy.cli import emvy as _emvy` +
    `cli.add_command(_emvy)` en `main_cli()` (junto a `_magspoof`).
  - ✅ **`tests/test_cli_emvy.py`** (8) — registro en el root CLI, `emvy --help` lista `info`,
    `info` OK (version/state/operaciones), campos vacíos → `—`, y **refutación** con exit 1 +
    hint en 4 escenarios: otro `fw_name` (nfcgate), sin `fw_name` (build pre-`fw_name`), placa
    que no hace handshake (`ping_ok=False`), y cierre del link tras refutar.
- **Estado de la suite completa:** `pytest` → **960 passed** (sin fallos). El fallo de entorno
  ajeno de la Sesión 3 (`test_cli_setup_env::test_the_sudo_hint…`) no reproduce en el venv
  `~/BomberCat`. Smoke test manual: `bombercat emvy --help` lista el grupo y `info`.
- **Decisiones confirmadas:** **P-1 = gate por id** (sin tocar `CAPABILITY_PROVIDER`, D2/D3).
  **P-2** quedó obsoleto bajo Opción B; en la práctica `emvy info` sólo necesita discovery, así
  que `_emvy_session` abre **un solo `DeviceLink`**. Los verbos operativos (Fases 3-4) abrirán
  un `EmvyLink` **tras** este mismo gate (el firmware sigue en el dialecto histórico; la
  migración FW-1…FW-3 no se ha ejecutado).
- **Próximos pasos exactos para la siguiente sesión:**
  1. **(Campo, cuando haya placa + tarjeta)** capturar las transcripciones literales @115200
     de los 12 verbos a `tests/fixtures/emvy/*.txt` (Fase 0, tareas 1–2), pendientes por hardware.
  2. **Fase 3** — `emvy read` (SCAN → `stream_until("JSON_END")` → tabla PAN/expiry/AID/…) y
     `emvy apdu` (secuencia obligatoria `WAIT → APDU: → RELEASE`, D4). Aquí entra `EmvyLink`
     abierto tras el gate de `_emvy_session` (decidir si el helper gana un modo «operativo»
     que cierre el `DeviceLink` y abra el `EmvyLink`, o si cada subcomando lo abre aparte).
  3. Si se arranca la migración FW (Opción B): bump de `BOMBERCAT_FW_VERSION` + feature-detect
     por `info`, luego FW-1…FW-3 (§7).

### Sesión 3 — 2026-09-21

- **Fase en curso:** **Fase 1 — Cliente serial del dialecto (`EmvyLink`) → COMPLETADA**
  (código + tests host verdes, sin hardware). El usuario confirmó reconocimiento en campo:
  `bombercat status` devuelve `EMVyBomberCat` v`1.1.0.1`, `detected: handshake (certain)`,
  `capabilities: identify, monitor, passthrough` (baseline de la tarea 1 de Fase 0 ✅ vía
  usuario; captura literal de los 12 verbos a `tests/fixtures/emvy/*.txt` **sigue
  pendiente** por requerir monitor serie con tarjeta — no bloquea Fase 1).
- **Entregado en esta sesión (Fase 1, ruta Opción A/puente `EmvyLink`, tal como pidió el
  usuario):**
  - ✅ **`modules/emvy/link.py`** — clase `EmvyLink` (composición sobre `serial.Serial` vía
    `core/usb_connection.open_serial`/`DEFAULT_BAUDRATE`/`DEFAULT_TIMEOUT`). Mismo ciclo de
    vida que `DeviceLink` (`open`/`close`/context-manager, settle 0.3 s + `reset_input_buffer`),
    hook de trace `tx`/`rx` para `-v`, y cap `_MAX_LINE_BYTES=4096` en `readline` (misma
    defensa M14 que `DeviceLink`). Métodos:
    - `send(line)` — escritura cruda `\n`-terminada (usada antes de un stream y por `exchange`).
    - `exchange(line, terminators=DEFAULT_TERMINATORS, timeout)` — envía y acumula líneas
      hasta que una hace *prefix-match* con un terminador (`RESP:`/`ERR:`/`OK`/`READY:`/
      `NFCINFO:` por defecto; el llamador pasa los suyos p.ej. `("TAG:",)` / `("EMU:SCANNED",)`).
      Salta ticks de timeout vacíos; `EmvyError` al agotar deadline. Devuelve todas las líneas
      (incluidos logs `#` y el terminador).
    - `stream_until(sentinel, on_line, stop_cmd, timeout)` — lee el stream llamando `on_line`
      por línea hasta que una empiece por `sentinel` (`JSON_END` para SCAN, `EMU:DONE` para
      emulación); en **Ctrl-C** envía `stop_cmd` (best-effort) y re-lanza `KeyboardInterrupt`
      (D5); `timeout=None` = espera abierta.
  - ✅ **`modules/emvy/parser.py`** — `EmvyError` + parsers puros:
    `parse_apdu_resp(lines)->bytes`, `parse_scan_json(lines)->dict`
    (bloque `JSON_START/END`), `parse_tag(line)->{proto,uid}`,
    `parse_cardscan(line)->{aid,pan,exp,t2}`, `parse_emu_event(line)->{kind,raw,fields|hex}`.
    **Desvío deliberado del plan:** los parsers *lanzan* `EmvyError` en las ramas `ERR:*`
    (`NOCARD`/`BADAPDU`/`TXFAIL`/`SCANFAIL`) en vez de devolver `bytes|Error`, para que la
    capa de comandos envuelva cada operación en un solo `try/except → print_error` + exit≠0
    (forma que ya usan los demás módulos). Documentado en el header de `parser.py`.
  - ✅ **`tests/test_emvy_link.py`** (13) + **`tests/test_emvy_parser.py`** (21) — **34/34
    verdes** con el `FakeSerial` de `conftest.py` (patrón `linked` calcado de
    `test_core_protocol.py`). Cubren: `RESP:` OK, corte en primer terminador, `WAIT`→`READY:`,
    `ERR:*`, terminadores custom (`TAG`), **timeout sin terminador**, link cerrado; y en
    streaming: corte en `JSON_END`/`EMU:DONE`, `on_line`, **Ctrl-C → envía `STOP` + re-lanza**,
    timeout de sentinela; más ciclo de vida por context-manager.
- **Estado de la suite completa:** `pytest` → **951 passed, 1 failed**. El único fallo es
  **ajeno a Fase 1**: `test_cli_setup_env::test_the_sudo_hint_from_a_checkout_is_runnable`
  compara `sudo {sys.executable}`; al correr desde un venv temporal de ruta muy larga, Rich
  la parte en varias líneas y el match textual falla (artefacto de entorno, no regresión).
- **Criterios de éxito de Fase 1 — cumplidos:** unit tests verdes; `EmvyLink.exchange("APDU:…")`
  → `RESP:` parseable a bytes; `stream_until` corta en `EMU:DONE`; ramas de error
  `NOCARD/BADAPDU/TXFAIL` cubiertas.
- **Nota sobre Opción B (Sesión 2):** el equipo decidió migrar el firmware al framing canónico
  y dejar `EmvyLink` como *puente transitorio* (D7). Esta sesión implementó el puente porque el
  usuario pidió explícitamente «Fase 1 — Cliente serial del dialecto (EmvyLink)». `EmvyLink`
  queda listo tanto para la ruta híbrida como para servir de fallback durante la migración FW.
- **Próximos pasos exactos para la siguiente sesión:**
  1. **(Campo, cuando haya placa + tarjeta)** capturar las transcripciones literales @115200
     de los 12 verbos a `tests/fixtures/emvy/*.txt` (Fase 0, tareas 1–2) para blindar los
     tests contra la salida real y fijar la línea base pre-migración.
  2. **Fase 2** — grupo `bombercat emvy` + gating por identidad (`fw_name == "emvybombercat"`,
     **sin** auto-flash, D2), `emvy info`, y registro en `core/cli.py` (P-2 quedó obsoleto bajo
     Opción B; P-1 = gate por id sigue como recomendación).
  3. Si se arranca la migración FW (Opción B): bump de `BOMBERCAT_FW_VERSION` + feature-detect
     por `info`, luego FW-1…FW-3 (§7).

### Sesión 2 — 2026-09-21

- **Fase en curso:** Fase 0 en ejecución (esqueleto + fixtures hechos; captura de campo
  pendiente por hardware). Luego arranca Fase 1 bajo la ruta **Opción B**.
- **Fase 0 — ejecutado en esta sesión (sin hardware):**
  - ✅ **Tarea 4:** esqueleto `modules/emvy/__init__.py` (vacío) + `modules/emvy/cli.py`
    (`@click.group("emvy")` vacío, help puesto). Verificado que importa; **NO** registrado
    aún en `core/cli.py` (correcto para Fase 0).
  - ✅ **Tarea 3:** carpeta `tests/fixtures/emvy/` creada con `README.md` que fija la
    convención de nombres y el guion de captura de los 12 verbos (con/sin tarjeta).
  - ⏳ **Tareas 1–2 (BLOQUEADAS por hardware):** correr `bombercat status`/`device list`/
    `identify` contra la placa y capturar las transcripciones seriales @115200 reales.
    Requieren una BomberCat con EMVyBomberCat flasheado + tarjeta EMV de prueba + monitor
    serie. Pendientes para una sesión con la placa conectada; volcar según el README.
- **Decisión arquitectónica RESUELTA:** el conflicto de §4 se cierra a favor de la
  **Opción B — Migrar el firmware al framing canónico** (`+OK`/`-ERR`/`:key value`). Se
  descarta la ruta híbrida (Opción A + Fase 5 opcional) que el doc recomendaba. Motivo:
  hacer de EMVyBomberCat un ciudadano de primera clase del contrato serial y que el módulo
  host reuse `DeviceLink.command()`/`device_session` sin un cliente serial paralelo. Nota
  de decisión añadida en §4.
- **Impacto en la hoja de ruta (reordenamiento):**
  - El trabajo antes numerado **Fase 5 + §7 (FW-1…FW-3)** deja de ser opcional/estratégico
    y pasa a ser el **workstream fundacional** de la integración.
  - **P-3 resuelto:** sí se ejecuta la migración de framing.
  - `EmvyLink` (Opción A) ya **no** es el destino; a lo sumo queda como *puente transitorio*
    para retrocompat con el consumidor externo `emvy/readers/bombercat.py` (D7), a deprecar.
  - **P-1** (capacidades vs. gate por id): sigue vigente, ahora en el marco de FW canónico;
    recomendación previa (gate por id, sin `CAPABILITY_PROVIDER`, D2) se mantiene.
  - **P-2** (uno o dos objetos de link) queda **obsoleto** bajo Opción B: se usa un único
    `DeviceLink` para discovery y operativo una vez migrado el firmware.
- **Regla de oro reafirmada:** la migración reencuadra **solo** la capa operativa
  (`emvyCommand()`, `.ino:3462+`); NO se tocan los pines fijos (`IRQ=11 VEN=13 ADDR=0x28`)
  ni el Discovery Contract ya consumido (`ping`/`info`/`identify`).
- **Próximos pasos exactos para la siguiente sesión (Fase 1 · Opción B):**
  1. Completar antes las tareas de campo pendientes de Fase 0 (baseline + captura de
     transcripciones seriales @115200 de los 12 verbos del §2.3, con y sin tarjeta, a
     `tests/fixtures/emvy/`). Estas transcripciones son la línea base *pre-migración* para
     verificar la retrocompat del puente `EmvyLink`.
  2. Introducir el **bump de `BOMBERCAT_FW_VERSION`** y el feature-detect por `info`
     (el host distingue la variante de framing) — prerequisito de FW-1…FW-3 (D7).
  3. Implementar **FW-1** en `emvyCommand()` (respuestas puntuales al framing canónico):
     `WAIT`→`+OK ready`/`-ERR nocard`; `APDU:`→`:resp <hex>`+`+OK`/`-ERR <reason>`;
     `RELEASE`/`MAG:`/`STOP`→`+OK`; `TAG`→`:proto`/`:uid`+`+OK`; `CARDSCAN`→`:aid :pan
     :exp :t2`+`+OK`; `NFCINFO`→`:fwver`+`+OK`/`-ERR chip`.
  4. Implementar **FW-2** (`SCAN`: mantener JSON, terminar con `+OK`) y **FW-3**
     (streaming `EMU:*` como progreso + terminador claro `EMU:DONE …`→`:sent <n>`+`+OK`).
  5. Re-flashear una placa y re-probar los 12 verbos contra el nuevo framing; verificar
     que el Discovery Contract sigue intacto (`bombercat status`/`identify`).
  6. Abrir la entrada «Sesión 3» y marcar el estado de FW-1…FW-3.
- **Bloqueadores / riesgos:**
  - Re-flasheo de placas de campo y re-prueba de cada verbo (riesgo medio/alto, §4/§7).
  - Retrocompat del consumidor externo `emvy/readers/bombercat.py` (D7): debe migrar o
    apoyarse en el puente `EmvyLink` durante la transición.
  - `arduino-cli` requerido (sketch built-from-source; sin uf2 prebuilt, D2/§5).

### Sesión 1 — 2026-09-21

- **Fase en curso:** Fase 0 (baseline) — **plan creado**, aún sin ejecutar tareas de campo.
- **Tareas completadas en esta sesión:**
  - Análisis completo del firmware `EMVyBomberCat.ino` (3791 líneas): identificadas las
    dos superficies de control (Discovery Contract vs dialecto operativo) y los 12 verbos
    operativos con sus respuestas (§2.3).
  - Análisis del patrón CLI (`firmwares.py`, `bombercat.py`, `requirements.py`,
    `ensure_firmware.py`, `detection_cli.py`, `readers/tags/magspoof cli.py`).
  - Confirmado que EMVyBomberCat **ya** está registrado en `firmwares.py` y reconocido por
    `status`/`device list`/`identify`.
  - Identificada la divergencia central (§4): el dialecto operativo no usa el framing
    `+OK/-ERR` que `DeviceLink.command()` exige → hace falta `EmvyLink` (Opción A) o
    migrar el firmware (Opción B).
  - Documentadas 7 divergencias (§5) y el bloqueo de auto-flash por ser built-from-source.
  - Redactado este plan de 6 fases + cambios de firmware.
- **Bloqueadores / decisiones pendientes:**
  - **P-1:** ¿capacidades nuevas o gate por id de firmware? (recomendación: gate por id).
  - **P-2:** ¿`EmvyLink` único con `ping` propio, o `DeviceLink`+`EmvyLink` separados?
    (recomendación: `EmvyLink` único).
  - **P-3:** ¿se ejecutará la Fase 5 (migración de framing) o se para en Fase 4?
    (decisión del equipo; no bloquea Fases 1–4).
- **Próximos pasos exactos para la siguiente sesión (Fase 0):**
  1. Con la placa conectada, correr `bombercat status` / `device list` / `identify` y
     pegar la salida real en una nota.
  2. Capturar transcripciones seriales @115200 de cada verbo del §2.3 (con y sin tarjeta)
     y guardarlas en `tests/fixtures/emvy/` (crear la carpeta).
  3. Crear el esqueleto `modules/emvy/__init__.py` y `modules/emvy/cli.py` (vacío, solo
     el `@click.group("emvy")`), sin registrarlo aún en `core/cli.py`.
  4. Al terminar, abrir la entrada «Sesión 2» y marcar Fase 0 completa → arrancar Fase 1.
- **Notas adicionales:**
  - Las líneas `# …` del firmware son *log noise* (ignoradas por el protocolo de líneas);
    útiles para el usuario en `-v`, no para parseo.
  - `WAIT→APDU→RELEASE` es una **secuencia obligatoria** (estado de flanco); nunca exponer
    `APDU:` suelto (D4).
  - `EMU:`/`EMUEMV:` son asíncronos: stremear hasta `EMU:DONE`, Ctrl-C debe mandar `STOP` (D5).
  - `REBOOT` re-enumera el USB: el link queda muerto tras enviarlo (D6).

<!-- Plantilla para la próxima entrada (copiar y rellenar):

### Sesión N — YYYY-MM-DD
- **Fase en curso:**
- **Tareas completadas en esta sesión:**
- **Bloqueadores / decisiones pendientes:**
- **Próximos pasos exactos para la siguiente sesión:**
- **Notas adicionales:**
-->
