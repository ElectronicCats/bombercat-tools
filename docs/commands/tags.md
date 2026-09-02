# `bombercat tags`

> NFC tag detection over the **DetectTags** firmware's PN7150 reader.

## Quick Start

```sh
bombercat status                # confirm DetectTags is flashed (or flash it)
bombercat flash DetectTags -d 1 # if it isn't

bombercat tags read             # wait for one tag, print its UID and exit
bombercat tags watch            # stream detections until Ctrl-C
bombercat tags scan -t 20       # sample for 20s, print aggregated summary
bombercat tags info             # firmware version + which event format it speaks
```

---

## Subcommands

The `tags` commands live under `bombercat tags …`. They need a board flashed with **DetectTags** (confirm with [`bombercat status`](../commands/status.md)) and, like `relay`, verify the control handshake before doing anything:

```
✗ /dev/ttyACM0 did not answer the handshake. `tags` needs the DetectTags
  firmware — check what's flashed with:  bombercat status
```

All four subcommands take the [device selectors](../reference.md#device-selection) plus their own `-v`/`--verbose` (see [Global options](../reference.md#global-options) for what `-v` does here specifically — it traces the wire protocol, not just the log level).

### Structured vs. legacy events

Every published `.uf2` today predates the firmware's `:tag <ts_ms> <tech> <protocol> <uid_hex|-> [k=v …]` event line, so `tags` parses the older human-readable `displayCardInfo()` text instead — same information, slightly less of it (no UID at all for NFC-B/NFC-F, no `extra` fields). The parser detects which one a board speaks on the fly and switches permanently to structured mode the moment it sees a `:tag` line. [`tags info`](#tags-info) tells you which mode a board is in.

---

### `tags read`

> Wait for one tag and print its UID.

| Option | Description |
|---|---|
| `-t, --timeout SEC` | Seconds to wait for a tag (default `15`). |
| `--json` | Emit one JSON object on stdout instead of the field table. |

```sh
bombercat tags read
bombercat tags read -t 30 --json
```

```
ℹ Waiting for a tag on /dev/ttyACM0 — Ctrl-C to abort

  Tag detected
  uid           04:1A:2B:3C
  technology    NFC-A
  protocol      T2T
  SAK           08
```

`--json` prints a single clean object on stdout (nothing else touches stdout, so it's pipeable) with every field, `extra` merged in flat:

```json
{"uid": "041A2B3C", "tech": "NFC-A", "protocol": "T2T", "ts_ms": 1234, "SAK": "08"}
```

No tag within the timeout is exit code `1`:

```
✗ no tag detected in 15s
```

On a firmware in legacy mode with **NFC-B** or **NFC-F** presented, `uid` has no value to print — the field table shows why instead of a blank:

```
  uid           unavailable (NFC-B: firmware prints no ID)
```

(`--json`'s `"uid"` is `null` in that case, not a placeholder string.)

---

### `tags watch`

> Stream tag detections continuously. Ctrl-C to stop and print a summary.

| Option | Description |
|---|---|
| `--dedupe` | Collapse repeat detections of the same UID into a `seen again (xN)` line instead of reprinting the row. |
| `--quiet-noise` / `--no-quiet-noise` | Hide firmware boot/idle chatter — `Restarting…`, `Waiting for a Card…`, `Card removed!` (default: hidden). |
| `--json` | Emit one JSON object per line instead of the formatted line. |

```sh
bombercat tags watch --dedupe
bombercat tags watch --json
```

```
ℹ Watching /dev/ttyACM0 — Ctrl-C to stop
[12:26:48] NFC-A   T2T        04:1A:2B:3C
  ↳ 04:1A:2B:3C seen again (x2)
[12:26:48] NFC-B   ISODEP     unavailable (NFC-B: firmware prints no ID)

ℹ 3 detections, 2 unique UIDs, 41s
```

Without `--dedupe`, a repeat detection just prints another line. With `-v`/`--verbose`, the boot/idle noise always shows regardless of `--quiet-noise` — that flag only controls the *default* (non-verbose) view. `--json` emits one object per detection and skips the noise lines and the closing summary, so the stream stays valid NDJSON.

---

### `tags scan`

> Sample tag detections for a while and print an aggregated summary.

Repeat detections of the same UID collapse into one row with a count and a first/last time seen (elapsed seconds since the scan started), instead of scrolling past like `watch` does.

| Option | Description |
|---|---|
| `-t, --timeout SEC` | Seconds to sample for (default `30`). |
| `--json-out FILE` | Also write the aggregate as a JSON array to `FILE`. |
| `--csv-out FILE` | Also write the aggregate as CSV to `FILE` (base columns first, then any `extra` keys). |
| `--force` | Overwrite `--json-out`/`--csv-out` if the file already exists. |

```sh
bombercat tags scan -t 10
bombercat tags scan -t 30 --json-out tags.json --csv-out tags.csv --force
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

A transient progress bar (elapsed / timeout, live detection count) shows while sampling and clears before the summary prints. An empty sample prints `no tags detected` instead of an empty table; `--json`/`--csv` still get written (an empty array / header-only file) so scripted runs don't have to special-case a quiet scan. Ctrl-C ends the sample early and summarizes whatever was seen so far — same as `watch`.

---

<a id="tags-info"></a>
### `tags info`

> Report what this DetectTags image can do — mainly, which event mode it speaks.

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

`info` listens for up to 2s after the handshake to catch a `:tag` line if one happens to arrive; it does not itself trigger a scan, so a board that's had nothing presented to it since boot reports `legacy text` even on FW-1 firmware until something is actually tapped. Every `.uf2` published today predates FW-1 and will therefore always report legacy mode — see [Firmwares](../commands/status.md#firmware-capability-table).

---

### Notes

- Requires **DetectTags** firmware (confirm with [`bombercat status`](../commands/status.md)).
- See [Device selection: `-d` / `-p`](../reference.md#device-selection) for selector rules.
- Two things worth knowing:
  - **Every published `.uf2` today parses as legacy text**, not the newer structured `:tag` events — same detections, just without the `extra` fields the structured format can carry. `bombercat tags info` tells you which mode a given board is in.
  - **NFC-B and NFC-F tags print no UID on today's published `.uf2`** — that's a firmware limitation, not a CLI bug. `tags` reports it honestly as `unavailable (NFC-B: firmware prints no ID)` rather than a blank or a made-up value. A `DetectTags.ino` update that extracts the real UID for both (PUPI for NFC-B, IDm for NFC-F) exists in the firmware source but isn't in a published release yet — boards built from that source report the real UID (plus `attrib`/`bitrate` extras) instead.
- See [Troubleshooting](../troubleshooting.md#no-tags-detected) if `watch`/`scan` looks quiet with a card actually on the reader.

---

## See also

- [`readers`](../commands/readers.md) — the mirror image: detect *readers/terminals* instead of *tags*, over the **DetectReaders** firmware's emulated card. Same `read`/`watch`/`scan`/`info` shape.
- [`status`](../commands/status.md) — check or flash the firmware a board needs before running any of these.
