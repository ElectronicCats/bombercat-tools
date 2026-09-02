# `bombercat flash`

> Download a prebuilt `.uf2` from the firmware releases and write it to a board.

## Quick Start

```sh
bombercat flash --list                # what is available
bombercat flash NFCGate               # download (if needed) and flash
bombercat flash NFCGate -d 2          # pick a board by ID
bombercat flash ./build/mine.uf2      # flash a local image
```

No Arduino toolchain: images come already compiled from [bombercat-firmware](https://github.com/ElectronicCats/bombercat-firmware) releases and are copied to the board's UF2 bootloader drive. To build from source instead, use [`flash_bombercat.sh`](https://github.com/ElectronicCats/bombercat-firmware/blob/main/flash_bombercat.sh) in the firmware repo (`bash` + `arduino-cli`).

---

## Subcommands

### `flash`

> Download and flash a BomberCat firmware image.

| Option | Description |
|---|---|
| `-l, --list` | List the firmwares in the cached release and exit. |
| `--refresh` | Re-check GitHub for a newer release right now (cache otherwise revalidates once a day). |
| `--full` | Print descriptions in full instead of clipping them to the column. |
| `-y, --yes` | Don't ask for confirmation before writing. |
| `-p, --port PATH` | Raw serial port (see [device selection](../reference.md#device-selection) — exception below). |
| `-d, --device ID` | Stable device ID from `bombercat device list`. |

```sh
bombercat flash --list
bombercat flash --list --full
bombercat flash --refresh --list
bombercat flash NFCGate
bombercat flash NFCGate -d 2
bombercat flash ./build/mine.uf2
bombercat flash magspoof -y
```

**`flash` does not use the handshake to find the board.** Other selectors resolve a board by making it answer the control REPL, which only NFCGate does — that would make it impossible to flash a board running any of the other eight images. Here a single attached BomberCat is picked by USB identity alone; several attached still require `-d`/`-p`, and a board already in bootloader mode needs no port at all.

---

#### `flash --list`

```sh
bombercat flash --list
```

```
                                 Firmware images — v1.0.1.0-beta
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Firmware                    ┃   Size ┃ Description                                               ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
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

Descriptions are clipped to one line; `--full` prints the whole paragraph. What each image is good for on the host side is in [Firmwares](../commands/status.md#firmware-capability-table).

---

#### Naming the image

`FIRMWARE` is resolved in this order, first match wins:

1. **An existing path** — `./build/NFCGate.uf2`. Used as-is, no cache, no network.
2. **The exact asset name** — `NFCGate.uf2`.
3. **The name, case-insensitively** — `nfcgate`, `NFCGate`.
4. **A unique substring** — `magspoofc` → `MagspoofCVSAttack.uf2`. Several matches is an error listing candidates; it never guesses.

---

#### Tab completion

With [`completion`](completion.md) installed, `FIRMWARE` completes by name:

```
$ bombercat flash <TAB>
DetectTags                   MagspoofCVSAttack            host_Relay_NFC
ESP32SerialPassthroughFlash  NFCGate                      magspoof
MagSpoofMqtt                 WiFiWebServer                client_Relay_NFC

$ bombercat flash magspoof<TAB>
MagSpoofMqtt  MagspoofCVSAttack  magspoof

$ bombercat flash ./build/<TAB>      # paths fall back to file completion
```

Matching is by substring, the same rule the resolver above uses. zsh and fish also show each image's description next to its name.

The names come from the cached release; with an empty cache the nine known images are offered anyway, so completion works on a fresh install. It only ever reads the disk — pressing `<TAB>` never contacts GitHub and never fills the cache.

---

#### Flashing

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

Flashing is destructive, so it confirms first; `-y` skips the prompt. The sequence:

1. The image is validated as a UF2 for the RP2040 **before** anything reboots — a bad file must not leave the board stranded in bootloader mode.
2. If an `RPI-RP2` drive is already mounted, the board is in bootloader mode and steps 3–4 are skipped.
3. The board is rebooted into the bootloader with a 1200-bps touch, and the CLI waits for its serial port to disappear (≤ 5 s) and the drive to appear (≤ 15 s).
4. The `.uf2` is copied to the drive.
5. The board reboots itself into the new firmware; the CLI waits up to 15 s for a serial port to reappear and reports it.

Anything that goes wrong is a one-line `✗` plus exit code 1, except the board not reaching bootloader mode, which prints a panel with the manual route (double-tap RESET). See [Troubleshooting](../troubleshooting.md#no-rpi-rp2-drive).

The configuration saved in the board's flash (WiFi, nfcgate parameters) **survives** reflashing — it lives in a separate region the `.uf2` doesn't overwrite.

---

### Firmwares

`status` recognises the images published by [bombercat-firmware](https://github.com/ElectronicCats/bombercat-firmware) (the same set `flash` can write). **NFCGate** carries the full control REPL (`SerialControl`); five more embed the small `BomberCatControl` REPL, enough to answer `ping`/`info`/`identify` and name themselves. The remaining three have no REPL at all and are best-effort only (USB presence + optional boot banner).

| Firmware | Control REPL | Host capabilities |
|---|---|---|
| **NFCGate** | ✅ handshake | relay, config, monitor, identify, capture |
| DetectTags | ✅ handshake | monitor, identify, tags ([`tags read`/`watch`/`scan`/`info`](../commands/tags.md)) |
| DetectReaders | ✅ handshake | monitor, identify, readers ([`readers read`/`watch`/`scan`/`info`](../commands/readers.md)) |
| magspoof | ✅ handshake | monitor, identify, magspoof ([`magspoof play`/`show`/`watch`/`info`/`nfc`/`card`](../commands/magspoof.md)) |
| MagspoofCVSAttack | ✅ handshake | monitor, identify |
| MagSpoofMqtt | ✅ handshake | monitor, identify |
| WiFiWebServer | ✅ handshake | identify (browser UI; nothing else on serial) |
| host_Relay_NFC | ❌ banner only | monitor |
| client_Relay_NFC | ❌ banner only | monitor |
| ESP32SerialPassthroughFlash | ❌ | passthrough |

> The REPL arrived with the firmware refactor that also added `fw_name`. Every `.uf2` published **before** it — which includes the images `flash` downloads today — answers only in NFCGate's case, so most boards in the wild land on level 2 or 3 above until they are reflashed from a newer release.

---

### The release cache

Images are cached under `~/.bombercat/firmware/<tag>/`, alongside an `index.json` recording the tag and the day it was last checked:

```
~/.bombercat/firmware/
├── index.json                  # {"tag": "v1.0.1.0-beta", "checked": "2026-08-21"}
└── v1.0.1.0-beta/
    ├── NFCGate.uf2
    ├── …
    ├── descriptions.json
    └── release.json
```

- **The network is only touched on demand**, never on startup: the first `--list`/`flash` that needs the cache fills it. A local `.uf2` never does.
- **Revalidation is daily**, or immediate with `--refresh`.
- Every asset is checked against the SHA256 the release publishes; downloads go to a staging directory and are moved into place only once complete, so an interrupted download can't leave a cache that looks valid.
- With a populated cache and no connectivity, `flash` warns that it could not reach GitHub and uses what's on disk. With an empty cache, it's an error.
- If the repo has no published release, that is said as such — the build workflow only attaches `.uf2` assets on `release: published`.

Redirect the cache with `BOMBERCAT_FIRMWARE_CACHE` and the source repo with `BOMBERCAT_FIRMWARE_REPO` (useful against a fork); `GITHUB_TOKEN` is used for the API if set, which raises the unauthenticated 60 requests/hour limit.

---

### Notes

- See [Device selection: `-d` / `-p`](../reference.md#device-selection) for selector rules (with the exception noted above for `flash`).
- For wiring, board profile, or building from source, see [`firmware/NFCGate/README.md`](../../../firmware/NFCGate/README.md).
