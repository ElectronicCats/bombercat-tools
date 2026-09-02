# `bombercat magspoof`

> Magstripe emulation control over the **magspoof** firmware's REPL — play a swipe, inspect what's loaded, watch reproductions live, and manage a flash-resident multi-card store.

## Quick Start

```sh
# 1. Flash magspoof firmware
bombercat flash magspoof

# 2. Inspect what's loaded
bombercat magspoof show

# 3. Emulate a swipe (same as pressing the physical button)
bombercat magspoof play

# 4. Watch reproductions live
bombercat magspoof watch

# 5. Manage cards in the persistent store
bombercat magspoof card add visa \
  --t1 '%B4111111111111111^DOE/JOHN^25121010000000000000?' \
  --t2 ';4111111111111111=25121010000000000000?'
bombercat magspoof card list
bombercat magspoof card select visa

# 6. NFC VISA contactless emulation
bombercat magspoof nfc visa

# 7. Read a physical EMV/Visa card over NFC and store it
bombercat magspoof nfc read mycard

# 8. Normalize Service Code for magstripe fallback (FOR AUTHORIZED TESTING ONLY)
bombercat magspoof card normalize-sc visa --apply

# 9. Restore/harden the Service Code (undo a normalize-sc)
bombercat magspoof card require-sc visa --apply
```

---

## Subcommands

The `magspoof` commands live under `bombercat magspoof …`. They need a board flashed with **magspoof** (confirm with [`bombercat status`](../commands/status.md) — this is a different image from `MagspoofCVSAttack`/`MagSpoofMqtt`, which share the same boot output but don't carry this REPL surface) and, like `tags`/`readers`, verify the control handshake before doing anything:

```
✗ /dev/ttyACM0 did not answer the handshake. `magspoof` needs the MagSpoof
  firmware — check what's flashed with:  bombercat status
```

Every subcommand takes the [device selectors](../reference.md#device-selection) plus its own `-v`/`--verbose` (see [Global options](../reference.md#global-options) — `-v` traces the raw wire protocol here too).

Unlike `tags`/`readers` (passive observers), `play` and the write-subcommands under `card` actively drive the board. There's no aggregator/scan mode here: a reproduction (`:mag` event) is discrete and just gets counted by [`watch`](#magspoof-watch).

An old **magspoof** image — one predating the `magplay`/`magset`/`magget`/`magbtn` REPL hook, indistinguishable by version alone since it's still `1.1.1.0` — answers every one of these commands with a generic `-ERR unknown command`. Every subcommand below recognises that specific reply and points at a reflash instead of just failing:

```
✗ play failed: unknown command
ℹ this firmware predates magplay/magset/magget/magbtn — reflash the magspoof
  image (bombercat flash magspoof) to use it.
```

---

<a id="magspoof-play"></a>
### `magspoof play`

> Emulate a swipe of the active card — the same effect as pressing the physical button.

Takes the [device selectors](../reference.md#device-selection). No other options.

```sh
bombercat magspoof play
```

```
✓ played track 1
```

A two-track (financial) card plays track 1 forward then track 2 in reverse — what a reader sees on a real swipe; a single-track (membership/loyalty) card plays just the track it carries. The firmware picks which by itself: the CLI always sends a bare `magplay`. Reproduction blocks the device for ~0.6–1.5s (worst case) before the reply arrives — that delay is normal, not a stall.

Exit code: `0` played, `1` error (including old firmware without `magplay`).

---

<a id="magspoof-show"></a>
### `magspoof show`

> Print the tracks currently loaded on the device, decoded.

| Option | Description |
|---|---|
| `--json` | Emit `{"t1": ..., "t2": ..., "btn": ..., "analysis": {...}}`. |
| `--verbose-analysis` / `--no-verbose-analysis` | Show the detected standard (ISO 7813 financial, PBOC/UnionPay, AAMVA driver's license, loyalty/transit) and Service Code analysis (chip/PIN/fallback) for financial cards. Default: shown. |

```sh
bombercat magspoof show
bombercat magspoof show --json
bombercat magspoof show --no-verbose-analysis
```

```
magspoof card @ /dev/ttyACM0

  track 1       %B4111111111111111^DOE/JOHN^25121010000000000000?
  track 2       ;4111111111111111=25121010000000000000?
  button        alternating 1 and 2

  ── analysis ──────────────────────────────
  standard      ISO 7813 financial (payment card)
  cardholder    DOE/JOHN
  PAN           4111 1111 1111 1111
  expires       12/25
  service code  201 → 101
  security      ⚠ chip required — swipe may be refused
  chip required yes
  PIN required  no

ℹ use: bombercat magspoof card normalize-sc --apply
```

`standard` is one of ISO 7813 financial (payment card), PBOC / UnionPay (financial), AAMVA driver's license / ID, loyalty / transit / generic, or unknown. The `security` badge and the `use:` recommendation only appear when a financial card's Service Code demands chip and/or PIN — [`card normalize-sc`](#magspoof-card-normalize-sc) is what it points at. `--json` always includes the raw `t1`/`t2`/`btn`; with `--no-verbose-analysis` the `analysis` key is simply omitted from the payload.

---

<a id="magspoof-watch"></a>
### `magspoof watch`

> Stream every reproduction — triggered by `magplay` or the physical button — until Ctrl-C, then print a summary.

| Option | Description |
|---|---|
| `--quiet-noise` / `--no-quiet-noise` | Hide firmware boot/idle chatter (`Activating MagSpoof...`, `Default tracks: ...`, `Track 1/2: ...`, `Updated tracks:`, `Press the MagSpoof button`). Default: hidden. |
| `--json` | Emit one JSON object per line instead of the formatted line. |

```sh
bombercat magspoof watch
bombercat magspoof watch --json
```

```
ℹ Watching /dev/ttyACM0 — Ctrl-C to stop
▶ track 1 @ 2.3s
▶ track 2 @ 2.3s

ℹ 2 plays, track 1 ×1, track 2 ×1, 12s
```

Every reproduction shows up here — a `magspoof play` from this CLI, one from another client, or the physical button — since it's the firmware's `:mag` event that drives the line, not the CLI's own request. `--json` emits one object per event (`{"ts_ms": ..., "track": ..., ...}`, any extra `k=v` fields merged in flat) and skips the noise lines and the closing summary, so the stream stays valid NDJSON. Ctrl-C stops cleanly and still prints the summary (outside `--json`).

---

<a id="magspoof-info"></a>
### `magspoof info`

> Report the firmware version and state, and whether a `:mag` event has been seen yet.

```sh
bombercat magspoof info
```

```
        magspoof @ /dev/ttyACM0
┌─────────┬────────────────────────────┐
│ version │ 1.1.1.0                    │
│ events  │ structured (':mag' events) │
│ state   │ idle                       │
└─────────┴────────────────────────────┘
```

Like [`tags info`](../commands/tags.md#tags-info)/[`readers info`](../commands/readers.md#readers-info), `info` listens for up to 2s after the handshake to catch a `:mag` line if one happens to arrive; it never triggers a reproduction itself, so a board that hasn't played anything since boot reports `no ':mag' events seen yet` until something actually plays.

---

## `magspoof nfc`

> PN7150 contactless commands: force the advertised chip mode, emulate a VISA MSD tap, read a physical EMV/Visa card over NFC, and check NFC status.

The four subcommands live under `bombercat magspoof nfc …` and take the [device selectors](../reference.md#device-selection) plus their own `-v`/`--verbose`.

<a id="magspoof-nfc-selres"></a>
### `magspoof nfc selres`

> Set the PN7150's SEL_RES chip bit.

```sh
bombercat magspoof nfc selres chip     # advertise ISO-DEP/EMV support (0x33)
bombercat magspoof nfc selres nochip   # force MSD magstripe fallback (0x13)
```

```
✓ SEL_RES set to nochip
```

This is a manual override: it applies immediately, but is reset back to the current mode's own default (chip for reader mode, nochip for emulation) the next time the device's NFC reset runs — so it doesn't survive a mode switch. To pin the mode per-card instead, use [`card set --nfc`](#magspoof-card-set).

---

<a id="magspoof-nfc-visa"></a>
### `magspoof nfc visa`

> Start a VISA MSD contactless emulation session for the active card.

```sh
bombercat magspoof nfc visa
```

```
ℹ waiting for a contactless tap (up to 15s)...
✓ VISA MSD emulation complete
```

Switches the PN7150 into emulation mode (SEL_RES forced to `nochip`, so the terminal falls back to magstripe instead of attempting EMV crypto) and emulates the active card's Track 2 — or a built-in fallback token if the active card has none — through the PPSE/VISA-AID/GPO/READ-RECORD exchange. Blocks up to 15s waiting for the tap; tap the BomberCat to a contactless reader once the command starts.

---

<a id="magspoof-nfc-read"></a>
### `magspoof nfc read`

> Read a physical EMV/Visa card's Track 2 over NFC and store it on `NAME`.

```sh
bombercat magspoof nfc read visacard
```

```
ℹ waiting for a card (up to 8s)...
✓ created visacard — stored track 2: ;4111111111111111=25121010000000000000?
```

`NAME` picks new-vs-existing: an existing card gets just its Track 2 updated (any Track 1 it holds is left untouched); a name not yet in the store is created fresh with the scanned Track 2. Present the card once the command starts — it switches the PN7150 into reader mode, waits up to 8s for a card to enter the field, then runs the PPSE/VISA-AID/GPO/READ-RECORD sequence once.

---

<a id="magspoof-nfc-info"></a>
### `magspoof nfc info`

> Report the PN7150's firmware version, RF role, SEL_RES state and the last tag it saw.

```sh
bombercat magspoof nfc info
```

```
      magspoof NFC @ /dev/ttyACM0
┌────────────────┬───────────────────┐
│ PN7150 firmware │ 4.11              │
│ mode            │ reader            │
│ SEL_RES         │ chip              │
│ last tag seen   │ yes               │
│ UID             │ 04:1A:2B:3C       │
└─────────────────┴───────────────────┘
```

A pure status read — unlike `nfc visa`/`nfc read` it never switches mode or waits for a tag, so `last tag seen`/`UID` reflect the last detection (from a prior `nfc read`, or the reader-mode bring-up at boot), not a fresh probe. `UID` is only shown when a tag has been seen.

---

## `magspoof card`

> Manage the persistent multi-card store (requires flash-storage firmware — every published `magspoof.uf2` today has it).

Cards live in the board's flash and survive a reset or reflash. The **active** card is what [`magspoof play`](#magspoof-play)/[`magspoof show`](#magspoof-show) and the physical button act on — `card select` switches it. The nine subcommands live under `bombercat magspoof card …` and take the [device selectors](../reference.md#device-selection) plus their own `-v`/`--verbose`; wherever a command takes `NAME`, it tab-completes against the board's stored cards. Older firmware without the store answers with `-ERR unknown command` (see above) — reflash with `bombercat flash magspoof`.

---

<a id="magspoof-card-list"></a>
### `magspoof card list`

> List every stored card, marking the active one.

| Option | Description |
|---|---|
| `--json` | Emit one JSON object per card: `{"name", "t1", "t2", "type", "active"}`. |

```sh
bombercat magspoof card list
bombercat magspoof card list --json
```

```
                     magspoof cards @ /dev/ttyACM0
┏━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┓
┃   ┃ name        ┃ type      ┃ track 1                ┃ track 2             ┃
┡━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━┩
│ ● │ visacard    │ financial │ %B4111111111111111^…  │ ;4111111111111111=…│
│   │ membership1 │ loyalty   │ —                      │ ;6001112223334445…│
└───┴─────────────┴───────────┴────────────────────────┴─────────────────────┘
```

The raw tracks are pre-clipped to a fixed width so the extra `type` column never squeezes them to an unreadable stub; the untruncated data is in `--json`/[`card get`](#magspoof-card-get). An empty store still prints the table shell with `no cards stored` rather than an empty grid.

---

<a id="magspoof-card-add"></a>
### `magspoof card add`

> Add a new card `NAME` with one or both tracks.

| Option | Description |
|---|---|
| `--t1 DATA` | Track 1 data (starts with `%`, ends with `?`). |
| `--t2 DATA` | Track 2 data (starts with `;`, ends with `?`). |
| `--normalize-sc` / `--no-normalize-sc` | Auto-normalize `--t2`'s Service Code for magstripe fallback (no chip/PIN) before writing it. **FOR AUTHORIZED TESTING ONLY.** Default: off. |

At least one of `--t1`/`--t2` is required — a two-track financial card gives both, a single-track membership/loyalty card gives just the one it carries.

```sh
bombercat magspoof card add visacard \
  --t1 '%B4111111111111111^DOE/JOHN^25121010000000000000?' \
  --t2 ';4111111111111111=25121010000000000000?'
bombercat magspoof card add membership1 --t2 ';6001112223334445=2512?'
bombercat magspoof card add visacard --t1 '...' --t2 '...' --normalize-sc
```

```
✓ added 2-track card visacard
✓ added 1-track card membership1
✓ added 2-track card visacard (service code normalized)
```

Each track is validated locally first (ISO sentinel `%…?`/`;…?`, max 126 chars, no newline) so a malformed track never makes the serial round trip. The card is created and its track(s) written in one command; if a track write fails partway through, the empty card is deleted so a failed `add` leaves no trace. With `--normalize-sc`, the confirmation notes whether the Service Code actually changed.

---

<a id="magspoof-card-del"></a>
### `magspoof card del`

> Delete the card called `NAME`.

```sh
bombercat magspoof card del membership1
```

```
✓ deleted card membership1
```

---

<a id="magspoof-card-set"></a>
### `magspoof card set`

> Update one or both tracks, and/or the SEL_RES preference, of an existing card.

| Option | Description |
|---|---|
| `--t1 DATA` | New track 1 (starts with `%`, ends with `?`). |
| `--t2 DATA` | New track 2 (starts with `;`, ends with `?`). |
| `--nfc [chip\|nochip]` | SEL_RES preference for this card: `chip` advertises ISO-DEP/EMV support during `nfc visa`/`nfc read`, `nochip` forces MSD fallback. Consulted whenever this card is active; a card with no preference falls back to the mode's own default. |
| `--normalize-sc` / `--no-normalize-sc` | Auto-normalize `--t2`'s Service Code before writing it. **FOR AUTHORIZED TESTING ONLY.** Default: off. |

At least one of `--t1`/`--t2`/`--nfc` is required.

```sh
bombercat magspoof card set visacard --nfc nochip
bombercat magspoof card set visacard --t2 ';4111111111111111=25121010000000000000?'
bombercat magspoof card set visacard --t1 '...' --t2 '...' --nfc chip --normalize-sc
```

```
✓ updated nfc nochip on visacard
✓ updated track 2 on visacard
✓ updated track 1 and track 2 and nfc chip on visacard (service code normalized)
```

Track data is validated locally the same way as `card add` before the serial round trip. To normalize a card's *already-stored* Track 2 (instead of one you're writing now), use [`card normalize-sc --apply`](#magspoof-card-normalize-sc).

---

<a id="magspoof-card-select"></a>
### `magspoof card select`

> Make `NAME` the active card (persisted across resets).

```sh
bombercat magspoof card select visacard
```

```
✓ active card is now visacard
```

---

<a id="magspoof-card-get"></a>
### `magspoof card get`

> Show a card's tracks (the active card when `NAME` is omitted).

| Option | Description |
|---|---|
| `--json` | Emit `{"name": ..., "t1": ..., "t2": ..., "nfc": ..., "active": ...}`. |

```sh
bombercat magspoof card get visacard
bombercat magspoof card get
bombercat magspoof card get --json
```

```
  name          visacard
  active        yes
  track 1       %B4111111111111111^DOE/JOHN^25121010000000000000?
  track 2       ;4111111111111111=25121010000000000000?
  nfc selres    nochip
```

`nfc selres` reads `—` on firmware predating the SEL_RES-preference field (empty `nfc` field in the response). On firmware that has the field, a card with no preference set reads `default` instead (verified against a `magspoof` v1.2.4.0 board), not `—`.

---

<a id="magspoof-card-info"></a>
### `magspoof card info`

> Show store stats: card count, capacity, active card and button mode.

```sh
bombercat magspoof card info
```

```
        magspoof store @ /dev/ttyACM0
┌─────────┬──────────────────────────┐
│ cards   │ 2 / 8                    │
│ active  │ visacard                 │
│ button  │ alternating 1 and 2      │
└─────────┴──────────────────────────┘
```

---

<a id="magspoof-card-normalize-sc"></a>
### `magspoof card normalize-sc`

> Normalize a stored card's Track 2 Service Code for magstripe fallback (the active card when `NAME` is omitted).

| Option | Description |
|---|---|
| `--apply` | Write the normalized Track 2 back to the card. Without it, this only previews the change. |
| `--remove-chip` / `--no-remove-chip` | Clear the chip requirement (Service Code 1st digit 2/6 → 1). Passed alone (with `--remove-pin` omitted), only the chip requirement is cleared. |
| `--remove-pin` / `--no-remove-pin` | Clear the PIN requirement (Service Code 3rd digit 6 → 1). Passed alone (with `--remove-chip` omitted), only the PIN requirement is cleared. |
| `--json` | Emit `{"name", "service_code", "service_code_normalized", "track2", "track2_normalized", "is_ic_card", "requires_pin"}`. |

Passing neither `--remove-chip`/`--remove-pin` clears both requirements (the default). Passing exactly one of them clears only that one, leaving the other alone; `--no-remove-chip` or `--no-remove-pin`, passed alone, has the same effect — it isolates the clear to the *other* field.

```sh
bombercat magspoof card normalize-sc visacard
bombercat magspoof card normalize-sc visacard --apply
bombercat magspoof card normalize-sc visacard --remove-pin --apply
bombercat magspoof card normalize-sc visacard --json
```

```
  name              visacard
  service code      201 → 101
  chip required     yes
  PIN required      no

ℹ use --apply to write the normalized track 2 to the card
```

```
✓ service code normalized on visacard: 201 → 101
```

A card whose Service Code is already normalized reports so and writes nothing, with or without `--apply`.

> **FOR AUTHORIZED TESTING ONLY** — this deliberately weakens the card's stated security requirements (chip/PIN enforcement) so a swipe falls back to plain magstripe.

---

<a id="magspoof-card-require-sc"></a>
### `magspoof card require-sc`

> Set a stored card's Track 2 Service Code to demand a chip and/or a PIN (the active card when `NAME` is omitted) — the inverse of [`card normalize-sc`](#magspoof-card-normalize-sc).

| Option | Description |
|---|---|
| `--apply` | Write the hardened Track 2 back to the card. Without it, this only previews the change. |
| `--require-chip` / `--no-require-chip` | Set the chip requirement (Service Code 1st digit 1/5 → 2/6). Passed alone (with `--require-pin` omitted), only the chip requirement is set. |
| `--require-pin` / `--no-require-pin` | Set the PIN requirement (Service Code 3rd digit → 6). Passed alone (with `--require-chip` omitted), only the PIN requirement is set. |
| `--json` | Emit `{"name", "service_code", "service_code_hardened", "track2", "track2_hardened", "is_ic_card", "requires_pin"}`. |

Passing neither `--require-chip`/`--require-pin` sets both requirements (the default). Passing exactly one of them sets only that one, leaving the other alone; `--no-require-chip` or `--no-require-pin`, passed alone, has the same effect — it isolates the set to the *other* field.

```sh
bombercat magspoof card require-sc visacard
bombercat magspoof card require-sc visacard --apply
bombercat magspoof card require-sc visacard --require-chip --apply
bombercat magspoof card require-sc visacard --json
```

```
  name              visacard
  service code      101 → 201
  chip required     no
  PIN required      no

ℹ use --apply to write the hardened track 2 to the card
```

```
✓ service code hardened on visacard: 101 → 201
```

A card whose Service Code already meets the requested requirements reports so and writes nothing, with or without `--apply`. Useful to restore a card [`normalize-sc`](#magspoof-card-normalize-sc) weakened earlier, or to test how a terminal reacts to a stricter Service Code.

---

### Notes

- Requires **magspoof** firmware (confirm with [`bombercat status`](../commands/status.md)).
- See [Device selection: `-d` / `-p`](../reference.md#device-selection) for selector rules.
- Card names tab-complete against the board's stored cards (requires `--port`/`-d` to target the right board).
- The `--normalize-sc` flag (on `card add`, `card set`, and `card normalize-sc`) is **FOR AUTHORIZED TESTING ONLY** — it weakens the card's stated security requirements. [`card require-sc`](#magspoof-card-require-sc) reverses it.
