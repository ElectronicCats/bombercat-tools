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

The `tags` commands live under `bombercat tags …`. They need a board flashed with **DetectTags**, and check for it before doing anything else. A board running something else doesn't just fail — it's offered a fix, or a clean error if declined ([Auto-flash](../reference.md#auto-flash)):

```
✗ `tags` needs DetectTags; this board is running magspoof.
    bombercat flash DetectTags
```

A board that answers **nothing at all** — no candidate firmware, wrong port, unplugged — is the older, separate error:

```
✗ nothing responded on /dev/ttyACM0.
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
  atqa          0004
  sak           08
  model         MIFARE Classic 1K
```

`atqa`, `sak` and `model` are resolved **host-side** from the `SENS RES` /
`SEL RES` lines the firmware already prints — in **both** event modes, so they
work on the published `.uf2` you have today ([see below](#chip-model)).

`--json` prints a single clean object on stdout (nothing else touches stdout, so it's pipeable) with every field, `extra` merged in flat:

```json
{"uid": "041A2B3C", "tech": "NFC-A", "protocol": "T2T", "ts_ms": 1234, "atqa": "0004", "sak": "08", "model": "MIFARE Classic 1K"}
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
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━┳━━━━━━┓
┃ UID                          ┃ Tech  ┃ Protocol ┃ Model                    ┃ Count ┃ First ┃ Last ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━╇━━━━━━┩
│ 04:1A:2B:3C:4D:5E:6F         │ NFC-A │ T2T      │ MIFARE Ultralight / NTAG │     2 │  0.0s │ 4.1s │
│ unavailable (NFC-B: firmware │ NFC-B │ ISODEP   │ —                        │     1 │  6.7s │ 6.7s │
│ prints no ID)                │       │          │                          │       │       │      │
└──────────────────────────────┴───────┴──────────┴──────────────────────────┴───────┴───────┴──────┘
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

### `tags mifare`

> Mifare Classic auth/read/write/sector/check/dump/analyze/restore/code/write-text commands. Requires the **MifareClassic** firmware (not DetectTags — confirm with [`bombercat status`](../commands/status.md)). `analyze` is the exception — it runs fully offline on a saved dump and needs no firmware or card.

Tap a Mifare Classic card to let the firmware select it — `auth`, `read`, `write`, `sector`, `check`, `dump`, `restore` and `write-text` all wait for this automatically (default 5s, `-t/--timeout` to change it — `write-text` defaults to `15`) if no card is selected yet. The card then stays selected for ~10s of inactivity between commands before the firmware closes the session and re-arms discovery.

```sh
bombercat tags mifare keys                                            # list built-in default keys, no card needed
bombercat tags mifare auth --block 4 --key-type A --key FFFFFFFFFFFF  # authenticate a sector
bombercat tags mifare read --block 4                                  # read a block from the authenticated sector
bombercat tags mifare write --block 4 --data 00112233445566778899AABBCCDDEEFF
bombercat tags mifare sector --sector 1 --key-type A --key FFFFFFFFFFFF --json
bombercat tags mifare code "carnet: 12345" --sector 1                 # offline: text → block hex
bombercat tags mifare write-text "carnet: 12345" --sector 1 -k card.keys  # encode + write it to the card
```

`auth`/`read`/`write`/`sector` take `--block`/`--sector` (numeric), `--key`/`--key-type` where relevant (12 hex chars, `A` or `B`), and `--json` on the ones that return data. `read` and `write` need a sector already authenticated by `auth`; `sector` does auth + read of every block in one self-contained call. `sector` also accepts `-k, --keys-file FILE` instead of `--key`/`--key-type` — a `sector:keyA:keyB` file from `mifare check --output-keys` (tries key A, falls back to key B, and shows the real keys in the trailer line instead of the zeros the card reads back).

---

<a id="tags-mifare-check"></a>
### `tags mifare check`

> Dictionary attack: try known keys against every sector and report which ones open. This is the "check keys" phase of `mfoc`/`hf mf chk` — it demonstrates the well-known weakness of Mifare Classic, that most cards in the wild still answer to default or leaked vendor keys.

> ⚠️ **Authorized use only.** Test only cards you own or have explicit permission to test.

| Option | Description |
|---|---|
| `--keys FILE` | Key dictionary (`.keys`/`.dic`/`.md`), repeatable. Defaults to the bundled dictionary (2477 known keys); passing `--keys` **replaces** the bundled one — include it explicitly alongside your own file if you want both. |
| `--dict NAMES` | Named key families to add **on top** of the base dictionary, comma-separated and repeatable (e.g. `--dict mad,transport`). Known: `transport`, `mad`, `ndef`, `locks`, or `all`. Their higher-probability keys are tried before the base dictionary; MAD keys are tried first on the MAD sectors (0/16). See `--list-dicts`. |
| `--dict-file FILE` | Extra `.dic`/`.keys` file to add as candidates (repeatable), at the lowest/generic priority. Unlike `--keys`, this **adds** to the base dictionary instead of replacing it. |
| `--list-dicts` | List the registered named key families and exit (no device needed). |
| `--uid-derived NAMES` | Also try keys derived from the card UID by a **public** diversification scheme, comma-separated and repeatable. Known: `mizip` (MiZip / MIFARE Mini), or `all`. Off by default; needs the UID (read from block 0). Secret-key (AES/HMAC) schemes are out of scope and refuse. See `--list-uid-schemes`. |
| `--uid-max N` | Cap UID-derived candidates tried per sector (`0` = no cap). Bounds the extra authentications over the slow I²C link. |
| `--list-uid-schemes` | List the registered UID-derived key schemes and exit (no device needed). |
| `--sectors N` | Number of sectors to check (default `16` = 1K). Max `32`; 4K's 16-block sectors (32–39) aren't supported yet. |
| `--key-type A\|B\|both` | Which key slot(s) to try (default `both`). |
| `--json` | Emit one JSON object on stdout instead of the table. |
| `--out FILE` | Write the recovered keys as a keyfile (one 12-hex key per line, mfoc/proxmark-compatible). |
| `-o, --output-keys FILE` | Write the recovered keys as one `sector:keyA:keyB` line per sector — the format [`mifare sector --keys-file`](#tags-mifare), [`mifare dump -k`](#tags-mifare-dump), [`mifare analyze -k`](#tags-mifare-analyze), [`mifare code -k`](#tags-mifare-code) and [`mifare write-text -k`](#tags-mifare-write-text) all read. A key type not recovered is left blank. |
| `--force` | Overwrite `--out`/`--output-keys` if it already exists. |
| `-t, --timeout SEC` | Seconds to wait for a card tap if none is selected yet (default `5`). |

```sh
bombercat tags mifare check
bombercat tags mifare check --sectors 16 --key-type A --out recovered.keys
bombercat tags mifare check -o card.keys           # sector:keyA:keyB file for sector/dump/analyze/code/write-text
bombercat tags mifare check --dict mad,transport   # add named families on top of the base dictionary
bombercat tags mifare check --uid-derived mizip    # + try public UID-derived keys (MiZip/MIFARE Mini)
bombercat tags mifare check --list-dicts           # inspect the named families, no device needed
bombercat tags mifare check --keys mine.dic --json
```

```
ℹ Checking /dev/ttyACM0 — 16 sector(s) x 2 key type(s), 2477 keys — Ctrl-C for partial results

   MifareClassic check @ /dev/ttyACM0
┏━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ Sector ┃ Key A        ┃ Key B        ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│      0 │ A0A1A2A3A4A5 │ A0A1A2A3A4A5 │
│      1 │ FFFFFFFFFFFF │ FFFFFFFFFFFF │
└────────┴──────────────┴──────────────┘

ℹ 32/32 keys recovered — card exposes 16/16 sectors with known keys
```

A cell with no key in the dictionary shows `[unknown]` instead. `--json` emits one object: `{"sectors": [{"sector": 0, "key_a": "...", "key_b": null}, …], "recovered": n, "total": n}`.

Known keys are tried first on later sectors — real cards commonly reuse the same key across sectors, so once one is recovered the sweep tries it before the rest of the dictionary. A progress bar tracks sectors completed; **Ctrl-C prints whatever was recovered so far** instead of losing it. Exit code is `0` only if every requested `(sector, key type)` was recovered — `1` if anything came back `unknown` or the sweep was interrupted.

**Named dictionaries (`--dict`) and UID-derived keys (`--uid-derived`).** Both only add *more known/public keys* to the candidate pool — they never break Crypto-1. `--dict` layers registered families (MAD, transport, NDEF, lock-vendor defaults) ahead of the base dictionary, ordered by probability, with sector-biased families (MAD → sectors 0/16) tried first where they belong; add your own with `--dict-file <file>`. `--uid-derived` recomputes the keys a **public** diversification algorithm fully specifies for the card's UID (currently `mizip`, ported verbatim from proxmark3's public MiZip calculator) and injects them right after the reuse candidates. Schemes that would need a secret master key (AES/HMAC) are registered only so the CLI can name the boundary and **refuse** — the master key is never guessed, and `all` never sweeps a secret scheme. Because both feed the same known-first-then-early-cut pipeline, turning them on is cheap: on a card that reuses keys, the extra candidates are only ever paid for on the sectors no reuse or default opened (see the performance note below).

Per-sector candidate order is: **confirmed keys (reuse) → UID-derived → named families → base dictionary → `--dict-file`**, de-duplicated. With no `--dict`/`--dict-file`/`--uid-derived`, the sweep is byte-for-byte the pre-Fase-2 behaviour.

**Trailer-read key-B recovery.** When the dictionary opens a sector's key A but not its key B, `check` then reads that sector's trailer with key A. On cards whose access bits leave key B *readable with key A* (the common transport/default configuration — NXP MF1S50yyX Table 8, trailer configs `000`/`001`), key B is stored in the trailer in cleartext and comes back for free — reported as `sector N key B recovered via trailer read` and folded into the table, `--json`, `--out` and `-o`/`--output-keys` output like any dictionary hit. This is **not** the Crypto-1 *nested* attack: the onboard PN7150 performs Crypto-1 inside the chip and never surfaces an encrypted nonce, so no host-side cryptographic recovery is possible on this hardware. It is a plain authenticated read of a key the card is configured to hand over. Key A itself never reads back under any access condition, so only key B is recoverable this way.

Worst case (a card using no known key) means trying the full dictionary against every sector/key-type — with the bundled 2477-key list and `both` key types over 16 sectors that's tens of thousands of auth round-trips, which can take minutes; the progress bar and Ctrl-C partial results make that tolerable for a one-off check.

**Performance note.** The dominant cost is any *unknown* sector, which must sweep the whole candidate pool once. Everything else opens early: a confirmed key is tried first on the remaining sectors (reuse), and the search cuts as soon as a sector's key types are found. This is why `--dict`/`--uid-derived` stay cheap even though they enlarge the pool — the added candidates are only ever fully paid for on sectors that nothing else opened. On the representative 1K test card (key-type A, one private-key sector), the auth count moves from **2514** (baseline) to **2525** with `--dict all` and **2527** with `--dict all --uid-derived mizip` — a naive per-sector sweep of the same enlarged pool would be ≈ 39 000. This is pinned as a regression test (`tests/test_mifare_phase5.py`).

---

<a id="tags-mifare-dump"></a>
### `tags mifare dump`

> Read every sector of a card in one batch pass and save it to file. The bulk-extraction counterpart to `mifare sector` — `sector` inspects **one** sector interactively; `dump` reads the **whole** card non-interactively and writes it out. Neither replaces the other.

Feed it a keyfile produced by [`mifare check --out`](#tags-mifare-check) (or hand-written as `sector:keyA:keyB` lines). A sector missing from the keyfile, or whose read fails, is recorded as a **gap** — `dump` never aborts the rest of the card for one bad sector.

| Option | Description |
|---|---|
| `-k, --keys-file FILE` | **Required.** `sector:keyA:keyB` file (see `mifare check --out`). A sector missing, or blank for both keys, dumps as a gap. |
| `--sectors N` | Number of sectors to dump (default `16` = 1K). Max `32` (2K); 4K's 16-block sectors (32-39) aren't supported yet. |
| `--out FILE` | Write the dump as canonical JSON — see format below. |
| `--mfd FILE` | Write a raw binary dump (1024 bytes for 1K), Proxmark/mfoc `hf mf restore`-compatible. Gaps are filled with zeros. |
| `--eml FILE` | Write a hex-per-line dump (32 chars/line), Proxmark `hf mf eload`-compatible. Gaps are filled with zeros. |
| `--force` | Overwrite `--out`/`--mfd`/`--eml` if they already exist. |
| `--json` | Emit the dump as JSON on stdout instead of the table. |
| `--decode` | After the status table, print each read sector's raw blocks with an ASCII column plus the dissected block 0 (UID/BCC/SAK/ATQA). Human view only — `--out`/`--mfd`/`--eml`/`--json` are unchanged. Ignored with `--json`. |
| `-t, --timeout SEC` | Seconds to wait for a card tap if none is selected yet (default `5`). |

```sh
bombercat tags mifare dump -k recovered.keys --out card.json
bombercat tags mifare dump -k recovered.keys --out card.json --mfd card.mfd --eml card.eml
bombercat tags mifare dump -k recovered.keys --sectors 32 --json
bombercat tags mifare dump -k recovered.keys --decode
```

```
ℹ Dumping /dev/ttyACM0 — 16 sector(s) — Ctrl-C for partial results

   MifareClassic dump @ /dev/ttyACM0
┏━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ Sector ┃ Status ┃ Opened with ┃ Reason       ┃
┡━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│      0 │ OK     │ A           │              │
│      1 │ OK     │ A           │              │
│     12 │ FAILED │ —           │ no key available │
└────────┴────────┴─────────────┴──────────────┘

ℹ 15/16 sectors read — uid DEADBEEF
⚠ dump incomplete — interrupted before finishing
```

(The last warning line only shows on Ctrl-C; a run that finishes on its own just stops after the `N/total sectors read` line.)

With `--decode`, every successfully read sector prints again below the table — raw block hex plus an ASCII column, and sector 0's block 0 dissected the same way `mifare read --block 0`/`mifare sector --sector 0` show it:

```
Decoded sectors

  Sector 0
  block 0       DEADBEEF220800040102030405060708  |...."..........|
  block 1       48656C6C6F20576F726C642100000000  |Hello World!....|
  block 2       00000000000000000000000000000000  |................|
  block 3 (trailer)  FFFFFFFFFFFFFF078069FFFFFFFFFFFF

  Block 0 (UID sector)
  uid           DEADBEEF
  bcc           22  (OK)
  sak           08  -> MIFARE Classic 1K
  atqa          0004  -> MIFARE Classic 1K
  mfg data      0102030405060708
```

**Canonical JSON (`--out`/`--json`)** is the reference format — the only one that keeps a dump's provenance and its real keys:

```json
{
  "uid": "DEADBEEF",
  "size": "1K",
  "sectors_read": 15,
  "sectors_total": 16,
  "read_at": "2026-09-09T12:00:00Z",
  "source_keyfile": "recovered.keys",
  "sectors": [
    { "sector": 0, "opened_with": "A", "blocks": ["...", "...", "...", "..."] }
  ],
  "failed_sectors": [
    { "sector": 12, "reason": "no key available" }
  ]
}
```

Key A never reads back from the card (it's always zeros in the raw trailer), so `dump` substitutes the real keys from the keyfile into each sector's trailer block before writing it out — same substitution `mifare sector` shows on screen. The access-bits bytes in the trailer are left exactly as read.

**`--mfd`/`--eml` are not canonical** — they can't tell "not read" from real zeros and can't carry the recovered keys, so a gap is silently zero-filled in both. Prefer `--out` when you need to know what was actually read, or to round-trip into [`mifare restore`](#tags-mifare-restore).

Exit code is `0` only if every requested sector was read and the run wasn't interrupted — `1` for any gap or a Ctrl-C partial, in both table and `--json` mode (same convention as `check`).

---

<a id="tags-mifare-analyze"></a>
### `tags mifare analyze`

> Turn a [`mifare dump`](#tags-mifare-dump) JSON into an interpretable security report. **100% offline** — no card, no board, no firmware: it only reads what a dump already holds and explains it. The report sibling of `dump`/`restore`.

`analyze` never breaks Crypto-1 and never guesses a key. It reads a canonical `mifare dump --out` file and reports: the **MAD** (which application owns each sector, sector 0 plus MAD2 in sector 16), **value blocks** (the ±value/complement/address layout, told apart from data), **weak keys** (which sectors opened with a default/known key vs. a custom one), **insecure access bits** (readable key B, unprotected data blocks, bits that fail their own inverse), and — always — the **gaps**: sectors that were never opened, declared as not evaluable rather than assumed secure. Use it to justify migrating off MIFARE Classic.

| Option | Description |
|---|---|
| `DUMP_FILE` | **Required (positional).** A canonical `mifare dump --out` JSON to analyze. |
| `-k, --keys-file FILE` | A `sector:keyA:keyB` file (from [`mifare check --output-keys`](#tags-mifare-check)). When given it is **authoritative** for which keys were recovered — it tells a genuine all-zero key apart from a sector whose key was never found. |
| `--dict NAMES` | Named key families counted as *known/weak* when classifying keys, comma-separated and repeatable. Same families as [`mifare check --dict`](#tags-mifare-check) (`transport`, `mad`, `ndef`, `locks`, or `all`) — a key that is a public family key is a weakness, not a custom key. |
| `--dict-file FILE` | Extra key dictionary file counted as *known/weak* (repeatable). The bundled default dictionary is always included. |
| `--json` | Emit the full report as JSON on stdout instead of the tables. |

```sh
bombercat tags mifare analyze card.json                       # offline report from a dump
bombercat tags mifare analyze card.json -k recovered.keys     # classify recovered keys precisely
bombercat tags mifare analyze card.json --dict locks --json   # count lock-vendor keys as weak, machine-readable
```

```
ℹ MIFARE Classic dump analysis — uid DECAFBAD (1K)
   sectors            15 analyzed / 16 total

MAD (application directory)
  crc                12  (CRC ok)
  sector 1           AID 0004
  sector 4           AID 1234

Sectors
┏━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┓
┃ S ┃ Opened ┃ Key A                 ┃ Key B                 ┃ Notes                ┃
┡━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━┩
│ 0 │ A weak │ A0A1A2A3A4A5 default  │ …           default   │ MAD1 directory       │
│ 4 │ A weak │ FFFFFFFFFFFF default  │ …           default   │ value block @16 = 1000 │
└───┴────────┴───────────────────────┴───────────────────────┴──────────────────────┘

Gaps (not evaluable)
  sector 15          not present in dump (never read)
⚠ gaps were never opened — no security claim is made about them

Summary
  weak-key sectors   0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14
  custom-key sectors none
  gap sectors        15
```

`--keys-file` makes the classification precise: without it, keys come from the dump's trailer bytes, where the all-zero read-back a card gives for a hidden key is treated as "not recovered". A sector that never opened is always listed under **Gaps** and never contributes a security claim — the report cannot conclude a sector is "secure" just because it could not be read. `analyze` exits `0` unless the dump file itself can't be read (it's a report tool, not a pass/fail verdict).

**`--json`** emits the full report — `uid`, `size`, `mad`/`mad2`, a per-sector array (`key_a`/`key_a_class`, `value_blocks`, `access.issues`, …), a `gaps` list, and a `summary` (weak/custom/insecure/gap sector lists, value-block count) — with a stable schema for downstream tooling.

---

<a id="tags-mifare-workflow"></a>
### End-to-end workflow: `check` → `dump` → `analyze`

The three commands chain through two stable file contracts — the `sector:keyA:keyB` keyfile and the canonical dump JSON — so a full "recover keys, extract, report" pass is:

```sh
# 1. recover keys and write the per-sector keyfile
bombercat tags mifare check -o card.keys

# 2. dump the whole card using those keys, to canonical JSON
bombercat tags mifare dump -k card.keys --out card.json

# 3. report on the dump, offline, with the keyfile for precise classification
bombercat tags mifare analyze card.json -k card.keys
```

Steps 1–2 need the **MifareClassic** firmware and the card on the reader; step 3 needs neither — it runs anywhere from the saved `card.json`. A sector `check` cannot open stays blank in `card.keys`, becomes a gap in `card.json`, and is reported as *not evaluable* by `analyze` — honest end to end, never filled in or assumed secure. `check --out card.dic` additionally writes a flat mfoc/proxmark keyfile if you want to feed the keys to other tooling.

---

<a id="tags-mifare-restore"></a>
### `tags mifare restore`

> Write a [`mifare dump`](#tags-mifare-dump) JSON back to a card, sector by sector. The write counterpart to `dump` — round-trips a `--out` file back onto a card, typically a magic/clone (gen2, CUID) card.

> ⚠️ **Authorized use only.** Clone only cards you own or have explicit permission to clone, and only onto a magic card — never a genuine Mifare Classic. On a genuine card block 0 is factory-OTP: writing it fails, and in the worst case bricks the card permanently.

`restore` reads the canonical JSON `mifare dump --out` produces, pulls each sector's write keys straight out of its own trailer (key A, falling back to key B — the same substitution `dump` wrote in), and writes the data blocks back. Trailers (keys + access bits) are written last, after the data blocks; a trailer whose access bits fail their own inverse check is refused outright — writing it could permanently lock the sector. Sector 0's block 0 (the UID/BCC/SAK/ATQA) is skipped unless `--write-block0`, and even then only after a non-destructive probe (read block 0, write it back unchanged) proves the card actually accepts writes to it — a genuine card's factory-OTP block 0 fails that probe and the UID is never touched. A sector whose key can't authenticate, or whose write is denied, is recorded as a failure and `restore` moves on to the next sector rather than aborting.

| Option | Description |
|---|---|
| `--dump FILE` | **Required.** A canonical `mifare dump --out` JSON to write back to the card. |
| `--sectors N` | Restore only sectors `0..N-1` instead of every sector present in the dump. |
| `--write-block0` | Also write sector 0's block 0 (the card's UID/BCC/SAK/ATQA). Off by default; only ever succeeds on a magic card — see the non-destructive probe above. |
| `--skip-trailers` | Write only data blocks, leave every sector's trailer (keys + access bits) untouched. **Recommended for a first run** — it clones the card's contents without risking a mismatched access-bit layout locking a sector. Off by default (trailers are written). |
| `--yes` | Skip the interactive confirmation before writing block 0 (for scripted use). Still requires `--write-block0`. |
| `--json` | Emit the restore result as one JSON object instead of the table. |
| `-t, --timeout SEC` | Seconds to wait for a card tap if none is selected yet (default `5`). |

```sh
bombercat tags mifare restore --dump card.json --skip-trailers        # data only, first run
bombercat tags mifare restore --dump card.json                       # data + trailers
bombercat tags mifare restore --dump card.json --write-block0         # + clone the UID (magic card only)
bombercat tags mifare restore --dump card.json --sectors 4 --json
```

Writing block 0 asks for interactive confirmation first (skip it with `--yes` for scripts):

```
Writing block 0 rewrites the card's UID/BCC/SAK/ATQA — its identity. Only do
this on a magic card (gen2/CUID) you own or have permission to clone; on a
genuine MIFARE Classic block 0 is factory-OTP and the write will fail (in the
worst case bricking it). Continue?  [y/N]:
```

```
ℹ Restoring 16 sector(s) to /dev/ttyACM0 — Ctrl-C for partial results

   MifareClassic restore @ /dev/ttyACM0
┏━━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ Sector ┃ Status ┃ Blocks ┃ Reason       ┃
┡━━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━┩
│      0 │ OK     │      3 │              │
│      1 │ OK     │      4 │              │
│     12 │ FAILED │      0 │ no write key in dump trailer │
└────────┴────────┴────────┴──────────────┘

ℹ 15/16 sectors written
```

(A row with `written` less than the sector's block count — 4, or 3 with `--skip-trailers`, or 1/2 for sector 0 depending on `--write-block0` — means the write was denied partway through; the sector's `reason` explains where it stopped.)

`--json` emits one object:

```json
{
  "target_sectors": 16,
  "sectors_written": 15,
  "block0_written": false,
  "trailers_skipped": false,
  "interrupted": false,
  "source_dump": "card.json",
  "written_at": "2026-09-09T12:00:00Z",
  "sectors": [
    { "sector": 0, "written": 4, "block0": false, "reason": "ok" },
    { "sector": 12, "written": 0, "block0": false, "reason": "no write key in dump trailer" }
  ],
  "failed_sectors": [
    { "sector": 12, "written": 0, "block0": false, "reason": "no write key in dump trailer" }
  ]
}
```

A sector's dump entry with an all-zero key A/B (what a card reads back when the real key wasn't substituted in — see [`mifare dump`](#tags-mifare-dump)) has no usable write key and is reported as `no write key in dump trailer`. A trailer's access bits that fail their own inverse check are refused with `trailer not written: invalid access bits (would risk locking the sector)`, leaving the card's existing trailer alone; a *valid* but self-locking layout (all three write conditions permanently closed) is still written, with a `warning` field (and a printed `⚠` line in table mode) since it may be intentional as part of a faithful clone. Ctrl-C stops the run and reports whatever was written so far. Exit code is `0` only if every targeted sector wrote clean and the run wasn't interrupted — `1` for any failure or a Ctrl-C partial (same convention as `check`/`dump`).

---

<a id="tags-mifare-code"></a>
### `tags mifare code`

> Turn a plain string into the block hex a sector stores. **Offline** — no card, no board, no firmware: it only formats, the writing is still [`mifare write`](#tags-mifare).

A Mifare Classic block is 16 bytes, written whole. `code` UTF-8 encodes TEXT, pads it out to whole blocks, and prints the 32-hex-char strings `mifare write --data` takes. With `--sector` it also places them on that sector's **data** blocks — the trailer (keys + access bits) is never a target, and neither is block 0 of sector 0 (the factory UID block).

| Option | Description |
|---|---|
| `--sector N` | Lay the blocks out over sector `N`'s data blocks and label them with their real block numbers. Without it, blocks are listed unplaced. Max `31` (2K); 4K's sectors 32-39 aren't supported yet. |
| `-k, --keys-file FILE` | A `sector:keyA:keyB` file (see [`mifare check --output-keys`](#tags-mifare-check)). Needs `--sector`; uses that sector's key A (falling back to key B) to print the exact `auth`/`write` lines to run. |
| `--pad HEX2` | Filler byte for the tail of the last block (default `00`). |
| `--json` | Emit the encoding as one JSON object instead of the field list. |

```sh
bombercat tags mifare code "ElectronicCats"                        # just the padded block hex
bombercat tags mifare code "carnet: 12345" --sector 1              # placed on blocks 4-6
bombercat tags mifare code "carnet: 12345" --sector 1 -k card.keys # + the commands to run
bombercat tags mifare code "hola" --sector 1 --json
```

```
  text          carnet: 12345
  bytes         13 → 16 padded with 0x00 (1 block(s))
  sector        1
  key           D3F7D3F7D3F7 (key A, from card.keys)

  Blocks
  block 4       6361726E65743A203132333435000000

  Write it
  bombercat tags mifare auth --block 4 --key-type A --key D3F7D3F7D3F7
  bombercat tags mifare write --block 4 --data 6361726E65743A203132333435000000
```

A sector holds **48 bytes** across its three data blocks (**32 bytes** for sector 0, whose block 0 is off limits); text that doesn't fit is an error naming the capacity, not a silent truncation — split it yourself across sectors you have keys for. Exit code is `1` for that, for a malformed `--pad`, for empty text, and for a sector missing from the keyfile.

To run those lines instead of copy-pasting them, use [`mifare write-text`](#tags-mifare-write-text) — same encoding, one command, and it writes to the card.

---

<a id="tags-mifare-write-text"></a>
### `tags mifare write-text`

> [`code`](#tags-mifare-code)'s encoder plus a real write pass: encode a string and write it to the sector you choose, authenticating with the keys you hold. Requires the **MifareClassic** firmware and a card on the reader.

> ⚠ **Authorized use only.** Write only to cards you own or have explicit permission to write.

`code` stops at printing the `auth`/`write` lines; `write-text` runs them. TEXT is UTF-8 encoded and padded to whole 16-byte blocks exactly as `code` does, the target sector is authenticated (key A, falling back to key B), and each block is written in order. Sector trailers and block 0 of sector 0 are never targets — those are [`mifare restore`](#tags-mifare-restore) territory.

| Option | Description |
|---|---|
| `--sector N` | Target sector; the text starts at its **first data block** (block 1 for sector 0, whose block 0 is off limits). Max `31` (2K). |
| `--block N` | Start at this absolute block instead, its sector derived (4 blocks per sector). A trailer or block 0 of sector 0 is refused with the list of usable blocks. |
| `--key HEX12` / `--key-type A\|B` | A single key to authenticate the sector with. |
| `-k, --keys-file FILE` | A `sector:keyA:keyB` file (see [`mifare check --output-keys`](#tags-mifare-check)) — uses that sector's key A, falling back to key B. Mutually exclusive with `--key`. |
| `--pad HEX2` | Filler byte for the tail of the last block (default `00`). |
| `--json` | Emit the write result as one JSON object. |
| `-t, --timeout` | Seconds to wait for a card tap if none is selected yet (default `15`). |

```sh
bombercat tags mifare write-text "carnet: 12345" --sector 1 -k card.keys
bombercat tags mifare write-text "carnet: 12345" --sector 1 --key D3F7D3F7D3F7
bombercat tags mifare write-text "hola" --block 6 --key-type B --key FFFFFFFFFFFF
bombercat tags mifare write-text "hola" --sector 1 -k card.keys --json
```

```
  text          carnet: 12345
  bytes         13 → 16 padded with 0x00 (1 block(s))
  sector        1
  key           key A, from card.keys

  Blocks written
  block 4       6361726E65743A203132333435000000

  ✔ wrote 1 block(s) to sector 1
```

```json
{"text": "hola", "encoding": "utf-8", "bytes": 4, "padded_bytes": 16, "pad": "00",
 "sector": 1, "key_type": "A", "blocks_total": 1, "blocks_written": 1,
 "blocks": [{"block": 4, "data": "686F6C61000000000000000000000000"}],
 "reason": "ok", "written_at": "2026-09-09T12:00:00Z"}
```

Capacity is the same as `code`'s — **48 bytes** per sector (**32** for sector 0), less if `--block` starts mid-sector; text that doesn't fit is an error naming the room left, never a silent truncation. A denied write stops the run and reports the blocks written so far (`blocks_written` < `blocks_total`), so a partial write is always visible. Exit code is `1` for a failed auth, a denied write, a full/partial failure, or any of the validation errors above.

---

### Notes

- `read`/`watch`/`scan`/`info` require **DetectTags** firmware; `mifare …` requires **MifareClassic** firmware instead — confirm which is flashed with [`bombercat status`](../commands/status.md).
- See [Device selection: `-d` / `-p`](../reference.md#device-selection) for selector rules.
- Two things worth knowing:
  - **Every published `.uf2` today parses as legacy text**, not the newer structured `:tag` events — same detections, just without the `extra` fields the structured format can carry. `bombercat tags info` tells you which mode a given board is in. ([Chip model fingerprinting](#chip-model) is the exception: it is derived from prose both modes print, so it works either way.)
  - **NFC-B and NFC-F tags print no UID on today's published `.uf2`** — that's a firmware limitation, not a CLI bug. `tags` reports it honestly as `unavailable (NFC-B: firmware prints no ID)` rather than a blank or a made-up value. A `DetectTags.ino` update that extracts the real UID for both (PUPI for NFC-B, IDm for NFC-F) exists in the firmware source but isn't in a published release yet — boards built from that source report the real UID (plus `attrib`/`bitrate` extras) instead.
- See [Troubleshooting](../troubleshooting.md#no-tags-detected) if `watch`/`scan` looks quiet with a card actually on the reader.

---

## See also

- [`readers`](../commands/readers.md) — the mirror image: detect *readers/terminals* instead of *tags*, over the **DetectReaders** firmware's emulated card. Same `read`/`watch`/`scan`/`info` shape.
- [`status`](../commands/status.md) — check or flash the firmware a board needs before running any of these.

---

<a id="chip-model"></a>
## Chip model fingerprinting

Every NFC-A detection carries two bytes that say a lot about what the tag
actually is: **ATQA** (the `SENS RES` the firmware prints) and **SAK** (`SEL
RES`). The firmware has always printed both as prose; the CLI now parses that
pair, normalizes it, and looks it up in `modules/tags/chips.py` — the same
(ATQA, SAK) table proxmark3's `hf 14a info` and libnfc/nfc-tools use, following
NXP AN10833 *"MIFARE type identification procedure"*.

The result shows up as three keys on every NFC-A detection — `atqa`, `sak` and
`model` — in `tags read`, `tags watch --json`, the `tags scan` table and its
JSON/CSV exports. **No firmware change is needed**: the bytes were already on
the wire, and this works in **legacy text mode as well as structured mode** —
which matters, because every published `.uf2` today is in legacy mode.

Only NFC-A carries an ATQA/SAK, so NFC-B, NFC-F and NFC-V detections never get
these keys (NFC-B's longer `SENSB_RES` is printed under the same `SENS RES`
label and is deliberately not mistaken for an ATQA).

### One-line detection delay on NFC-A

The firmware prints `SEL RES` — the SAK — *one line after* the UID:

```
	Technology: NFC-A
	SENS RES = 0x04 0x00
	NFC ID = 0x32 0x91 0x13 0x20
	SEL RES = 0x08
```

So in legacy mode an NFC-A detection is now finalized on the `SEL RES` line
rather than on the `NFC ID` line — otherwise the tag would be reported before
its own SAK existed. Both the published `.uf2` and the current firmware source
print `SEL RES` unconditionally right there, and `Remove the Card` (printed
after every detection) is the backstop: a detection is never lost, only
finalized a line later. Other technologies are unaffected.

### Byte order

ISO14443-3 transmits ATQA low byte first, so the firmware's
`SENS RES = 0x44 0x00` is ATQA **`0044`** — written the conventional MSB-first
way, matching how proxmark reports it. SAK is a single byte, as printed.

### What the table can and cannot tell apart

ATQA + SAK identify a **family**. Several chips are byte-identical at this
layer and can only be separated by a command `DetectTags` never issues, so
those entries deliberately name the family instead of guessing a part number:

| Signature | Reported as | Actually could be | Separated by |
|---|---|---|---|
| `0044` / `00` | MIFARE Ultralight / NTAG | UL, UL-C, UL EV1, NTAG203, NTAG210/212/213/215/216, NTAG I²C | `GET_VERSION` (`0x60`), or the UL-C 3DES auth |
| `0344` / `20` | MIFARE DESFire / NTAG 424 DNA | DESFire D40/EV1/EV2/EV3, NTAG 424 DNA | `GetVersion` (`0x60`) |
| `0004` / `08` | MIFARE Classic 1K | genuine 1K, Fudan FM11RF08, Gen1a/CUID/FUID "magic" clones | the `0x40`/`0x43` magic backdoor probe |

Magic/clone cards answer ATQA and SAK *exactly* like the chip they impersonate
— that is the point of them — so they are not, and cannot be, a table entry.

An unknown pair, or a tag that printed no `SENS RES`/`SEL RES` at all (NFC-B,
NFC-F, NFC-V), leaves `model` unset and the table column blank. The detection
itself is unaffected: `uid`, `tech` and `protocol` are parsed exactly as before.
