# `bombercat emvy`

> The **EMVyBomberCat** swiss-army knife over its control link — read an EMV card, tunnel raw APDUs to a live card, read a tag's UID, emulate a magstripe swipe, and emulate an NDEF tag or an EMV card while watching every reader/terminal APDU live.

**For authorized security testing and research only.** These commands read and emulate payment cards; use them only on cards and terminals you own or are explicitly authorized to test.

## Quick Start

```sh
# 0. This firmware is built from source, not a bombercat flash release — see below.
#    Confirm the board is running it and see what it exposes:
bombercat emvy info

# 1. Read a physical EMV/contactless card (present it when prompted)
bombercat emvy read
bombercat emvy read --amount 199 --json

# 2. Tunnel a raw APDU to a live card (SELECT PPSE)
bombercat emvy apdu 00A404000E325041592E5359532E444446303100

# 3. Read any tag's protocol/tech/UID
bombercat emvy tag

# 4. Emulate a magstripe swipe (one or both tracks)
bombercat emvy mag --t2 ';4111111111111111=25121010000000000000?'

# 5. Emulate an NDEF tag, watching each reader APDU live (Ctrl-C to stop)
bombercat emvy emu ndef D1010E55016578616D706C652E636F6D

# 6. Read a card into device RAM, then reemulate it to a terminal
bombercat emvy cardscan
bombercat emvy emu card --from-ram

# 7. Diagnose the PN7150 over I2C
bombercat emvy nfcinfo
```

---

## Subcommands

The `emvy` commands live under `bombercat emvy …` and drive a board flashed with the **EMVyBomberCat** firmware. Every subcommand takes the [device selectors](../reference.md#device-selection) (`-d`/`-p`) plus its own `-v`/`--verbose` (see [Global options](../reference.md#global-options) — `-v` also traces the raw `>`/`<` wire protocol here).

### Gating by firmware identity — no auto-flash

Every other command group can reflash a board that's running the wrong firmware ([Auto-flash](../reference.md#auto-flash)). **`emvy` cannot, on purpose.** EMVyBomberCat is a multi-file Arduino sketch built from source with `arduino-cli`; it is **not** a prebuilt `.uf2` in the [firmware releases](https://github.com/ElectronicCats/bombercat-firmware), so there is no image `bombercat flash` could install. Instead, `emvy` gates by **firmware identity**: it opens the control link, checks that `info` reports `fw_name = emvybombercat`, and on a mismatch refuses with a build-and-flash hint rather than reaching for auto-flash:

```
✗ /dev/ttyACM0 is not running EMVyBomberCat (it reports fw_name=nfcgate).
ℹ the `emvy` commands need the EMVyBomberCat firmware — build and flash it
  from bombercat-firmware/EMVyBomberCat with arduino-cli. It is not a prebuilt
  release, so `bombercat flash` cannot install it.
```

Discovery (`ping`/`info`/`identify`) speaks the canonical `+OK`/`-ERR` REPL, so `emvy info` reads it directly. The operative verbs (`read`/`apdu`/`tag`/`mag`/`emu`/`cardscan`/`nfcinfo`) speak EMVyBomberCat's own historical serial dialect (`WAIT`/`APDU:`/`RESP:`/`SCAN`+`JSON_START/END`/`EMU:…`), which the CLI talks through a dedicated client once the identity gate passes.

### The card-engage sequence, and streaming

Two behaviours differ from the other groups and are worth knowing up front:

- **APDU passthrough is a session, not a single verb.** The firmware engages a card on an edge-triggered notification, so [`emvy apdu`](#emvy-apdu) always drives the full `WAIT → APDU: → RELEASE` sequence itself and `RELEASE` always fires on exit (including Ctrl-C or a mid-run error) so the card is never left engaged for the next command.
- **Emulation streams.** [`emvy emu`](#emvy-emu) and its friends are asynchronous: the firmware starts emulating and then streams `EMU:RX`/`EMU:TX`/`EMU:MSG-SENT` progress lines until `EMU:DONE`, your `--timeout`, or Ctrl-C — which sends `STOP` to the board before exiting, so it doesn't keep emulating after the CLI is gone. For a long emulation, Ctrl-C is the expected way to stop it and exits `0`.

---

<a id="emvy-info"></a>
### `emvy info`

> Report the EMVyBomberCat firmware version and state, and the operations the group exposes.

Takes the [device selectors](../reference.md#device-selection). No other options.

```sh
bombercat emvy info
```

```
       EMVyBomberCat @ /dev/ttyACM0
┌─────────┬──────────┐
│ version │ 1.1.0.1  │
│ state   │ idle     │
└─────────┴──────────┘

  operations
  read   EMV card read (SCAN → PAN/expiry/track2/AID)
  apdu   APDU passthrough to a live card (WAIT/APDU/RELEASE)
  tag    tag UID read (TAG)
  mag    magstripe swipe emulation (MAG)
  emu    NDEF tag / EMV-card emulation (EMU/EMUEMV)
  cardscan  read an EMV card into device RAM for reemulation (CARDSCAN)
  nfcinfo   PN7150 I2C diagnostic (NFCINFO)
  reboot    reset the MCU; USB re-enumerates (REBOOT)
```

`state` is the firmware's own: `idle`, `scanning` (a `read`/`cardscan` in progress), `emulating` (an `emu` running), or `hw-error` (NFC/WiFi fault). The `operations` list is the honest surface EMVyBomberCat serves — it is a fixed list, not probed, because the firmware offers no capability query.

Exit code: `0` identified, `1` wrong firmware or link error.

---

<a id="emvy-read"></a>
### `emvy read`

> Read an EMV/contactless card via a full `SCAN`: PAN, expiry, application label, AID, Track 2 equivalent, and (when the flow reaches them) ARQC/ATC/AIP.

| Option | Description |
|---|---|
| `--amount CENTS` | Transaction amount in cents presented to the card during the scan (default `500`). |
| `--json` | Emit the parsed scan result as one JSON object (a **verbatim passthrough** of the firmware's object — the human analysis below is not included). |
| `--raw` | Print every raw line as `SCAN` streams, instead of a parsed table — handy to capture a fixture. |
| `-t`, `--timeout` | Seconds to wait for the scan to complete (default `30`). |

```sh
bombercat emvy read
bombercat emvy read --amount 199
bombercat emvy read --json
```

```
ℹ Waiting for a card on /dev/ttyACM0 (up to 30s)...
EMV card @ /dev/ttyACM0

  PAN           4111111111111111
  expiry        2512
  label         VISA CREDIT
  AID           A0000000031010
  track2        4111111111111111D25121010000000000000F

  standard      ISO 7813 financial
  service code  101 (normal, no restrictions)
  security      ✓ magstripe fallback allowed
```

Present the card once the command starts. The scanned Track 2 that a chip read returns is the **EMV Track 2 equivalent** (tag `57`) — `PAN 'D' YYMM SC discretionary ['F' padding]`, not a magstripe `;…=…?` — and `read` decodes it through the shared core parser to show the card's `standard` and a Service Code `security` verdict (chip/PIN/fallback), the same read-only call [`magspoof show`](magspoof.md#magspoof-show) makes. That analysis block is **presentation only**: `--json` stays an exact passthrough of the firmware's parsed object, so a script reading the JSON sees only the raw firmware keys. If the scanned Track 2 can't be decoded (e.g. a truncated `--raw` capture), the analysis block is simply omitted.

On a scan failure the firmware emits a `# ERROR: …` log (e.g. `# ERROR: Timeout`) and never sends the closing `JSON_END`; `read` surfaces that immediately instead of waiting out `--timeout`:

```
✗ read failed: Timeout
```

Exit code: `0` read, `1` no card / scan error / link error.

> **Field names are not yet fixed by a captured fixture.** Hardware field-capture (Fase 0) is still pending, so the table matches the common EMV fields under a few aliases and falls back to printing any other key the firmware included. Use `--raw` against a real card to capture the exact wire format.

---

<a id="emvy-apdu"></a>
### `emvy apdu`

> Tunnel one or more command APDUs to a live card and print each response, managing the whole `WAIT → APDU: → RELEASE` engage sequence.

| Option | Description |
|---|---|
| `--stdin` | Read one hex APDU per line from stdin, all inside **one** `WAIT`/`RELEASE` session (script an EMV exchange). |
| `--json` | Emit `{"resp": "<hex>"}` per exchange. |
| `-w`, `--wait MS` | Milliseconds `WAIT` arms discovery for (default `30000`). |

Exactly one of an `APDU` argument or `--stdin` is required.

```sh
# One-shot: SELECT PPSE
bombercat emvy apdu 00A404000E325041592E5359532E444446303100

# Scripted exchange, one APDU per line, in a single session
printf '00A404000E325041592E5359532E444446303100\n80A8000002830000\n' \
  | bombercat emvy apdu --stdin
```

```
ℹ Waiting for a card on /dev/ttyACM0 (up to 30s)...
6F1A840E325041592E5359532E4444463031A5088801015F2D02...9000
```

Present the card once the command starts. The argument (or each stdin line) is validated as hex before anything is sent; a bad APDU in a `--stdin` batch is reported and skipped, and the session continues. `RELEASE` **always** fires on exit — a clean run, a mid-run error, or Ctrl-C — so a passthrough never leaves the card engaged for the next command. Firmware-side errors map to a message and a non-zero exit:

```
✗ apdu failed: nocard          # no card engaged within the WAIT window
✗ apdu 00... failed: badapdu   # malformed APDU rejected by the card path
✗ apdu 00... failed: txfail    # RF transmit failure
```

Ctrl-C during a `--stdin` batch is treated as an abort (`⚠ aborted`, exit `1`) — a half-sent APDU batch is a partial failure of what you asked for, unlike an emulation (below), where Ctrl-C is the normal stop.

Exit code: `0` every APDU answered, `1` no card / a bad APDU / link error / aborted.

---

<a id="emvy-tag"></a>
### `emvy tag`

> Read any nearby tag's protocol, technology, and UID.

| Option | Description |
|---|---|
| `--json` | Emit `{"proto": ..., "tech": ..., "uid": ...}`. |

```sh
bombercat emvy tag
```

```
ℹ Waiting for a tag on /dev/ttyACM0 (up to 8s)...
tag @ /dev/ttyACM0

  protocol      ISO-DEP
  tech          A
  UID           04A1B2C3D4E5F6
```

Present the tag once the command starts; the firmware waits up to ~8s. The wire format is `TAG:<proto> TECH:<tech> UID:<hex>` — `tech` is shown when present and omitted otherwise. No tag in the window gives a clean `✗ tag failed: notag`, exit `1`.

Exit code: `0` read, `1` no tag / link error.

---

<a id="emvy-mag"></a>
### `emvy mag`

> Emulate a magstripe swipe of one or both tracks (MagSpoof over the TC4424).

| Option | Description |
|---|---|
| `--t1` | Track 1 data (raw; ISO sentinels `%...?` optional). |
| `--t2` | Track 2 data (raw; ISO sentinels `;...?` optional). |

At least one of `--t1`/`--t2` is required.

```sh
# Two-track (financial) card
bombercat emvy mag \
  --t1 '%B4111111111111111^DOE/JOHN^25121010000000000000?' \
  --t2 ';4111111111111111=25121010000000000000?'

# Single-track (membership/loyalty, or a lone captured track)
bombercat emvy mag --t2 ';4111111111111111=25121010000000000000?'
```

```
✓ 2-track swipe emulated
```

Each track is independently present-or-absent, so a single-track swipe is first-class — no bare `""` placeholder. A track counts as present only when non-empty (matching the firmware's own test), so `--t1 ''` reads as "no track 1".

**Validation here is deliberately laxer than [`magspoof`](magspoof.md).** EMVyBomberCat's `emvyMagPlay` accepts tracks with **or without** their ISO sentinels and tolerates either track being empty, so `emvy mag` only enforces what the firmware actually requires: no newline, no embedded `|` (the `MAG:` field separator), and ≤127 chars per track. It does **not** reuse magspoof's stricter validation, which demands the ISO sentinels that magspoof's own card store requires but this firmware does not. (Both functions are annotated in the source so a future change doesn't unify them by mistake.)

Exit code: `0` played, `1` invalid track / link error.

---

<a id="emvy-cardscan"></a>
### `emvy cardscan`

> Read an EMV card into the device's RAM so it can be reemulated with `emvy emu card --from-ram`.

| Option | Description |
|---|---|
| `--json` | Emit `{"aid": ..., "pan": ..., "exp": ..., "t2": ...}`. |

```sh
bombercat emvy cardscan
```

```
ℹ Waiting for a card on /dev/ttyACM0 (up to ~20s)...
scanned card @ /dev/ttyACM0 (stored in device RAM)

  AID           A0000000031010
  PAN           4111111111111111
  expiry        2512
  track2        4111111111111111D25121010000000000000F

ℹ use `emvy emu card --from-ram` to reemulate it
```

Present the card once the command starts; the firmware polls for ~20s. This is the on-device *scan → RAM → reemulate* flow: the scanned card lives in device RAM until the next scan or reboot, and [`emvy emu card --from-ram`](#emvy-emu) replays it to a terminal. No card gives `✗ cardscan failed: nocard`, a failed EMV read `✗ cardscan failed: scanfail`.

Exit code: `0` scanned, `1` no card / scan failure / link error.

---

<a id="emvy-emu"></a>
## `emvy emu`

> Emulation commands: serve an NDEF tag, or emulate an EMV card to a payment terminal — both streaming every reader/terminal APDU live.

The two subcommands live under `bombercat emvy emu …` and take the [device selectors](../reference.md#device-selection) plus their own `-v`/`--verbose`. Both are **asynchronous streams**: the firmware starts emulating and streams `EMU:*` events until `EMU:DONE`, your `--timeout`, or Ctrl-C. **Ctrl-C is the normal way to stop** — the CLI sends `STOP` to the board and exits `0`. The firmware also self-stops after its own 180s safety cap.

<a id="emvy-emu-ndef"></a>
### `emvy emu ndef`

> Emulate an NFC Forum Type 4 tag serving `HEX` as its NDEF message, streaming every reader APDU.

| Argument / Option | Description |
|---|---|
| `HEX` | The NDEF message as hex. `""` serves an empty tag. |
| `--raw` | Print every raw `EMU:*` line, unparsed. |
| `-t`, `--timeout` | Seconds to bound the whole stream (default: run until `EMU:DONE`, Ctrl-C, or the firmware's 180s auto-stop). |

```sh
# Serve a URI record for example.com
bombercat emvy emu ndef D1010E55016578616D706C652E636F6D
```

```
ℹ emulating an NDEF tag on /dev/ttyACM0 — Ctrl-C to stop
EMU:START mode=ndef
EMU:RX 00A4040007D276000085010100
EMU:TX 9000
EMU:RX 00A4000002E103
...
EMU:MSG-SENT bytes=16
^C
⚠ stopped
```

Present the tag to a reader once the command starts. Each `EMU:*` event is decoded to `EMU:<kind> field=value…`; a line that doesn't parse is shown dimmed as-is, and `--raw` prints the whole stream verbatim (useful for capturing a fixture). Invalid `HEX` is rejected before any session opens.

<a id="emvy-emu-card"></a>
### `emvy emu card`

> Emulate an EMV card to a payment terminal (EMUEMV), streaming every terminal APDU.

| Option | Description |
|---|---|
| `--from-ram` | Reemulate the card most recently stored by [`emvy cardscan`](#emvy-cardscan). |
| `--aid` | AID to announce in the PPSE (hex). |
| `--pan` | PAN to serve in the record (hex BCD). |
| `--exp` | Expiry to serve in the record (hex, `YYMMDD`). |
| `--track2` | Track 2 to serve in the record (hex). |
| `--raw` | Print every raw `EMU:*` line, unparsed. |
| `-t`, `--timeout` | Seconds to bound the whole stream (default: `EMU:DONE`/Ctrl-C/180s auto-stop). |

`--from-ram` and the `--aid`/`--pan`/`--exp`/`--track2` field set are mutually exclusive. With no options, a canned test Visa is served.

```sh
# Canned test Visa
bombercat emvy emu card

# Reemulate the card just captured with `emvy cardscan`
bombercat emvy emu card --from-ram

# Inject a specific captured card
bombercat emvy emu card --aid A0000000031010 --pan 4111111111111111 --exp 251231
```

```
ℹ emulating an EMV card on /dev/ttyACM0 — Ctrl-C to stop
EMU:START mode=emv
EMU:RX 00A404000E325041592E5359532E444446303100    # SELECT PPSE
EMU:TX 6F1A...9000
EMU:RX 80A8000002830000                             # GET PROCESSING OPTIONS
...
^C
⚠ stopped
```

Present the emulated card to a terminal once the command starts. Terminal APDUs are decoded live (`SELECT-PPSE`/`SELECT-AID`/`GPO`/`READ-RECORD`/`GENERATE-AC`). **This is not a working card** — it never returns a valid cryptogram; the point is to profile or fuzz a terminal by seeing exactly what it asks for. Each hex field is validated before the session opens (empty = firmware default).

---

<a id="emvy-nfcinfo"></a>
### `emvy nfcinfo`

> Diagnose the PN7150 over I2C and re-arm discovery.

Takes the [device selectors](../reference.md#device-selection). No other options.

```sh
bombercat emvy nfcinfo
```

```
✓ PN7150 alive @ /dev/ttyACM0 — firmware version 12
```

A live chip answers with its firmware version — if that's what you get, the I2C link is fine, so a "no card" symptom is an RF problem (card, antenna, placement), not wiring. A dead/unreachable chip prints the firmware's raw error line instead. As a side effect the firmware re-arms tag discovery, which can clear a stuck scan state.

---

<a id="emvy-reboot"></a>
### `emvy reboot`

> Reset the board's MCU.

Takes the [device selectors](../reference.md#device-selection). No other options.

```sh
bombercat emvy reboot
```

```
⚠ /dev/ttyACM0 is rebooting — it will re-enumerate over USB; reconnect before
  the next emvy command.
```

The USB CDC re-enumerates on reset, so **this port cannot be reused** and a subsequent `emvy` command may need a different `--port` (re-check with `bombercat device list`). No reply is expected — the board resets before it could send one — so the command just fires `REBOOT` and returns.

---

## Notes

- **Built from source, no auto-flash.** EMVyBomberCat is compiled with `arduino-cli` from `bombercat-firmware/EMVyBomberCat`, not published as a `.uf2` release, so `bombercat flash` cannot install it and `emvy` never auto-flashes. See [Gating by firmware identity](#gating-by-firmware-identity--no-auto-flash) and [limitations](../limitations.md#emvybombercat-is-built-from-source).
- **Fixed NFC pins.** The PN7150 uses the BomberCat defaults (`IRQ=11`, `VEN=13`, I2C `0x28`); no extra wiring.
- **Fixtures pending hardware.** The exact wire format of `SCAN`'s JSON and `TAG:` is confirmed by reading the firmware source, not yet by a captured fixture on real hardware. `emvy read --raw` / `emvy tag --json` against a real card/tag are the way to pin those down — see the [implementation plan](../IMPLEMENTATION_PLAN_EMVyBomberCat_CLI.md)'s Progress Log (Fase 0).
- **Authorized testing only.** Reading, tunnelling to, and emulating payment cards is for cards/terminals you own or are explicitly authorized to test.

## Credits

The **EMVyBomberCat** firmware was contributed by [**Glitchboi-sudo**](https://github.com/Glitchboi-sudo) in [ElectronicCats/bombercat-firmware#4](https://github.com/ElectronicCats/bombercat-firmware/pull/4). These `emvy` CLI commands drive that firmware over its control link.
