# `bombercat readers`

> NFC reader/terminal detection over the **DetectReaders** firmware's PN7150 emulated card.

## Quick Start

```sh
bombercat status                    # confirm DetectReaders is flashed (or flash it)
bombercat flash DetectReaders -d 1  # if it isn't

bombercat readers read              # wait for one reader, print fingerprint and exit
bombercat readers watch             # stream detections until Ctrl-C
bombercat readers scan -t 20        # sample for 20s, print aggregated summary
bombercat readers info              # firmware version + whether events have been seen
```

The board runs the PN7150 in card-emulation (LISTEN) mode, presenting an emulated contactless card. Any reader/terminal that activates the field is reported as a structured `:reader` event — always structured, there is no legacy text mode to fall back to. The first APDU the reader sends is fingerprinted: a PPSE SELECT identifies an EMV payment terminal (`emv-payment`), well-known AIDs identify payment apps (`visa`, `mastercard`, `amex`) or NDEF readers (`ndef`); anything else is `unknown`, with the selected AID surfaced separately when the command was a SELECT.

---

## Subcommands

All subcommands take the [device selectors](../reference.md#device-selection) plus their own `-v`/`--verbose` (see [Global options](../reference.md#global-options)).

Like [`tags`](../commands/tags.md), the `readers` commands verify the control handshake before doing anything:

```
✗ /dev/ttyACM0 did not answer the handshake. `readers` needs the
  DetectReaders firmware — check what's flashed with:  bombercat status
```

---

### `readers read`

> Wait for one reader/terminal to probe the emulated card.

| Option | Description |
|---|---|
| `-t, --timeout SEC` | Seconds to wait for a reader (default `15`). |
| `--json` | Emit one JSON object on stdout instead of the field table. |

```sh
bombercat readers read
bombercat readers read -t 30 --json
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

`--json` prints a single clean object on stdout with every field, `extra` merged in flat:

```json
{"ts_ms": 1234, "tech": "NFC-A", "protocol": "ISODEP", "intf": "ISODEP", "apdu": "00A404000E325041592E5359532E4444463031", "aid": null, "label": "emv-payment", "n": 3}
```

No reader within the timeout is exit code `1`:

```
✗ no reader detected in 15s
```

---

### `readers watch`

> Stream reader detections continuously. Ctrl-C to stop and print a summary.

| Option | Description |
|---|---|
| `--dedupe` | Collapse repeat detections of the same fingerprint into a `seen again (xN)` line instead of reprinting the row. Two distinct terminals sharing a label (e.g. two EMV readers, both `emv-payment`) share a fingerprint too, so the counter can mix them. |
| `--quiet-noise` / `--no-quiet-noise` | Hide firmware boot/idle chatter — `Waiting for a Reader …`, `Re-arm: …`, `Re-armed. …` (default: hidden). |
| `--json` | Emit one JSON object per line instead of the formatted line. |

```sh
bombercat readers watch --dedupe
bombercat readers watch --json
```

```
ℹ Watching /dev/ttyACM0 — Ctrl-C to stop
[12:26:48] emv-payment   NFC-A   ISODEP     00 A4 04 00 0E 32 50 41 59 2E 53 59 53 2E 44 44 46 30 31
  ↳ emv-payment seen again (x2)
[12:27:03] visa          NFC-A   ISODEP     00 A4 04 00 07 A0 00 00 00 03 10 10

ℹ 3 detections, 2 unique fingerprints, 41s
```

Without `--dedupe`, a repeat detection just prints another line. With `-v`/`--verbose`, the boot/idle noise always shows regardless of `--quiet-noise` — that flag only controls the *default* (non-verbose) view. `--json` emits one object per detection and skips the noise lines and the closing summary, so the stream stays valid NDJSON.

---

### `readers scan`

> Sample reader detections for a while and print an aggregated summary.

Repeat detections of the same fingerprint collapse into one row with a count and a first/last time seen (elapsed seconds since the scan started).

| Option | Description |
|---|---|
| `-t, --timeout SEC` | Seconds to sample for (default `30`). |
| `--json-out FILE` | Also write the aggregate as a JSON array to `FILE`. |
| `--csv-out FILE` | Also write the aggregate as CSV to `FILE` (base columns first, then any `extra` keys). |
| `--force` | Overwrite `--json-out`/`--csv-out` if the file already exists. |

```sh
bombercat readers scan -t 30
bombercat readers scan -t 10 --json-out readers.json --csv-out readers.csv
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

The full APDU is left out of the printed table (it can run to hundreds of hex characters) but is still written to `--json-out`/`--csv-out`. A transient progress bar shows while sampling and clears before the summary prints. An empty sample prints `no readers detected` instead of an empty table. Ctrl-C ends the sample early and summarizes whatever was seen so far — same as `watch`.

---

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

`info` listens for up to 2s after the handshake to catch a `:reader` line if one happens to arrive; it does not itself arm anything extra, so a board that hasn't had a reader presented to it since boot reports `no ':reader' events seen yet` until something actually probes it. `state` is `listening` while armed and `reader-detected` for the duration of an active detection session.

---

### Notes

- Requires **DetectReaders** firmware (confirm with [`bombercat status`](../commands/status.md)).
- Unlike [`tags`](../commands/tags.md), there is no legacy text mode — DetectReaders was born speaking the structured `:reader` event line, so every published `.uf2` parses the same way.
- The fingerprint is driven by the first APDU: `emv-payment`, `visa`, `mastercard`, `amex`, `ndef`, or `unknown` (with AID surfaced for SELECT commands). A reader that never sends an APDU (RF-layer activation only) still gets classified `unknown` with `apdu=-`.

---

## See also

- [`tags`](../commands/tags.md) — the mirror image: detect *tags* instead of *readers/terminals*, over the **DetectTags** firmware's PN7150 reader. Same `read`/`watch`/`scan`/`info` shape.
- [`status`](../commands/status.md) — check or flash the firmware a board needs before running any of these.
