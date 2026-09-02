# End-to-end usage

The real workflow: bring up an NFCGate relay on hardware and drive it from the CLI. Two topologies are supported and validated on hardware:

- **Path A** — two BomberCats (reader + card) joined by an `nfcgate-server`.
- **Path B** — one BomberCat against the **NFCGate Android app** as the other peer (both variants: BomberCat as reader or as card).

**Control plane only.** Everything the CLI does below travels over USB-serial as text control commands. The actual APDUs never touch serial — they go over WiFi/TCP between the two peers through the `nfcgate-server`:

```mermaid
flowchart LR
    card([Card]) -->|RF| reader[BomberCat READER]
    reader -->|WiFi / TCP| server[nfcgate-server]
    server -->|WiFi / TCP| emu[BomberCat CARD]
    emu -->|RF| term([Terminal])

    cli[bombercat CLI]
    cli -. "USB-serial<br/>(config / run / status / capture)" .-> reader
    cli -. "USB-serial<br/>(config / run / status / capture)" .-> emu

    subgraph data [APDU data plane]
        reader
        server
        emu
    end
```

---

## Quick links to workflows

Each command group now includes a **Quick Start** section with the relevant workflow:

| Workflow | Command reference | Quick Start |
|---|---|---|
| **NFCGate relay (Path A / B)** | [`relay`](commands/relay.md) | [Two-board setup](commands/relay.md#quick-start) • [Android app](commands/relay.md#path-b--against-the-nfcgate-android-app) |
| **Capture APDUs to Wireshark/pcap** | [`capture`](commands/capture.md) | [Capture workflow](commands/capture.md#quick-start) |
| **Detect NFC tags (DetectTags)** | [`tags`](commands/tags.md) | [Tag detection](commands/tags.md#quick-start) |
| **Detect NFC readers (DetectReaders)** | [`readers`](commands/readers.md) | [Reader detection](commands/readers.md#quick-start) |
| **Magstripe emulation (MagSpoof)** | [`magspoof`](commands/magspoof.md) | [MagSpoof workflow](commands/magspoof.md#quick-start) |
| **Flash firmware** | [`flash`](commands/flash.md) | [Flash workflow](commands/flash.md#quick-start) |
| **Discover devices** | [`device`](commands/device.md) | [Device discovery](commands/device.md#quick-start) |
| **Check firmware** | [`status`](commands/status.md) | [Firmware check](commands/status.md#quick-start) |
| **Local test server** | [`testserver`](commands/testserver.md) | [Test server](commands/testserver.md#quick-start) |

---

## Prerequisites

- Both boards flashed with the **NFCGate relay firmware** and answering the handshake (`bombercat device info` shows a `fw` version and `state idle`). Check what each one is running with `bombercat status`, and flash it from the CLI if it isn't NFCGate:
  ```sh
  bombercat flash NFCGate -d 1
  bombercat flash NFCGate -d 2
  ```
  See [`flash`](commands/flash.md). For wiring, the board profile, or to build the firmware from source, see [`firmware/NFCGate/README.md`](../../firmware/NFCGate/README.md).
  - The PN7150 pins are the BomberCat defaults; no extra wiring is needed.
  - The firmware must boot into the control REPL, i.e. `RELAY_AUTOSTART = 0` (the default). With `RELAY_AUTOSTART = 1` and a non-empty SSID the board blocks on WiFi bring-up before the REPL starts and the CLI can't reach it.
- A reachable `nfcgate-server`. For a bench setup, run the local one:
  ```sh
  bombercat testserver run           # Docker, host :5566
  ```
  Both peers point at this host:port. (Real deployments use any reachable `nfcgate-server` — the app's default public server, or your own.)
- The two boards usually share one host over USB. List and address them by ID:
  ```sh
  bombercat device list
  ```

---

## Cheat sheet

A single-board vs. two-board command cheat sheet lives in [`relay` → Cheat sheet](commands/relay.md#cheat-sheet).

---

## Further reading

- [`reference.md`](reference.md) — complete command reference index
- [`protocol.md`](protocol.md) — wire protocol details
- [`capture.md`](commands/capture.md) — pcap/ISO 14443 framing details
- [`troubleshooting.md`](troubleshooting.md) — common issues and fixes
