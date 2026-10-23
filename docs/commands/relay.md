# `bombercat relay`

> NFCGate relay: configure it, run it, and watch the APDU relay.

## Quick Start

### Path A — Two BomberCats via `nfcgate-server`

One board reads a physical card (`reader`), the other emulates it to a terminal (`card`). Both share the same `--server` and `--session`.

```sh
# 0. No nfcgate-server of your own (no VPS)? Run one on this machine, in
#    another terminal, and use its LAN address as --server below
bombercat relay server run

# 1. Discover devices
bombercat device list

# 2. Configure WiFi (same network for both)
bombercat relay config wifi -d 1 --ssid MyNet --pass 's3cret'
bombercat relay config wifi -d 2 --ssid MyNet --pass 's3cret'

# 3. Configure nfcgate: same server + session, opposite roles
bombercat relay config nfcgate -d 1 --server 192.168.1.5:5566 --session 42 --role reader
bombercat relay config nfcgate -d 2 --server 192.168.1.5:5566 --session 42 --role card

# 4. Confirm (each config blinks the LED of the board it configured)
bombercat relay config show -d 1
bombercat relay config show -d 2

# 5. Start the relay on both
bombercat relay run -d 1        # reader
bombercat relay run -d 2        # card

# 6. Watch it
bombercat relay status -d 1
bombercat relay monitor -d 1    # reader side APDU logs

# 7. Capture APDUs (see [Capture](../commands/capture.md))
bombercat capture start -d 1 -ws           # reader side (pre-mutation APDU)
bombercat capture start -d 2 -ws -o emv.pcap  # card side (post-mutation) + file

# 8. Stop
bombercat relay stop -d 1
bombercat relay stop -d 2
```

### Path B — Against the NFCGate Android App

Phone running NFCGate app is one peer, single BomberCat is the other.

```sh
# B1 example: BomberCat is the reader
bombercat relay config wifi    --ssid MyNet --pass 's3cret'
bombercat relay config nfcgate --server <server-host>:5566 --session 42 --role reader
bombercat relay run
bombercat relay status          # peer present yes  once the phone joins the session
bombercat relay monitor
```

In the NFCGate app, point it at the same `nfcgate-server`, set the same session, and pick the opposite role (card/HCE for B1, reader for B2). The BomberCat's `status` flips `peer present` to `yes` when the app joins the session.

> Card/HCE mode in the NFCGate app **requires a rooted phone** with NFCGate's
> native module (Zygisk + LSPosed) — see the
> [rooting guide](../android-nfcgate-rooting-guide.en.md). If you'd rather not
> root the phone, use it as the `reader` instead (no root required).

---

## Subcommands

The relay commands live under `bombercat relay …`. All but [`relay server`](#relay-server-run) need a board flashed with the **NFCGate** firmware, and check for it before doing anything else. A board running something else is offered a fix, or a clean error if declined ([Auto-flash](../reference.md#auto-flash)):

```
✗ `relay` needs NFCGate; this board is running DetectTags.
    bombercat flash NFCGate
```

The old root spellings (`config`, `run`, `stop`, `monitor`) still work for one release as hidden aliases that warn and forward here.

All subcommands except `relay server` take the [device selectors](../reference.md#device-selection).

---

### `relay config`

> Configure the relay (WiFi + nfcgate parameters), persisted in flash.

All three subcommands take the [device selectors](../reference.md#device-selection). The two `config` setters also **blink the LED** of the board they just configured (a non-fatal courtesy — a board on pre-0.7.0 firmware just earns a warning), so you can match `-d 2` to a physical board on the desk.

#### `relay config wifi`

> Set the WiFi credentials.

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

> Set the `nfcgate-server`, session and role.

| Option | Description |
|---|---|
| `--server TEXT` | `nfcgate-server` as `host` or `host:port`. **Required.** |
| `--session INTEGER` | Session byte `1..255`; **both peers must match**. **Required.** |
| `--role [reader\|card]` | `reader` = read a physical card, `card` = emulate one to a terminal. **Required.** |
| `--save` / `--no-save` | Persist to flash (default: `--save`). |

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

`--server` may include the port (`host:port`); an out-of-range port is rejected with a clean error. If omitted, the device keeps its stored port (default 5566).

#### `relay config show`

> Show the device's current configuration (same table as `device info`).

```sh
bombercat relay config show -d 2
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

---

### `relay run`

> Start the relay (associate WiFi, connect the server, begin the session).

Takes the [device selectors](../reference.md#device-selection).

`run` is **non-blocking on the device**: it only *accepts* the request and starts the bring-up in the background. The CLI then polls `status` and reports progress until the relay reaches `relaying` (success) or `error`, or a **45 s** budget expires. A `-ERR` on acceptance means it could not even start (e.g. empty SSID, already running).

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

If it does not reach `relaying` in time the device is **not** wedged (the REPL stayed live) — the CLI points you at the likely culprit (server not listening, PN7150 not responding) and suggests `bombercat relay status` / `monitor`. See [Troubleshooting](../troubleshooting.md#run-times-out).

---

### `relay stop`

> Stop the relay.

Takes the [device selectors](../reference.md#device-selection).

```sh
bombercat relay stop
```

```
✓ relay stopped on /dev/ttyACM0
```

---

### `relay status`

> Show live relay status (state, link, peer, relayed count).

Takes the [device selectors](../reference.md#device-selection).

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

`state` is one of `idle`, `connecting`, `relaying`, `error` (see [protocol](../protocol.md#status-fields)).

---

### `relay monitor`

> Stream the device's serial output live (relay logs + APDU hex). Ctrl-C to quit.

Takes the [device selectors](../reference.md#device-selection).

`monitor` is read-only — it does not disturb a running relay. On entry it raises the firmware log level to Debug (so per-APDU hex dumps appear) and restores it to Warn on exit. Lines are colorized: APDU hex (`cmd:`/`resp:`) in cyan, errors in red, protocol markers dimmed.

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

### `relay server run`

> Run an nfcgate-server here until Ctrl-C; peers on your LAN connect to it.

The relay peers (two BomberCats, or a BomberCat and the NFCGate app) never talk to each other directly: they meet at an **nfcgate-server**. If you have no VPS running one, this command turns the machine you are on into that server. It needs no board, no Docker and no internet — the server is embedded in the CLI and uses only the Python standard library, so it works the same from the `.deb`, the Arch package, the macOS `.pkg`, the Windows installer or a source checkout.

| Option | Description |
|---|---|
| `-p, --port INTEGER` | TCP port to listen on, on every interface (default `5566`, the nfcgate default). |
| `-q, --quiet` | Don't print every relayed frame (see below). |

```sh
bombercat relay server run            # listen on :5566
bombercat relay server run -p 6000    # another port — point the peers at it
bombercat relay server run -q         # connections only, no per-frame log
```

```
✓ nfcgate-server listening on port 5566 — Ctrl-C to stop
ℹ point both relay peers at this machine's LAN address, e.g.:
  bombercat relay config nfcgate --server 192.168.1.5:5566 --session 1 --role reader
17:06:05.550  192.168.1.21:38236  connected
17:06:05.554  192.168.1.21:38236  joined session 42
17:06:05.755  192.168.1.22:51544  OP_PSH R: (initial) 00a404000e325041592e5359532e444446303100
17:06:05.755  192.168.1.21:38236  OP_PSH C: (initial) 6f23840e325041592e5359532e4444463031a5119000
…
server stopped
```

Each relayed frame is decoded as `OP_<opcode> <R|C>: [(initial)] <APDU hex>`, where `R` comes from the reader side and `C` from the card side. A frame that does not decode is printed as hex.

**Requirements:** none beyond the CLI itself, plus:

| Requirement | Why | Check |
|---|---|---|
| The port is free (default `5566`) | the server listens on it | `ss -ltn \| grep 5566` (Linux/macOS: `lsof -i :5566`) |
| The peers can reach this machine | they connect over the LAN/WiFi | from another device: `nc -vz <this-host-LAN-IP> 5566` |
| Inbound TCP allowed on that port | host firewalls drop it by default | Linux: `sudo ufw allow 5566/tcp` or `sudo firewall-cmd --add-port=5566/tcp`; Windows/macOS: accept the firewall prompt on the first run |

**Good to know:**

- The suggested `--server` address is this machine's LAN address. Point the peers at that, **not** `127.0.0.1`: for a BomberCat, `127.0.0.1` is the board itself.
- It listens on **every interface** (`0.0.0.0`), as it has to for boards on WiFi to reach it. Anyone on the same network can join a session, so run it on networks you trust. Like the upstream server, it does no authentication and no TLS.
- Peers only see the frames of their own session (`--session`). A frame never goes back to the peer that sent it.
- Printing every frame happens on the relay's lock-step hot path. Use `-q` when you are timing transactions rather than reading them.
- It is the [ElectronicCats/nfcgate-server](https://github.com/ElectronicCats/nfcgate-server) relay (Apache-2.0) with its latency fixes (TCP_NODELAY, one write per frame) built in. A port already in use, or one below 1024, gets an explanation and a fix instead of a traceback.
- Need the relay up **permanently**, or reachable from outside your LAN? See [Deployment](../deployment.md) for running `nfcgate-server` on a VPS.
- Contributors: the Docker fixture, the relay smoke test and the latency-patch verifier are dev scripts under [`testserver/`](../../testserver/README.md). They work against this server too.

---

### Notes

- Requires **NFCGate** firmware (confirm with [`bombercat status`](../commands/status.md)), except `relay server run`.
- See [Device selection: `-d` / `-p`](../reference.md#device-selection) for selector rules.
- The previous `bombercat status` (relay state) is now `bombercat relay status`.
- The old root spellings (`config`, `run`, `stop`, `monitor`) still work for one release as hidden aliases that warn and forward here.

---

## Cheat sheet

| Step | Single board | Two boards |
|---|---|---|
| Server (no VPS) | `bombercat relay server run` | same — one server for both |
| Discover | `bombercat device list` | `bombercat device list` |
| WiFi | `bombercat relay config wifi --ssid … --pass …` | add `-d 1`, `-d 2` |
| nfcgate | `bombercat relay config nfcgate --server … --session … --role …` | opposite roles, same session |
| Start | `bombercat relay run` | `bombercat relay run -d 1 && bombercat relay run -d 2` |
| Watch | `bombercat relay status` / `bombercat relay monitor` | add `-d <ID>` |
| Capture | `bombercat capture start -ws` | capture each side |
| Stop | `bombercat relay stop` | `bombercat relay stop -d 1 && bombercat relay stop -d 2` |

For a bench with no RF at all, the dev script `testserver/relay_smoketest.py` exercises the relay path against a running server (see [`testserver/README.md`](../../testserver/README.md#smoke-test-no-hardware)).

---

## See also

- [`capture`](../commands/capture.md) — tap a copy of the APDUs a running relay is passing, to Wireshark and/or a `.pcap` file. Start the relay first.
- [`device`](../commands/device.md) — list and identify boards before addressing them with `-d`.
- Full two-board and Android-app workflows: [End-to-end usage](../usage.md).
