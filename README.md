# `bombercat` — control CLI & dev tooling

Here you will find all the tools supported by Electronic Cats associated to Bombercat you can find more information in the [Bombercat Wiki](https://github.com/ElectronicCats/Bombercat/wiki) about ussage.

## Wiki and Getting Started

[Getting Started in our Wiki](https://github.com/ElectronicCats/Bombercat/wiki)

<p align=center>
<a href="https://github.com/ElectronicCats/BomberCat/wiki">
  <img src="https://github.com/ElectronicCats/BomberCat/assets/107638696/354ce958-4d73-4198-bb89-8b5e16c7cd0a" width=70% />
</a>
</p>

A `click`/`rich` command-line tool for BomberCat firmwares. It talks to a
BomberCat over **USB-serial** to configure it, start/stop the NFCGate relay,
watch it live, capture the relayed APDUs to Wireshark; on the
**DetectTags** firmware — read NFC tags directly with the board's PN7150; on
**DetectReaders** — detect the readers/terminals that probe it; and on
**magspoof** — control magstripe emulation: play/inspect the active card,
manage a flash-resident multi-card store, and emulate/read EMV cards over NFC.

**Control plane only.** No APDUs travel over serial — those go over WiFi/TCP to
the `nfcgate-server`. The USB link is a text, line-based control channel: it
configures, arms, starts, monitors and captures the device, nothing more
(docs/NFCGATE_PLAN.md Fase 6). Keep this in mind everywhere below — see the
[architecture diagram](docs/usage.md) for how the pieces connect.

## Install

### Virtual Environment
```sh
python -m venv .venv
source .venv/bin/activate    # On Windows: .venv\Scripts\activate
```

```sh
python3 -m pip install -r requirements.txt   # click, rich, pyserial (+ a few extras)
python3 bombercat.py --help
```

A virtualenv is recommended. On Linux, serial access usually needs your user in
the `dialout` group — see [Troubleshooting](docs/troubleshooting.md#serial-permission-denied).
The tool is developed and tested on Linux; read
[Current limitations](#current-limitations) before running it elsewhere.

Throughout the docs the command is written as `bombercat`; if you have not set up
the [`bombercat` alias](docs/reference.md#invocation) or
[shell completion](docs/commands/completion.md), run it as
`python3 bombercat.py …` from `tools/`.

## Quick start

```sh
# 1. Discover the board(s)
bombercat device list                 # IDs + serial ports; ✓ = answered the handshake
bombercat status                      # which firmware is flashed + what it can do

# 2. Put the relay firmware on it (skip if `status` already says NFCGate)
bombercat flash --list                # the published images, with descriptions
bombercat flash NFCGate               # download and write it over UF2

# 3. Configure the NFCGate relay (persisted to flash unless --no-save)
bombercat relay config wifi    --ssid MyNet --pass 's3cret'
bombercat relay config nfcgate --server 192.168.1.5:5566 --session 42 --role reader
bombercat relay config show

# 4. Run & watch
bombercat relay run                   # associate WiFi, connect server, start relay
bombercat relay status                # state / link / peer / relayed count
bombercat relay monitor               # live serial stream (relay logs + APDU hex)
bombercat relay stop

# 5. Capture the relayed APDUs to Wireshark
bombercat capture start -ws           # live Wireshark on a FIFO (Ctrl-C to stop)
```

Not doing a relay? `bombercat flash DetectTags` and read NFC tags directly —
no server, no second board:

```sh
bombercat tags read                   # wait for one tag, print its UID
bombercat tags watch                  # stream detections until Ctrl-C
bombercat tags scan -t 20             # sample for 20s, aggregated summary
```

See [Detecting NFC tags](docs/commands/tags.md).

Or flip it around: `bombercat flash DetectReaders` and detect the
readers/terminals that probe the board's emulated card — no server, no
second board:

```sh
bombercat readers read                # wait for one reader, print its fingerprint
bombercat readers watch               # stream detections until Ctrl-C
bombercat readers scan -t 20          # sample for 20s, aggregated summary
```

See [Detecting NFC readers](docs/commands/readers.md).

Or `bombercat flash magspoof` for magstripe emulation — play/inspect the
active card, manage a flash-resident multi-card store, or emulate an EMV
card over NFC:

```sh
bombercat magspoof show                                  # inspect what's loaded
bombercat magspoof play                                  # emulate a swipe
bombercat magspoof card add visa --t2 ';4111111111111111=25121010000000000000?'
```

See [`magspoof`](docs/commands/magspoof.md). Several `card`/`nfc` subcommands
are **for authorized security testing only** — see the doc's per-command notes.

> **Command layout changed.** The relay commands now live under `bombercat
> relay …`, and `bombercat status` reports the **flashed firmware** instead of
> the relay state (that moved to `bombercat relay status`). The old root
> spellings — `config`, `run`, `stop`, `monitor` — still work for one release as
> hidden aliases that print a deprecation notice and forward to `relay …`.

A relay needs **two** peers on the same `--server` and `--session`: one
`--role reader` (reads a physical card), the other `--role card` (emulates one to
a terminal). Both boards are usually plugged into the same host — address each
one by its ID with `-d/--device`. See the
[end-to-end guide](docs/usage.md).

## Documentation

| Page | What's in it |
|---|---|
| [Command reference](docs/reference.md) | Every command and subcommand: purpose, flags, examples, expected output. `device`, `status` (flashed firmware), `flash`, `relay …` (`config`/`run`/`stop`/`status`/`monitor`), `identify`, `capture`, `tags …` (`read`/`watch`/`scan`/`info`), `readers …` (`read`/`watch`/`scan`/`info`), `magspoof …` (`play`/`show`/`watch`/`info`, `nfc *`, `card *`), `proto`, `testserver`, `completion`, and device selection with `-d`/`-p`. |
| [End-to-end usage](docs/usage.md) | The real workflow on hardware — two BomberCats via `nfcgate-server` (Path A) and against the NFCGate Android app (Path B) — config → run → monitor → capture — plus the standalone `tags`/`readers` workflows on DetectTags/DetectReaders. |
| [Control protocol](docs/protocol.md) | The line-based `SerialControl` protocol (`:key value`, `+OK`, `-ERR`), the `DeviceLink` client, and how ports are discovered and numbered. For developers. |
| [Capture / Wireshark](docs/commands/capture.md) | How `capture` taps a copy of every relayed APDU, the classic-pcap vs pcapng distinction, and the `DLT_ISO_14443` encapsulation. |
| [Troubleshooting](docs/troubleshooting.md) | Serial permissions, board not detected, old firmware without `identify`/`capture`, `run` timeouts. |
| [Glossary](docs/glossary.md) | Terms used across these docs — REPL, SEL_RES, APDU, Service Code, UF2, VID/PID, and more. |
| [Current limitations](docs/limitations.md) | Platform support matrix, host requirements, device/serial and relay-scope constraints — known, not bugs. |
| [Deploy a dedicated server](docs/deployment.md) | Run `nfcgate-server` permanently on a VPS (Docker or systemd), verify the latency patch, day-2 ops. |
| [Rooting an Android phone for NFCGate](docs/android-nfcgate-rooting-guide.en.md) ([es](docs/android-nfcgate-rooting-guide.es.md)) | Required only for Path B card/HCE mode: rooting with Magisk and installing NFCGate's native module (Zygisk + LSPosed), with the associated risks. |

## Dev tooling

These wrap the reproducible build/test scripts (see docs/NFCGATE_PLAN.md Fases 1–5);
full details in [`proto`](docs/commands/proto.md) and [`testserver`](docs/commands/testserver.md):

```sh
bombercat proto gen                   # regenerate firmware/core/src/proto/*.pb.{c,h}
bombercat testserver run [-p 5566]    # local nfcgate-server in Docker
bombercat testserver smoke [host port]# relay smoke test (no RF)
```

See [`testserver/README.md`](testserver/README.md) for the local server fixture.

## Tests (dev-only, no hardware)

Unit tests — the CLI surface (every command, its output and its exit code) with
the serial, Docker and Wireshark layers faked, so they run anywhere:

```sh
pytest                          # the whole suite (tools/tests/test_*.py)
pytest -k capture               # one area
pytest --cov=modules            # with coverage
```

End-to-end host tests — standalone scripts that exercise the real protocol
against a fake device or a live server:

```sh
python3 tools/tests/serialctl_hosttest.py         # DeviceLink protocol parser (pty)
python3 tools/tests/capture_hosttest.py           # pcap writer + ISO 14443 vs tshark
tools/testserver/codec_hosttest/build_and_run.sh  # firmware codec vs live server
```

Both suites run on every push — see [`.github/workflows/tests.yml`](.github/workflows/tests.yml).

## Current limitations

Developed and validated on **Linux** (`VERSION` 1.1.0.0); most commands should
work on macOS/Windows but are untested there. Known constraints around
platform support, host requirements (Wireshark, Docker), device/serial
behavior and relay scope — see [Current limitations](docs/limitations.md).

## Appendix: run the server on a dedicated VPS

`bombercat testserver run` is for a **local, ephemeral** server. To keep a
relay up **permanently** on a VPS or LAN box (Docker or systemd), verify the
latency patch, and do day-2 ops — see [Deployment](docs/deployment.md).

## Firmware Repository
All Bombercat Firmware has been moved to a different repository, to have a better version control, and issue tracking you will find it here:

https://github.com/ElectronicCats/bombercat-firmware

All Bombercat versions are supported in this repository, you will need to check what version you own and select the proper branch to develop or release for just program your board.

## Hardware Repository
All Bombercat Hardware has been moved to a different repository, to have a better version control, and issue tracking you will find it here:

https://github.com/ElectronicCats/Bombercat

## Disclaimer
>[!IMPORTANT]
>BomberCat is a wireless penetration testing tool intended **solely for use in authorized security audits, where such usage is permitted by applicable laws and regulations**. Before utilizing this tool, it is crucial to ensure compliance with all relevant legal requirements and obtain appropriate permissions from the relevant authorities.
>
>The board **does not provide** any means or authorization to utilize credit cards or engage in any financial transactions that are not legally authorized. **Electronic Cats holds no responsibility for any unauthorized use of the tool or any resulting damages**.

## Contribute
<img width="1354" alt="image" src="https://github.com/ElectronicCats/CatSniffer-Tools/assets/15166625/f3d1a1a2-caf5-496f-bc4d-8c7614c8af62">

## How to contribute <img src="https://electroniccats.com/wp-content/uploads/2018/01/fav.png" height="35"><img src="https://raw.githubusercontent.com/gist/ManulMax/2d20af60d709805c55fd784ca7cba4b9/raw/bcfeac7604f674ace63623106eb8bb8471d844a6/github.gif" height="30">
 Contributions are welcome!

Please read the document  [**Contribution Manual**](https://github.com/ElectronicCats/electroniccats-cla/blob/main/electroniccats-contribution-manual.md)  which will show you how to contribute your changes to the project.

✨ Thanks to all our [contributors](https://github.com/ElectronicCats/CatSniffer-Tools/graphs/contributors)! ✨

See [**_Electronic Cats CLA_**](https://github.com/ElectronicCats/electroniccats-cla/blob/main/electroniccats-cla.md) for more information.

See the  [**community code of conduct**](https://github.com/ElectronicCats/electroniccats-cla/blob/main/electroniccats-community-code-of-conduct.md) for a vision of the community we want to build and what we expect from it.
