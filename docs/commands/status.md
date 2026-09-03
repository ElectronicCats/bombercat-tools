# `bombercat status`

> Report which firmware is flashed on a BomberCat, and what it can do.

## Quick Start

```sh
bombercat status
```

Identifies the board by handshake, boot banner, or USB ID — works with any firmware.

---

## Subcommands

### `status`

> Report which firmware is flashed on a BomberCat, and what it can do.

| Option | Description |
|---|---|
| `--no-sniff` | Skip boot-banner sniffing (levels 1 & 3 only). |
| `-p, --port PATH` | Raw serial port (`/dev/ttyACM0`, `COM3`). |
| `-d, --device ID` | Stable device ID from `bombercat device list`. |

```sh
bombercat status
bombercat status --no-sniff
bombercat status -d 2
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

---

### Detection Confidence Levels

`status` identifies a board by levels of confidence so it works for any firmware:

1. **handshake (certain)** — board answers control REPL *and* names itself (`fw_name` + version). Six of nine images do.
2. **handshake, name inferred (likely)** — REPL answers but sends no `fw_name` (image built before `fw_name` existed). Only NFCGate had REPL back then.
3. **boot banner (likely)** — REPL silent, but known boot-output string matched. Best-effort, reset-sensitive; disable with `--no-sniff`. Output matching *two* firmwares names neither.
4. **USB id only (firmware unknown)** — BomberCat present by USB VID/PID but nothing identified. Reported honestly, not guessed.
5. **not detected** — nothing responded on the port.

---

### Firmware Capability Table

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

> The REPL arrived with the firmware refactor that also added `fw_name`. Every `.uf2` published **before** it — which includes the images `flash` downloads today — answers only in NFCGate's case, so most boards in the wild land on level 2 or 3 above until reflashed from a newer release.

---

### Next Steps

`status` ends by suggesting commands the detected firmware actually supports. An unidentified board is pointed at [`flash`](flash.md), never at the relay controls it cannot serve. Exit code is `1` only when **nothing** responds on the port.

> The previous `bombercat status` (relay state) is now [`bombercat relay status`](relay.md#relay-status).
