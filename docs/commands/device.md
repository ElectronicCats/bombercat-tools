# `bombercat device`

> Discover and inspect BomberCat devices over USB-serial.

## Quick Start

```sh
bombercat device list          # list all serial ports and BomberCat IDs
bombercat device info          # handshake with auto-detected board
bombercat device info -d 2     # handshake with specific board
bombercat identify -d 1        # blink board #1's LED
```

---

## Subcommands

### `device list`

> List serial ports, the device ID of each BomberCat and who answers.

| Option | Description |
|---|---|
| `-a, --all` | Include non-candidate ports (built-in UARTs, Bluetooth). |

```sh
bombercat device list
bombercat device list -a
```

```
                              Serial ports
┏━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┓
┃ ID ┃ Port         ┃ BomberCat ┃ Serial#          ┃ HWID                ┃
┡━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━┩
│ #1 │ /dev/ttyACM1 │ ✓         │ 36A864E62A367EA3 │ USB VID:PID=…       │
│ #2 │ /dev/ttyACM0 │ ✓         │ E6614C775B4F2A21 │ USB VID:PID=…       │
└────┴──────────────┴───────────┴──────────────────┴─────────────────────┘
Target one with:  bombercat <command> -d <ID>   (e.g. bombercat config show -d 1)
```

**BomberCat column:**

- `✓` — port answered the control handshake (running NFCGate relay firmware).
- `USB id` — USB VID/PID says BomberCat, but did **not** answer handshake (probably not running NFCGate — check with [`bombercat status`](status.md)).
- blank — not a BomberCat candidate.

IDs are derived from a **stable USB identity** (serial number first, then USB port location), so a board keeps its number across replugs and reboots as long as the same set of boards is attached — not from the OS `/dev/ttyACM*` order, which is non-deterministic. Numbering never opens a port (opening one can reset the MCU), so `device list` is cheap; only the `✓` column costs a handshake.

If no attached port carries a BomberCat USB VID/PID, every candidate port is numbered instead and the table says so — verify the IDs before trusting `-d`.

---

### `device info`

> Handshake with a BomberCat and show its firmware/config.

Takes the [device selectors](../reference.md#device-selection).

```sh
bombercat device info            # single board, auto-detected
bombercat device info -d 2       # specific board
```

```
        BomberCat @ /dev/ttyACM0
┏━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Field   ┃ Value                     ┃
┡━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ fw      │ 0.9.7                      │
│ fw_name │ NFCGate                     │
│ role    │ reader                     │
│ ssid    │ MyNet                      │
│ server  │ 192.168.1.5                │
│ port    │ 5566                       │
│ session │ 42                         │
│ state   │ idle                       │
└─────────┴────────────────────────────┘
```

(`relay config show` prints the same table.)

---

### `identify`

> Blink a device's LED so you can tell which board an ID refers to.

Takes the [device selectors](../reference.md#device-selection). Requires firmware ≥ 0.7.0; on older firmware it reports that the board predates `identify`.

```sh
bombercat identify -d 1          # blink board #1's LED for a couple of seconds
```

```
✓ /dev/ttyACM1 is blinking its LED for a couple of seconds
```

---

### Notes

- See [Device selection: `-d` / `-p`](../reference.md#device-selection) for selector rules.
- If a board shows `USB id` but not `✓` in `device list`, it's likely running a firmware without the control REPL. Use [`bombercat status`](status.md) to identify it.
