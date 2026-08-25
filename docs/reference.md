# Command reference

Complete reference for every `bombercat` command and subcommand: purpose, flags,
examples and expected output.

**Control plane only.** Every command that talks to a board does so over the
USB-serial control protocol. No APDUs travel over serial; relayed APDUs go over
WiFi/TCP to the `nfcgate-server`. `capture` streams a *copy* of them over the
control link — see [Capture / Wireshark](capture.md).

- [Command reference](#command-reference)
  - [Invocation](#invocation)
  - [Global options](#global-options)
  - [Device selection: `-d` / `-p`](#device-selection--d---p)
  - [`device`](#device)
    - [`device list`](#device-list)
    - [`device info`](#device-info)
  - [`identify`](#identify)
  - [`status`](#status)
  - [Firmwares](#firmwares)
  - [`flash`](#flash)
    - [`flash --list`](#flash---list)
    - [Naming the image](#naming-the-image)
    - [Tab completion](#tab-completion)
    - [Flashing](#flashing)
    - [The release cache](#the-release-cache)
  - [`relay`](#relay)
    - [`relay config`](#relay-config)
      - [`relay config wifi`](#relay-config-wifi)
      - [`relay config nfcgate`](#relay-config-nfcgate)
      - [`relay config show`](#relay-config-show)
    - [`relay run`](#relay-run)
    - [`relay stop`](#relay-stop)
    - [`relay status`](#relay-status)
    - [`relay monitor`](#relay-monitor)
  - [`capture`](#capture)
    - [`capture start`](#capture-start)
    - [`capture stop`](#capture-stop)
  - [`tags`](#tags)
    - [`tags read`](#tags-read)
    - [`tags watch`](#tags-watch)
    - [`tags scan`](#tags-scan)
    - [`tags info`](#tags-info)
  - [`readers`](#readers)
    - [`readers read`](#readers-read)
    - [`readers watch`](#readers-watch)
    - [`readers scan`](#readers-scan)
    - [`readers info`](#readers-info)
  - [Dev tooling](#dev-tooling)
    - [`proto`](#proto)
      - [`proto gen`](#proto-gen)
    - [`testserver`](#testserver)
      - [`testserver run`](#testserver-run)
        - [Requirements](#requirements)
      - [`testserver smoke`](#testserver-smoke)
  - [`completion`](#completion)
    - [`completion install`](#completion-install)
  - [Environment variables](#environment-variables)
  - [Exit codes](#exit-codes)

---

## Invocation

Run from the `tools/` directory:

```sh
python3 bombercat.py <command> [options]
```

The docs write it as `bombercat` for brevity. To get that short form, either add
a shell alias:

```sh
alias bombercat='python3 /abs/path/to/tools/bombercat.py'
```

or install [shell completion](#completion), which also lets you run
`python bombercat.py <TAB>`.

Every command and group accepts `-h` / `--help`.

## Global options

Placed before the command (`bombercat -v device list`):

| Option | Description |
|---|---|
| `-v`, `--verbose` | Raise the log level to INFO (shows the `rich` logger's info lines; off by default, which is WARNING). Repeatable (`-vv`). |
| `-h`, `--help` | Show help and exit. |

`-v` is also accepted **after** the command (`bombercat device list -v`) wherever a
subcommand lists it in its own `Options:` — both positions mean the same thing,
and the higher count wins if you somehow pass both. On the [`tags`](#tags)
commands specifically, `-v` does more than raise the log level: it traces the
raw `>`/`<` wire protocol to stderr (tx cyan, rx dim), and `-vv` adds an
elapsed-time stamp and byte count ahead of each line:

```sh
bombercat tags read -v
```

```
> ping
< +OK bombercat
```

```sh
bombercat tags read -vv
```

```
> [   0.000s] (   4B) ping
< [   0.017s] (  13B) +OK bombercat
```

Traced output always goes to **stderr**, so `--json | jq` keeps working with
`-v` on — stdout carries only the JSON. `set pass …` lines are redacted at
any level, since that's the WiFi/relay secret in plain text. Other commands
don't wire a tracer yet, so `-v` there is log-level only.

Every run prints the ASCII header panel with the CLI version and a random tagline
before the command output. (The header is suppressed while generating shell
completion.)

<a id="device-selection"></a>
## Device selection: `-d` / `-p`

Every command that talks to a board takes the same two mutually-exclusive
selectors:

| Option | Description |
|---|---|
| `-p`, `--port PATH` | Raw serial port (`/dev/ttyACM0`, `COM3`). Used as-is; no enumeration. |
| `-d`, `--device ID` | Stable device ID from `bombercat device list` (for multiple boards). |

Resolution rules (implemented in `resolve_port`, see [protocol](protocol.md#port-discovery--numbering)):

- `--port` wins and is used verbatim.
- `--device` selects one of the numbered devices without handshaking the others.
- With **neither** given and exactly **one** BomberCat attached, it is
  auto-detected by handshake.
- Zero or several attached and no selector → a clean error telling you to pass
  `-d`/`--port`.

`-d` and `-p` are mutually exclusive; passing both is an error.

---

## `device`

> Discover and inspect BomberCat devices over USB-serial.

### `device list`

List serial ports, the device ID of each BomberCat and who answered the
handshake.

| Option | Description |
|---|---|
| `-a`, `--all` | Include non-candidate ports (built-in UARTs, Bluetooth, `ttyS*`). |

```sh
bombercat device list
```

Expected output (two boards attached):

```
                             Serial ports
┏━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┓
┃ ID ┃ Port         ┃ BomberCat ┃ Serial#          ┃ HWID                ┃
┡━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━┩
│ #1 │ /dev/ttyACM1 │ ✓         │ 36A864E62A367EA3 │ USB VID:PID=…       │
│ #2 │ /dev/ttyACM0 │ ✓         │ E6614C775B4F2A21 │ USB VID:PID=…       │
└────┴──────────────┴───────────┴──────────────────┴─────────────────────┘
Target one with:  bombercat <command> -d <ID>   (e.g. bombercat relay config show -d 1)
```

The **BomberCat** column:

- `✓` — the port answered the control handshake (it is running the relay firmware).
- `USB id` — its USB VID/PID says BomberCat, but it did **not** answer the
  handshake (probably not running the NFCGate relay firmware — see
  [Troubleshooting](troubleshooting.md#board-present-by-usb-id-but-no-handshake)).
- blank — not a BomberCat candidate.

IDs are derived from a **stable USB identity** (serial number first, then USB
port location), so a board keeps its number across replugs and reboots as long as
the same set of boards is attached — not from the OS `/dev/ttyACM*` order, which
is non-deterministic. Numbering never opens a port (opening one can reset the
MCU), so `device list` is cheap; only the `✓` column costs a handshake.

If no attached port carries a BomberCat USB VID/PID, every candidate port is
numbered instead and the table says so — verify the IDs before trusting `-d`.

### `device info`

Handshake with one board and show its firmware and config.

Takes the [device selectors](#device-selection).

```sh
bombercat device info            # single board, auto-detected
bombercat device info -d 2       # a specific board
```

Expected output:

```
        BomberCat @ /dev/ttyACM0
┏━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Field   ┃ Value                     ┃
┡━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ fw      │ 0.9.7                      │
│ role    │ reader                     │
│ ssid    │ MyNet                      │
│ server  │ 192.168.1.5                │
│ port    │ 5566                       │
│ session │ 42                         │
│ state   │ idle                       │
└─────────┴────────────────────────────┘
```

(`config show` prints the same table.)

---

## `identify`

> Blink a device's LED so you can tell which board an ID refers to.

Takes the [device selectors](#device-selection). Requires firmware ≥ 0.7.0; on
older firmware it reports that the board predates `identify`.

```sh
bombercat identify -d 1          # blink board #1's LED for a couple of seconds
```

```
✓ /dev/ttyACM1 is blinking its LED for a couple of seconds
```

---

## `status`

> Report which firmware is flashed on a BomberCat, and what it can do.

Takes the [device selectors](#device-selection), plus `--no-sniff`. Unlike the
relay commands, `status` does **not** require the NFCGate REPL — it identifies a
board by levels of confidence so it works for any of the firmwares below:

1. **handshake (certain)** — the board answers the control REPL *and* names
   itself: `info` reports `fw_name` and the version. Six of the nine images do.
2. **handshake, name inferred (likely)** — the REPL answers but sends no
   `fw_name`, which only happens on an image built before that field existed.
   Back then NFCGate was the only firmware with a REPL, so that is the name —
   reported as an inference, never as certainty. Reflash to make it certain.
3. **boot banner (likely)** — the REPL stayed silent, but a known boot-output
   string matched. Best-effort and reset-sensitive; disable with `--no-sniff`.
   Output matching *two* firmwares names neither of them.
4. **USB id only (firmware unknown)** — a BomberCat is present by USB VID/PID but
   nothing identified it. Reported honestly, not guessed.

| Option | Description |
|---|---|
| `--no-sniff` | Skip the boot-banner sniff (use handshake + USB id only). |

```sh
bombercat status
```

```
                  Firmware @ /dev/ttyACM0
┌──────────────┬───────────────────────────────────────────┐
│ name         │ NFCGate                                   │
│ version      │ 0.9.7                                     │
│ detected     │ handshake (certain)                       │
│ capabilities │ capture, config, identify, monitor, relay │
└──────────────┴───────────────────────────────────────────┘
ℹ Next:
  bombercat relay status    — live relay state
  bombercat relay run       — start the relay
```

`status` ends by suggesting the commands the detected firmware actually
supports. An unidentified board is pointed at [`flash`](#flash), never at the
relay controls it cannot serve. Exit code is `1` only when **nothing** responds
on the port.

> The previous `bombercat status` (relay state) is now
> [`bombercat relay status`](#relay-status).

## Firmwares

`status` recognises the images published by
[bombercat-firmware](https://github.com/ElectronicCats/bombercat-firmware) (the
same set [`flash`](#flash) can write). **NFCGate** carries the full control REPL
(`SerialControl`); five more embed the small `BomberCatControl` REPL, enough to
answer `ping`/`info`/`identify` and name themselves. The remaining three have no
REPL at all and are best-effort only (USB presence + optional boot banner).

| Firmware | Control REPL | Host capabilities |
|---|---|---|
| **NFCGate** | ✅ handshake | relay, config, monitor, identify, capture |
| DetectTags | ✅ handshake | monitor, identify, tags ([`tags read`/`watch`/`scan`/`info`](#tags)) |
| DetectReaders | ✅ handshake | monitor, identify, readers ([`readers read`/`watch`/`scan`/`info`](#readers)) |
| magspoof | ✅ handshake | monitor, identify |
| MagspoofCVSAttack | ✅ handshake | monitor, identify |
| MagSpoofMqtt | ✅ handshake | monitor, identify |
| WiFiWebServer | ✅ handshake | identify (browser UI; nothing else on serial) |
| host_Relay_NFC | ❌ banner only | monitor |
| client_Relay_NFC | ❌ banner only | monitor |
| ESP32SerialPassthroughFlash | ❌ | passthrough |

> The REPL arrived with the firmware refactor that also added `fw_name`. Every
> `.uf2` published **before** it — which includes the images `flash` downloads
> today — answers only in NFCGate's case, so most boards in the wild land on
> level 2 or 3 above until they are reflashed from a newer release.

---

<a id="flash"></a>
## `flash`

> Download a prebuilt `.uf2` from the firmware releases and write it to a board.

```sh
bombercat flash --list                # what is available
bombercat flash NFCGate               # download (if needed) and flash
bombercat flash NFCGate -d 2          # pick a board by ID
bombercat flash ./build/mine.uf2      # flash a local image
```

No Arduino toolchain: the images come already compiled from the
[bombercat-firmware](https://github.com/ElectronicCats/bombercat-firmware)
releases and are copied to the board's UF2 bootloader drive. To build from
source instead, use [`flash_bombercat.sh`](https://github.com/ElectronicCats/bombercat-firmware/blob/main/flash_bombercat.sh)
in the firmware repo (`bash` + `arduino-cli`).

| Option | Description |
|---|---|
| `-l`, `--list` | List the firmwares in the cached release and exit. |
| `--refresh` | Re-check GitHub for a newer release right now (the cache otherwise revalidates once a day). |
| `--full` | Print descriptions in full instead of clipping them to the column. |
| `-y`, `--yes` | Don't ask for confirmation before writing. |
| `-p` / `-d` | [Device selectors](#device-selection), with the exception below. |

**`flash` does not use the handshake to find the board.** The other selectors
resolve a board by making it answer the control REPL, which only NFCGate does —
that would make it impossible to flash a board currently running any of the
other eight images. Here a single attached BomberCat is picked by USB identity
alone; several attached still require `-d`/`-p`, and a board already sitting in
bootloader mode needs no port at all.

### `flash --list`

```sh
bombercat flash --list
```

```
                                Firmware images — v1.0.1.0-beta
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Firmware                    ┃   Size ┃ Description                                               ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ DetectTags                  │ 198 KB │ NFC tag reader for the BomberCat's onboard PN7150. It co… │
│ ESP32SerialPassthroughFlash │ 176 KB │ Utility firmware that turns the RP2040 into a transparen… │
│ MagSpoofMqtt                │ 201 KB │ Networked MagSpoof firmware that receives magnetic-strip… │
│ MagspoofCVSAttack           │ 250 KB │ MagSpoof variant that replays a sequence of magnetic-str… │
│ NFCGate                     │ 267 KB │ Role-selectable NFCGate relay endpoint built on the Bomb… │
│ WiFiWebServer               │ 478 KB │ All-in-one firmware that turns the BomberCat into a self… │
│ client_Relay_NFC            │ 258 KB │ Legacy MQTT-based NFC relay endpoint, CARD/HCE side. It … │
│ host_Relay_NFC              │ 260 KB │ Legacy MQTT-based NFC relay endpoint, READER side. It co… │
│ magspoof                    │ 179 KB │ Basic MagSpoof magnetic-stripe emulator for BomberCat. I… │
└─────────────────────────────┴────────┴───────────────────────────────────────────────────────────┘
ℹ Flash one with:  bombercat flash DetectTags
```

Descriptions are clipped to one line; `--full` prints the whole paragraph. What
each image is good for on the host side is in [Firmwares](#firmwares).

### Naming the image

`FIRMWARE` is resolved in this order, first match wins:

1. **An existing path** — `./build/NFCGate.uf2`. Used as-is, with no cache and
   no network involved.
2. **The exact asset name** — `NFCGate.uf2`.
3. **The name, case-insensitively** — `nfcgate`, `NFCGate`.
4. **A unique substring** — `magspoofc` → `MagspoofCVSAttack.uf2`. Several
   matches is an error listing the candidates; it never guesses.

### Tab completion

With [`completion`](#completion) installed, `FIRMWARE` completes by name:

```
$ bombercat flash <TAB>
DetectTags                   MagspoofCVSAttack            host_Relay_NFC
ESP32SerialPassthroughFlash  NFCGate                      magspoof
MagSpoofMqtt                 WiFiWebServer                client_Relay_NFC

$ bombercat flash magspoof<TAB>
MagSpoofMqtt  MagspoofCVSAttack  magspoof

$ bombercat flash ./build/<TAB>      # paths fall back to file completion
```

Matching is by substring, the same rule the resolver above uses, so whatever
completes is something `flash` can actually resolve. zsh and fish also show
each image's description next to its name.

The names come from the cached release; with an empty cache the nine known
images are offered anyway, so completion works on a fresh install. It only ever
reads the disk — pressing `<TAB>` never contacts GitHub and never fills the
cache.

### Flashing

```sh
bombercat flash DetectTags
```

```
ℹ About to flash DetectTags.uf2 (198 KB)
  Write it to /dev/ttyACM0? [y/N]: y
rebooting /dev/ttyACM0 into the UF2 bootloader (1200-bps touch)
waiting for the RPI-RP2 drive
copying DetectTags.uf2 (202240 bytes) to /media/you/RPI-RP2
waiting for the board to come back
✓ DetectTags.uf2 written to /media/you/RPI-RP2
ℹ The board came back on /dev/ttyACM0
```

Flashing is destructive, so it confirms first; `-y` skips the prompt. The
sequence is:

1. The image is validated as a UF2 for the RP2040 **before** anything reboots —
   a bad file must not leave the board stranded in bootloader mode.
2. If an `RPI-RP2` drive is already mounted, the board is in bootloader mode and
   steps 3–4 are skipped.
3. The board is rebooted into the bootloader with a 1200-bps touch, and the CLI
   waits for its serial port to disappear (≤ 5 s) and the drive to appear (≤ 15 s).
4. The `.uf2` is copied to the drive.
5. The board reboots itself into the new firmware; the CLI waits up to 15 s for
   a serial port to reappear and reports it.

Anything that goes wrong is a one-line `✗` plus exit code 1, except the board
not reaching bootloader mode, which prints a panel with the manual route
(double-tap RESET). See
[troubleshooting](troubleshooting.md#no-rpi-rp2-drive).

The configuration saved in the board's flash (WiFi, nfcgate parameters)
**survives** reflashing — it lives in a separate region the `.uf2` doesn't
overwrite.

### The release cache

Images are cached under `~/.bombercat/firmware/<tag>/`, alongside an
`index.json` recording the tag and the day it was last checked:

```
~/.bombercat/firmware/
├── index.json                  # {"tag": "v1.0.1.0-beta", "checked": "2026-08-21"}
└── v1.0.1.0-beta/
    ├── NFCGate.uf2
    ├── …
    ├── descriptions.json
    └── release.json
```

- **The network is only touched on demand**, never on startup: the first
  `--list`/`flash` that needs the cache fills it. A local `.uf2` never does.
- **Revalidation is daily**, or immediate with `--refresh`.
- Every asset is checked against the SHA256 the release publishes; downloads go
  to a staging directory and are moved into place only once complete, so an
  interrupted download can't leave a cache that looks valid.
- With a populated cache and no connectivity, `flash` warns that it could not
  reach GitHub and uses what's on disk. With an empty cache, it's an error.
- If the repo has no published release, that is said as such — the build
  workflow only attaches `.uf2` assets on `release: published`.

Redirect the cache with `BOMBERCAT_FIRMWARE_CACHE` and the source repo with
`BOMBERCAT_FIRMWARE_REPO` (useful against a fork); `GITHUB_TOKEN` is used for
the API if set, which raises the unauthenticated 60 requests/hour limit.

---

## `relay`

> NFCGate relay: configure it, run it, and watch the APDU relay.

The relay commands live under `bombercat relay …`. They need a board flashed
with the **NFCGate** firmware (the only one that answers the control REPL —
confirm with [`bombercat status`](#status)). The old root spellings (`config`,
`run`, `stop`, `monitor`) still work for one release as hidden aliases that warn
and forward here.

### `relay config`

> Configure the relay (WiFi + nfcgate parameters), persisted in flash.

All three subcommands take the [device selectors](#device-selection). The two
`config` setters also **blink the LED** of the board they just configured (a
non-fatal courtesy — a board on pre-0.7.0 firmware just earns a warning), so you
can match `-d 2` to a physical board on the desk.

#### `relay config wifi`

Set the WiFi credentials.

| Option | Description |
|---|---|
| `--ssid TEXT` | WiFi network name. **Required.** |
| `--password`, `--pass TEXT` | WiFi passphrase (empty for an open network). |
| `--save` / `--no-save` | Persist to flash (default: `--save`). `--no-save` applies for this session only, lost on reboot. |

```sh
bombercat relay config wifi --ssid MyNet --pass 's3cret'
```

```
✓ set ssid = MyNet
✓ set pass = ••••••
✓ saved to flash
ℹ /dev/ttyACM0 is blinking its LED — that's the board you just configured
```

#### `relay config nfcgate`

Set the `nfcgate-server`, session and role.

| Option | Description |
|---|---|
| `--server TEXT` | `nfcgate-server` as `host` or `host:port`. **Required.** |
| `--session INTEGER` | Session byte `1..255`; **both peers must match**. **Required.** |
| `--role [reader\|card]` | `reader` = read a physical card, `card` = emulate one to a terminal. **Required.** |
| `--save` / `--no-save` | Persist to flash (default: `--save`). |

`--server` may include the port (`host:port`); an out-of-range port is rejected
with a clean error. If omitted, the device keeps its stored port (default 5566).

```sh
bombercat relay config nfcgate --server 192.168.1.5:5566 --session 42 --role reader
```

```
✓ set server = 192.168.1.5
✓ set port = 5566
✓ set session = 42
✓ set role = reader
✓ saved to flash
```

#### `relay config show`

Show the device's current configuration (same table as `device info`).

```sh
bombercat relay config show -d 2
```

---

### `relay run`

> Start the relay (associate WiFi, connect the server, begin the session).

Takes the [device selectors](#device-selection).

`run` is **non-blocking on the device**: it only *accepts* the request and starts
the bring-up in the background. The CLI then polls `status` and reports progress
until the relay reaches `relaying` (success) or `error`, or a **45 s** budget
expires. A `-ERR` on acceptance means it could not even start (e.g. empty SSID,
already running).

```sh
bombercat relay run
```

Successful bring-up:

```
ℹ relay accepted 'run'; bringing up…
ℹ   … associating WiFi
ℹ   … connecting nfcgate-server
✓ relay started on /dev/ttyACM0
ℹ watch it with:  bombercat relay monitor   /   bombercat relay status
```

If it does not reach `relaying` in time the device is **not** wedged (the REPL
stayed live) — the CLI points you at the likely culprit (server not listening,
PN7150 not responding) and suggests `bombercat relay status` / `monitor`. See
[Troubleshooting](troubleshooting.md#run-times-out).

### `relay stop`

> Stop the relay.

Takes the [device selectors](#device-selection).

```sh
bombercat relay stop
```

```
✓ relay stopped on /dev/ttyACM0
```

### `relay status`

> Show live relay status (state, link, peer, relayed count).

Takes the [device selectors](#device-selection).

```sh
bombercat relay status -d 2
```

```
              Relay status @ /dev/ttyACM0
┏━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Field              ┃ Value                         ┃
┡━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ state              │ relaying                       │
│ link connected     │ yes                            │
│ peer present       │ yes                            │
│ APDU pairs relayed │ 7                              │
└────────────────────┴────────────────────────────────┘
```

`state` is one of `idle`, `connecting`, `relaying`, `error` (see the
[protocol](protocol.md#status-fields)).

### `relay monitor`

> Stream the device's serial output live (relay logs + APDU hex). Ctrl-C to quit.

Takes the [device selectors](#device-selection).

`monitor` is read-only — it does not disturb a running relay. On entry it raises
the firmware log level to Debug (so per-APDU hex dumps appear) and restores it to
Warn on exit. Lines are colorized: APDU hex (`cmd:`/`resp:`) in cyan, errors in
red, protocol markers dimmed.

```sh
bombercat relay monitor -d 1
```

```
ℹ Monitoring /dev/ttyACM1 — press Ctrl-C to stop
reader: vivo, peer presente, esperando comando del peer
R<- cmd: 0x00 0xA4 0x04 0x00 0x0E 0x32 0x50 0x41 0x59 …
reader: tarjeta activada
…
```

---

## `capture`

> Capture relayed APDUs to pcap (live Wireshark and/or a file).

Full behavior and the pcap details are in [Capture / Wireshark](capture.md); this
is the flag reference.

### `capture start`

Arm the device tap and stream APDUs to Wireshark and/or a file until Ctrl-C.
Takes the [device selectors](#device-selection). Requires firmware ≥ 0.8.0 (the
`capture` control command); the live feed also needs Wireshark installed.

| Option | Description |
|---|---|
| `-o`, `--output FILE` | Also write a `.pcap` file (classic pcap, opens in Wireshark). |
| `-ws`, `--wireshark` / `-nws`, `--no-wireshark` | Launch Wireshark on a live FIFO (opt-in; default off). |
| `--profile TEXT` | Wireshark configuration profile to launch with (`-C`). |

At least one of `-ws` / `-o` is required, otherwise there is nothing to do.

```sh
bombercat capture start -ws                # live Wireshark only
bombercat capture start -ws -o emv.pcap    # live Wireshark + file
bombercat capture start -o emv.pcap        # file only
```

```
ℹ waiting for Wireshark to attach…
✓ Wireshark attached — streaming APDUs
ℹ capturing from /dev/ttyACM0 — press Ctrl-C to stop
→ card       1234 ms  00a404000e325041592e5359532e444446303100
← card       1290 ms  6f23840e325041592e5359532e4444463031a5119000
```

`→ card` is a command (terminal→card), `← card` a response (card→terminal). If
you quit Wireshark, capture continues to the file (or stops cleanly if there is
no file). The tap is disarmed automatically on exit.

### `capture stop`

Disarm the tap on a board (e.g. one left armed by an interrupted `start`).
Takes the [device selectors](#device-selection).

```sh
bombercat capture stop -d 1
```

```
✓ capture disarmed on /dev/ttyACM1
```

---

<a id="tags"></a>
## `tags`

> NFC tag detection over the **DetectTags** firmware's PN7150 reader.

The `tags` commands live under `bombercat tags …`. They need a board flashed
with **DetectTags** (confirm with [`bombercat status`](#status)) and, like
`relay`, verify the control handshake before doing anything — a board that
doesn't answer gets the same clean `-ERR`-style message the relay commands
give, naming `tags` specifically:

```
✗ /dev/ttyACM0 did not answer the handshake. `tags` needs the DetectTags
  firmware — check what's flashed with:  bombercat status
```

All four subcommands take the [device selectors](#device-selection) plus
their own `-v`/`--verbose` (see [Global options](#global-options) for what
`-v` does here specifically — it traces the wire protocol, not just the log
level).

**Structured vs. legacy events.** Every published `.uf2` today predates the
firmware's `:tag <ts_ms> <tech> <protocol> <uid_hex|-> [k=v …]` event line, so
`tags` parses the older human-readable `displayCardInfo()` text instead —
same information, slightly less of it (no UID at all for NFC-B/NFC-F, no
`extra` fields). The parser detects which one a board speaks on the fly and
switches permanently to structured mode the moment it sees a `:tag` line.
[`tags info`](#tags-info) tells you which mode a board is in.

<a id="tags-read"></a>
### `tags read`

> Wait for one tag and print its UID.

| Option | Description |
|---|---|
| `-t`, `--timeout SEC` | Seconds to wait for a tag (default `15`). |
| `--json` | Emit one JSON object on stdout instead of the field table. |

```sh
bombercat tags read
```

```
ℹ Waiting for a tag on /dev/ttyACM0 — Ctrl-C to abort

  Tag detected
  uid           04:1A:2B:3C
  technology    NFC-A
  protocol      T2T
  SAK           08
```

`--json` prints a single clean object on stdout (nothing else touches
stdout, so it's pipeable) with every field, `extra` merged in flat:

```sh
bombercat tags read --json
```

```json
{"uid": "041A2B3C", "tech": "NFC-A", "protocol": "T2T", "ts_ms": 1234, "SAK": "08"}
```

No tag within the timeout is exit code `1`:

```
✗ no tag detected in 15s
```

On a firmware in legacy mode with **NFC-B** or **NFC-F** presented, `uid` has
no value to print — the field table shows why instead of a blank:

```
  uid           unavailable (NFC-B: firmware prints no ID)
```

(`--json`'s `"uid"` is `null` in that case, not a placeholder string.)

<a id="tags-watch"></a>
### `tags watch`

> Stream tag detections continuously. Ctrl-C to stop and print a summary.

| Option | Description |
|---|---|
| `--dedupe` | Collapse repeat detections of the same UID into a `seen again (xN)` line instead of reprinting the row. |
| `--quiet-noise` / `--no-quiet-noise` | Hide firmware boot/idle chatter — `Restarting…`, `Waiting for a Card…`, `Card removed!` (default: hidden). |
| `--json` | Emit one JSON object per line instead of the formatted line. |

```sh
bombercat tags watch --dedupe
```

```
ℹ Watching /dev/ttyACM0 — Ctrl-C to stop
[12:26:48] NFC-A   T2T        04:1A:2B:3C
  ↳ 04:1A:2B:3C seen again (x2)
[12:26:48] NFC-B   ISODEP     unavailable (NFC-B: firmware prints no ID)

ℹ 3 detections, 2 unique UIDs, 41s
```

Without `--dedupe`, a repeat detection just prints another line. With
`-v`/`--verbose`, the boot/idle noise always shows regardless of
`--quiet-noise` — that flag only controls the *default* (non-verbose) view.
`--json` emits one object per detection and skips the noise lines and the
closing summary, so the stream stays valid NDJSON.

<a id="tags-scan"></a>
### `tags scan`

> Sample tag detections for a while and print an aggregated summary.

Repeat detections of the same UID collapse into one row with a count and a
first/last time seen (elapsed seconds since the scan started), instead of
scrolling past like `watch` does.

| Option | Description |
|---|---|
| `-t`, `--timeout SEC` | Seconds to sample for (default `30`). |
| `--json FILE` | Also write the aggregate as a JSON array to `FILE`. |
| `--csv FILE` | Also write the aggregate as CSV to `FILE` (base columns first, then any `extra` keys). |

```sh
bombercat tags scan -t 10
```

```
ℹ Scanning /dev/ttyACM0 for 10s — Ctrl-C to stop early

ℹ Scan @ /dev/ttyACM0 — 10s, 3 detections, 2 unique tags
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━┳━━━━━━━┳━━━━━━━┳━━━━━━┓
┃ UID                                ┃ Tech  ┃ Protocol ┃ Count ┃ First ┃ Last ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━╇━━━━━━━╇━━━━━━━╇━━━━━━┩
│ 04:1A:2B:3C                        │ NFC-A │ T2T      │     2 │  0.0s │ 4.1s │
│ unavailable (NFC-B: firmware       │ NFC-B │ ISODEP   │     1 │  6.7s │ 6.7s │
│ prints no ID)                      │       │          │       │       │      │
└────────────────────────────────────┴───────┴──────────┴───────┴───────┴──────┘
```

A transient progress bar (elapsed / timeout, live detection count) shows
while sampling and clears before the summary prints. An empty sample prints
`no tags detected` instead of an empty table; `--json`/`--csv` still get
written (an empty array / header-only file) so scripted runs don't have to
special-case a quiet scan. Ctrl-C ends the sample early and summarizes
whatever was seen so far — same as `watch`.

<a id="tags-info"></a>
### `tags info`

> Report what this DetectTags image can do — mainly, which event mode it
> speaks.

```sh
bombercat tags info
```

Structured firmware (has the `:tag` event line):

```
       DetectTags @ /dev/ttyACM0
┌─────────┬────────────────────────────┐
│ version │ 1.0.2                      │
│ events  │ structured (':tag' events) │
│ state   │ idle                       │
└─────────┴────────────────────────────┘
```

A published (pre-FW-1) image, still on legacy text:

```
                        DetectTags @ /dev/ttyACM0
┌─────────┬─────────────────────────────────────────────────────────────┐
│ version │ 0.9.0                                                       │
│ events  │ legacy text  (no ':tag' events — reflash for exact parsing) │
│ state   │ idle                                                        │
└─────────┴─────────────────────────────────────────────────────────────┘
```

`info` listens for up to 2s after the handshake to catch a `:tag` line if one
happens to arrive; it does not itself trigger a scan, so a board that's had
nothing presented to it since boot reports `legacy text` even on FW-1
firmware until something is actually tapped. Every `.uf2` published today
predates FW-1 and will therefore always report legacy mode — see
[Firmwares](#firmwares).

---

<a id="readers"></a>
## `readers`

> NFC reader/terminal detection over the **DetectReaders** firmware's PN7150
> emulated card.

The `readers` commands live under `bombercat readers …`. They need a board
flashed with **DetectReaders** (confirm with [`bombercat status`](#status))
and, like `tags`, verify the control handshake before doing anything:

```
✗ /dev/ttyACM0 did not answer the handshake. `readers` needs the
  DetectReaders firmware — check what's flashed with:  bombercat status
```

All four subcommands take the [device selectors](#device-selection) plus
their own `-v`/`--verbose` (see [Global options](#global-options)).

**How it works.** The board runs the PN7150 in card-emulation (LISTEN) mode,
presenting an emulated contactless card. Any reader/terminal that activates
the field is reported as one structured `:reader <ts_ms> <tech> <protocol>
[intf=…] [apdu=…] [aid=…] [label=…] [n=…]` event line — always structured,
there is no legacy text mode to fall back to. The first APDU the reader sends
is fingerprinted: a PPSE SELECT identifies an EMV payment terminal
(`emv-payment`), well-known AIDs identify payment apps (`visa`,
`mastercard`, `amex`) or NDEF readers (`ndef`); anything else is `unknown`,
with the selected AID surfaced separately when the command was a SELECT.

<a id="readers-read"></a>
### `readers read`

> Wait for one reader/terminal to probe the emulated card.

| Option | Description |
|---|---|
| `-t`, `--timeout SEC` | Seconds to wait for a reader (default `15`). |
| `--json` | Emit one JSON object on stdout instead of the field table. |

```sh
bombercat readers read
```

```
ℹ Waiting for a reader on /dev/ttyACM0 — Ctrl-C to abort

  Reader detected
  label         emv-payment
  technology    NFC-A
  protocol      ISODEP
  interface     ISODEP
  fingerprint   emv-payment
  apdu          00 A4 04 00 0E 32 50 41 59 2E 53 59 53 2E 44 44 46 30 31
  n             3
```

`--json` prints a single clean object on stdout with every field, `extra`
merged in flat:

```sh
bombercat readers read --json
```

```json
{"ts_ms": 1234, "tech": "NFC-A", "protocol": "ISODEP", "intf": "ISODEP", "apdu": "00A404000E325041592E5359532E4444463031", "aid": null, "label": "emv-payment", "n": 3}
```

No reader within the timeout is exit code `1`:

```
✗ no reader detected in 15s
```

<a id="readers-watch"></a>
### `readers watch`

> Stream reader detections continuously. Ctrl-C to stop and print a summary.

| Option | Description |
|---|---|
| `--dedupe` | Collapse repeat detections of the same fingerprint into a `seen again (xN)` line instead of reprinting the row. Two distinct terminals sharing a label (e.g. two EMV readers, both `emv-payment`) share a fingerprint too, so the counter can mix them. |
| `--quiet-noise` / `--no-quiet-noise` | Hide firmware boot/idle chatter — `Waiting for a Reader …`, `Re-arm: …`, `Re-armed. …` (default: hidden). |
| `--json` | Emit one JSON object per line instead of the formatted line. |

```sh
bombercat readers watch --dedupe
```

```
ℹ Watching /dev/ttyACM0 — Ctrl-C to stop
[12:26:48] emv-payment   NFC-A   ISODEP     00 A4 04 00 0E 32 50 41 59 2E 53 59 53 2E 44 44 46 30 31
  ↳ emv-payment seen again (x2)
[12:27:03] visa          NFC-A   ISODEP     00 A4 04 00 07 A0 00 00 00 03 10 10

ℹ 3 detections, 2 unique fingerprints, 41s
```

<a id="readers-scan"></a>
### `readers scan`

> Sample reader detections for a while and print an aggregated summary.

Repeat detections of the same fingerprint collapse into one row with a count
and a first/last time seen (elapsed seconds since the scan started).

| Option | Description |
|---|---|
| `-t`, `--timeout SEC` | Seconds to sample for (default `30`). |
| `--json-out FILE` | Also write the aggregate as a JSON array to `FILE`. |
| `--csv-out FILE` | Also write the aggregate as CSV to `FILE` (base columns first, then any `extra` keys). |
| `--force` | Overwrite `--json-out`/`--csv-out` if the file already exists. |

```sh
bombercat readers scan -t 30
```

```
ℹ Scanning /dev/ttyACM0 for 30s — Ctrl-C to stop early

ℹ Scan @ /dev/ttyACM0 — 30s, 3 detections, 2 unique readers
┏━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━┳━━━━━━━┳━━━━━━┓
┃ Label       ┃ Tech  ┃ Protocol ┃ Interface ┃ Count ┃ First ┃ Last ┃
┡━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━╇━━━━━━━╇━━━━━━┩
│ emv-payment │ NFC-A │ ISODEP   │ ISODEP    │     2 │  0.0s │ 4.1s │
│ visa        │ NFC-A │ ISODEP   │ ISODEP    │     1 │  6.7s │ 6.7s │
└─────────────┴───────┴──────────┴───────────┴───────┴───────┴──────┘
```

The full APDU is left out of the printed table (it can run to hundreds of
hex characters) but is still written to `--json-out`/`--csv-out`. A
transient progress bar shows while sampling and clears before the summary
prints. An empty sample prints `no readers detected` instead of an empty
table. Ctrl-C ends the sample early and summarizes whatever was seen so
far — same as `watch`.

<a id="readers-info"></a>
### `readers info`

> Report what this DetectReaders image can do.

```sh
bombercat readers info
```

```
       DetectReaders @ /dev/ttyACM0
┌─────────┬───────────────────────────────┐
│ version │ 1.0.0                         │
│ events  │ structured (':reader' events) │
│ state   │ listening                     │
└─────────┴───────────────────────────────┘
```

`info` listens for up to 2s after the handshake to catch a `:reader` line if
one happens to arrive; it does not itself arm anything extra, so a board
that hasn't had a reader presented to it since boot reports
`no ':reader' events seen yet` until something actually probes it.
`state` is `listening` while armed and `reader-detected` for the duration of
an active detection session.

---

## Dev tooling

These wrap the reproducible build/test scripts under `tools/` (docs/NFCGATE_PLAN.md
Fases 1–5). They do **not** talk to a board.

### `proto`

> Nanopb protobuf sources for the NFCGate relay.

#### `proto gen`

Regenerate `firmware/core/src/proto/*.pb.{c,h}` from the vendored `.proto` files.
Wraps `tools/gen_proto.sh`, which bootstraps a pinned venv on first run.

```sh
bombercat proto gen
```

```
ℹ Running gen_proto.sh (bootstraps a pinned venv on first run) …
✓ Protobuf sources regenerated.
```

### `testserver`

> Local nfcgate-server for relay testing (no hardware/RF).

See [`testserver/README.md`](../testserver/README.md) for the fixture itself.

#### `testserver run`

Build (if needed) and run the local `nfcgate-server` in Docker. Ctrl-C to stop.
Wraps `tools/testserver/run.sh`.

| Option | Description |
|---|---|
| `-p`, `--port INTEGER` | Host port to publish (default `5566`; the container always listens on 5566). |

```sh
bombercat testserver run          # publish on host :5566
bombercat testserver run -p 6000  # publish on host :6000
```

##### Requirements

`testserver run` shells out to `tools/testserver/run.sh`, which builds and runs
the pinned `nfcgate-server` in Docker. It is pure dev tooling: it never touches
USB, serial or RF, so no board has to be plugged in — but the host must have:

| Requirement | Why | Check |
|---|---|---|
| `bash` on `PATH` | the CLI launches the script as `bash run.sh` | `bash --version` |
| Docker installed, daemon running, usable by your user | `run.sh` does `docker build` + `docker run` | `docker run --rm hello-world` |
| Your user in the `docker` group (Linux) | otherwise the socket denies the build — add it with `sudo usermod -aG docker "$USER"` and re-login | `id -nG \| grep docker` |
| The server clone at `<repo>/server` | it *is* the Docker build context (`docker build … <repo>/server`) | `ls server/server.py` |
| Network access on the **first** run | the image pulls `python:3.11-slim` and installs `protobuf==3.20.3` | — |
| The host port free (default `5566`) | it is published as `-p <port>:5566` | `ss -ltn \| grep 5566` |

All of it is pre-checked before the build starts: the CLI verifies the server
clone, Docker, the daemon, socket permissions and the host port, and on failure
prints the fix to apply instead of a raw Docker error — see
[troubleshooting](troubleshooting.md#testserver-errors) for what each one says.
When the clone is missing and you are on a terminal, it offers to fetch it.

The server is a dev-only fixture — not committed, not a submodule — so fetch it
once (needs `git`), the same step [`testserver smoke`](#testserver-smoke) needs:

```sh
tools/testserver/fetch_server.sh                            # clones ElectronicCats/nfcgate-server@fc9103d
SERVER_REPO=/path/to/clone tools/testserver/fetch_server.sh # offline / mirror
```

Good to know:

- The container **always** listens on 5566; `-p/--port` only changes the *host*
  port (the CLI passes it to `run.sh` as [`PORT`](#environment-variables)).
- The image (`bombercat-nfcgate-server`) is rebuilt on every invocation, but
  Docker's layer cache makes that a no-op after the first build — only that first
  build needs the network.
- Ctrl-C stops and removes the container (`bombercat-nfcgate-server-run`); a
  leftover container from a crashed run is force-removed at the next start.
- Nothing here needs `protobuf` on the host: that is only for
  [`testserver smoke`](#testserver-smoke), which bootstraps its own venv.
- If the relay peers live on other machines (a phone running the NFCGate app, a
  BomberCat on the WLAN), the host firewall must allow inbound TCP on that port,
  and they must target the host's LAN address — not `127.0.0.1`.

Failure modes are listed in
[Troubleshooting](troubleshooting.md#testserver-errors).

#### `testserver smoke`

Run the relay smoke test against a running server (needs `protobuf==3.20.3`,
bootstrapped into a throwaway venv if the CLI's interpreter lacks it). Wraps
`tools/testserver/relay_smoketest.py`.

| Argument | Default | Description |
|---|---|---|
| `HOST` | `127.0.0.1` | Server host. |
| `PORT` | `5566` | Server port. |

```sh
bombercat testserver smoke                 # 127.0.0.1:5566
bombercat testserver smoke 192.168.1.5 5566
```

The server must have been fetched once with `tools/testserver/fetch_server.sh`
(the smoke test imports its committed `*_pb2.py`).

---

## `completion`

> Install shell tab completion for bombercat. (Linux/macOS only.)

### `completion install`

Install tab completion for your shell, then restart your shell (or source your rc
file).

| Option | Description |
|---|---|
| `--shell [bash\|zsh\|fish]` | Shell to install completion for (auto-detected from `$SHELL` if omitted). |

```sh
bombercat completion install            # auto-detect shell
bombercat completion install --shell zsh
```

It writes an absolute-path completion script (so completion works whether or not
`bombercat` is on `PATH`, including `python bombercat.py <TAB>`), and for zsh adds
an `fpath` entry to `~/.zshrc` if one isn't there already.

Besides commands and options, this completes firmware names for
[`flash`](#tab-completion).

---

## Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `BOMBERCAT_VID` / `BOMBERCAT_PID` | device discovery | Declare a custom USB VID/PID for a board re-flashed with a non-stock USB identity (hex `0x1209` or decimal). Both must be set to add the pair to the match list. |
| `BOMBERCAT_FIRMWARE_REPO` | [`flash`](#flash) | GitHub repo to pull firmware releases from (default `ElectronicCats/bombercat-firmware`). Point it at a fork to test against its releases. |
| `BOMBERCAT_FIRMWARE_CACHE` | [`flash`](#flash) | Where downloaded images live (default `~/.bombercat/firmware`). |
| `GITHUB_TOKEN` | [`flash`](#flash) | Sent as a bearer token to the GitHub API, raising the 60 req/h unauthenticated limit. Optional. |
| `BOMBERCAT_SMOKE_VENV` | `testserver smoke` | Path to the throwaway protobuf venv (default `tools/.venv-smoke`). |
| `SERVER_REPO` | `testserver/fetch_server.sh` | Use an existing server clone / mirror instead of cloning. |
| `PORT` | `testserver/run.sh` | Host port for the server (set for you by `testserver run -p`). |

## Exit codes

- `0` — success.
- `1` — a handled error: no board found, handshake failed, a `set`/`run`/`capture`
  rejected by the device, a missing file, etc. These are reported as a clean
  one-line message (with a leading `✗`), **never** a Python traceback. A stray
  traceback is a bug worth reporting.
