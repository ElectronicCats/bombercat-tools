# Command reference

Complete reference for every `bombercat` command and subcommand: purpose, flags, examples and expected output.

**Control plane only.** Every command that talks to a board does so over the USB-serial control protocol. No APDUs travel over serial; relayed APDUs go over WiFi/TCP to the `nfcgate-server`. `capture` streams a *copy* of them over the control link — see [`capture`](commands/capture.md).

- [Invocation](#invocation)
- [Global options](#global-options)
- [Device selection: `-d` / `-p`](#device-selection)
- [Commands](#commands)
  - [`device`](commands/device.md)
  - [`identify`](commands/device.md#identify)
  - [`status`](commands/status.md)
  - [`flash`](commands/flash.md)
  - [`relay`](commands/relay.md)
  - [`capture`](commands/capture.md)
  - [`tags`](commands/tags.md)
  - [`readers`](commands/readers.md)
  - [`magspoof`](commands/magspoof.md)
  - [`proto`](commands/proto.md)
  - [`testserver`](commands/testserver.md)
  - [`completion`](commands/completion.md)
- [Environment variables](#environment-variables)
- [Exit codes](#exit-codes)
- [Glossary](glossary.md)
- [Old anchor links (pre-restructure)](#old-anchor-links-pre-restructure)

---

## Invocation

Run from the `tools/` directory:

```sh
python3 bombercat.py <command> [options]
```

The docs write it as `bombercat` for brevity. To get that short form, either add a shell alias:

```sh
alias bombercat='python3 /abs/path/to/tools/bombercat.py'
```

or install [shell completion](commands/completion.md), which also lets you run `python bombercat.py <TAB>`.

Every command and group accepts `-h` / `--help`.

<a id="global-options"></a>
## Global options

Placed before the command (`bombercat -v device list`):

| Option | Description |
|---|---|
| `-v`, `--verbose` | Raise the log level to INFO (shows the `rich` logger's info lines; off by default, which is WARNING). Repeatable (`-vv`). |
| `-h`, `--help` | Show help and exit. |

`-v` is also accepted **after** the command (`bombercat device list -v`) wherever a subcommand lists it in its own `Options:` — both positions mean the same thing, and the higher count wins if you somehow pass both. On the [`tags`](commands/tags.md) commands specifically, `-v` does more than raise the log level: it traces the raw `>`/`<` wire protocol to stderr (tx cyan, rx dim), and `-vv` adds an elapsed-time stamp and byte count ahead of each line:

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

Traced output always goes to **stderr**, so `--json | jq` keeps working with `-v` on — stdout carries only the JSON. `set pass …` lines are redacted at any level, since that's the WiFi/relay secret in plain text. Other commands don't wire a tracer yet, so `-v` there is log-level only.

Every run prints the ASCII header panel with the CLI version and a random tagline before the command output. (The header is suppressed while generating shell completion.)

<a id="device-selection"></a>
## Device selection: `-d` / `-p`

Every command that talks to a board takes the same two mutually-exclusive selectors:

| Option | Description |
|---|---|
| `-p`, `--port PATH` | Raw serial port (`/dev/ttyACM0`, `COM3`). Used as-is; no enumeration. |
| `-d`, `--device ID` | Stable device ID from `bombercat device list` (for multiple boards). |

Resolution rules (implemented in `resolve_port`, see [protocol](protocol.md#port-discovery--numbering)):

- `--port` wins and is used verbatim.
- `--device` selects one of the numbered devices without handshaking the others.
- With **neither** given and exactly **one** BomberCat attached, it is auto-detected by handshake.
- Zero or several attached and no selector → a clean error telling you to pass `-d`/`--port`.

`-d` and `-p` are mutually exclusive; passing both is an error.

### Examples

**One board attached — no selector needed:**

```sh
bombercat device info
```

```
      device @ /dev/ttyACM0
┌──────────┬─────────────────┐
│ port     │ /dev/ttyACM0    │
│ fw       │ 0.9.7           │
│ fw_name  │ NFCGate         │
│ state    │ idle            │
└──────────┴─────────────────┘
```

Auto-detection handshakes every candidate port; with exactly one answering
BomberCat, that's the target. Zero or several attached with no selector is a
clean error, not a guess — it tells you to pass `-d`/`--port`.

**Several boards — pick one by stable ID:**

```sh
bombercat device list
```

```
 id  port           usb id      answered  fw       fw_name
 1   /dev/ttyACM0   1209:005E   ✓         0.9.7    NFCGate
 2   /dev/ttyACM1   1209:005E   ✓         1.1.1.0  magspoof
```

```sh
bombercat -d 1 relay status
bombercat -d 2 magspoof show
```

IDs come from a stable USB identity (serial number, then topology, then port
path — see [Port discovery & numbering](protocol.md#port-discovery)), so `-d 1`
keeps pointing at the same physical board across replugs, **as long as the
same set of boards stays attached**. Confirm a mapping before trusting it —
blink the LED with `bombercat identify -d 1`.

**Bypass discovery — name the port directly:**

```sh
bombercat -p /dev/ttyACM0 status
bombercat status -p COM3
```

`--port` is used as-is, no handshake of anything else. Useful when discovery
itself is what you're debugging, or the board's USB identity isn't recognized
(see [`BOMBERCAT_VID`/`BOMBERCAT_PID`](#environment-variables)).

**Two boards for a relay pair:** each peer needs its own selector — see the
[relay cheat sheet](commands/relay.md#cheat-sheet) for a full two-board
session.

```sh
bombercat -d 1 relay run --role reader
bombercat -d 2 relay run --role card
```

For every troubleshooting scenario around a selector (wrong board answering,
IDs renumbering, zero/several boards found), see
[Wrong board answers to `-d`](troubleshooting.md#wrong-board) and
[No BomberCat found / board not detected](troubleshooting.md#board-not-detected).

---

## Commands

Each command group has its own detailed reference page under `docs/commands/`:

| Command | Description | Reference |
|---|---|---|
| [`device`](commands/device.md) | Discover and inspect BomberCat devices over USB-serial. | `device list`, `device info`, `identify` |
| [`status`](commands/status.md) | Report which firmware is flashed and what it can do. | `status` |
| [`flash`](commands/flash.md) | Download and flash a BomberCat firmware image. | `flash`, `flash --list` |
| [`relay`](commands/relay.md) | NFCGate relay: configure it, run it, and watch the APDU relay. | `config wifi/nfcgate/show`, `run`, `stop`, `status`, `monitor` |
| [`capture`](commands/capture.md) | Capture relayed APDUs to pcap (live Wireshark and/or a file). | `capture start`, `capture stop` |
| [`tags`](commands/tags.md) | NFC tag detection over the DetectTags firmware's PN7150 reader. | `read`, `watch`, `scan`, `info` |
| [`readers`](commands/readers.md) | NFC reader/terminal detection over the DetectReaders firmware. | `read`, `watch`, `scan`, `info` |
| [`magspoof`](commands/magspoof.md) | Magstripe emulation: play, show, watch, NFC, and multi-card store. | `play`, `show`, `watch`, `info`, `nfc *`, `card *` |
| [`proto`](commands/proto.md) | Nanopb protobuf sources for the NFCGate relay. | `proto gen` |
| [`testserver`](commands/testserver.md) | Local nfcgate-server for relay testing (no hardware/RF). | `run`, `verify`, `smoke` |
| [`completion`](commands/completion.md) | Install shell tab completion for bombercat. | `completion install` |

---

## Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `BOMBERCAT_VID` / `BOMBERCAT_PID` | device discovery | Declare a custom USB VID/PID for a board re-flashed with a non-stock USB identity (hex `0x1209` or decimal). Both must be set to add the pair to the match list. |
| `BOMBERCAT_FIRMWARE_REPO` | [`flash`](commands/flash.md) | GitHub repo to pull firmware releases from (default `ElectronicCats/bombercat-firmware`). Point it at a fork to test against its releases. |
| `BOMBERCAT_FIRMWARE_CACHE` | [`flash`](commands/flash.md) | Where downloaded images live (default `~/.bombercat/firmware`). |
| `GITHUB_TOKEN` | [`flash`](commands/flash.md) | Sent as a bearer token to the GitHub API, raising the 60 req/h unauthenticated limit. Optional. |
| `BOMBERCAT_SMOKE_VENV` | `testserver smoke` | Path to the throwaway protobuf venv (default `tools/.venv-smoke`). |
| `SERVER_REPO` | `testserver/fetch_server.sh` | Use an existing server clone / mirror instead of cloning. |
| `PORT` | `testserver/run.sh` | Host port for the server (set for you by `testserver run -p`). |

## Exit codes

- `0` — success.
- `1` — a handled error: no board found, handshake failed, a `set`/`run`/`capture` rejected by the device, a missing file, etc. These are reported as a clean one-line message (with a leading `✗`), **never** a Python traceback. A stray traceback is a bug worth reporting.
- `2` — `--strict` mode in [`capture start`](commands/capture.md#capture-start): link ended with zero frames captured.
- `130` — interrupted (Ctrl-C / EOF).

---

## Old anchor links (pre-restructure)

Before this file was split into `docs/commands/*.md`, every command lived here as one heading and its anchor was `reference.md#<name>`. If you (or an external doc) still have one of those bookmarked, each anchor below is kept alive and points at the new location.

<details>
<summary>Expand: old anchor → new location</summary>

| Old anchor | New location |
|---|---|
| <a id="device"></a>`#device` | [`device`](commands/device.md) |
| <a id="device-list"></a>`#device-list` | [`device list`](commands/device.md#device-list) |
| <a id="device-info"></a>`#device-info` | [`device info`](commands/device.md#device-info) |
| <a id="identify"></a>`#identify` | [`identify`](commands/device.md#identify) |
| <a id="status"></a>`#status` | [`status`](commands/status.md) |
| <a id="firmwares"></a>`#firmwares` | [Firmware Capability Table](commands/status.md#firmware-capability-table) |
| <a id="flash"></a>`#flash` | [`flash`](commands/flash.md) |
| <a id="flash---list"></a>`#flash---list` | [`flash --list`](commands/flash.md#flash---list) |
| <a id="naming-the-image"></a>`#naming-the-image` | [Naming the image](commands/flash.md#naming-the-image) |
| <a id="tab-completion"></a>`#tab-completion` | [Tab completion](commands/flash.md#tab-completion) |
| <a id="flashing"></a>`#flashing` | [Flashing](commands/flash.md#flashing) |
| <a id="the-release-cache"></a>`#the-release-cache` | [The release cache](commands/flash.md#the-release-cache) |
| <a id="relay"></a>`#relay` | [`relay`](commands/relay.md) |
| <a id="relay-config"></a>`#relay-config` | [`relay config`](commands/relay.md#relay-config) |
| <a id="relay-config-wifi"></a>`#relay-config-wifi` | [`relay config wifi`](commands/relay.md#relay-config-wifi) |
| <a id="relay-config-nfcgate"></a>`#relay-config-nfcgate` | [`relay config nfcgate`](commands/relay.md#relay-config-nfcgate) |
| <a id="relay-config-show"></a>`#relay-config-show` | [`relay config show`](commands/relay.md#relay-config-show) |
| <a id="relay-run"></a>`#relay-run` | [`relay run`](commands/relay.md#relay-run) |
| <a id="relay-stop"></a>`#relay-stop` | [`relay stop`](commands/relay.md#relay-stop) |
| <a id="relay-status"></a>`#relay-status` | [`relay status`](commands/relay.md#relay-status) |
| <a id="relay-monitor"></a>`#relay-monitor` | [`relay monitor`](commands/relay.md#relay-monitor) |
| <a id="capture"></a>`#capture` | [`capture`](commands/capture.md) |
| <a id="capture-start"></a>`#capture-start` | [`capture start`](commands/capture.md#capture-start) |
| <a id="capture-stop"></a>`#capture-stop` | [`capture stop`](commands/capture.md#capture-stop) |
| <a id="tags"></a>`#tags` | [`tags`](commands/tags.md) |
| <a id="tags-read"></a>`#tags-read` | [`tags read`](commands/tags.md#tags-read) |
| <a id="tags-watch"></a>`#tags-watch` | [`tags watch`](commands/tags.md#tags-watch) |
| <a id="tags-scan"></a>`#tags-scan` | [`tags scan`](commands/tags.md#tags-scan) |
| <a id="tags-info"></a>`#tags-info` | [`tags info`](commands/tags.md#tags-info) |
| <a id="readers"></a>`#readers` | [`readers`](commands/readers.md) |
| <a id="readers-read"></a>`#readers-read` | [`readers read`](commands/readers.md#readers-read) |
| <a id="readers-watch"></a>`#readers-watch` | [`readers watch`](commands/readers.md#readers-watch) |
| <a id="readers-scan"></a>`#readers-scan` | [`readers scan`](commands/readers.md#readers-scan) |
| <a id="readers-info"></a>`#readers-info` | [`readers info`](commands/readers.md#readers-info) |
| <a id="dev-tooling"></a>`#dev-tooling` | [`proto`](commands/proto.md) and [`testserver`](commands/testserver.md) |
| <a id="proto"></a>`#proto` | [`proto`](commands/proto.md) |
| <a id="proto-gen"></a>`#proto-gen` | [`proto gen`](commands/proto.md#proto-gen) |
| <a id="testserver"></a>`#testserver` | [`testserver`](commands/testserver.md) |
| <a id="testserver-run"></a>`#testserver-run` | [`testserver run`](commands/testserver.md#testserver-run) |
| <a id="testserver-smoke"></a>`#testserver-smoke` | [`testserver smoke`](commands/testserver.md#testserver-smoke) |
| <a id="completion"></a>`#completion` | [`completion`](commands/completion.md) |
| <a id="completion-install"></a>`#completion-install` | [`completion install`](commands/completion.md#completion-install) |

</details>
