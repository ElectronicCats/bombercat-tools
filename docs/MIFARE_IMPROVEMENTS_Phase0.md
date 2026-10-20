# MIFARE improvements — Fase 0 baseline (signatures, contracts, metrics)

Reference for anyone continuing `PLAN_Mejoras_MIFARE.md`. It records the current
`modules/tags/mifare/` surface, the I/O formats later phases must not break, the
hardware-free test base added in Fase 0, and the measured baseline auth counts.

Everything here describes the code **as it is today** — it introduces no new
behaviour. Scope of the plan is host/CLI only; no firmware changes.

---

## 1. Module inventory (`modules/tags/mifare/`)

| File | Role | Key public surface |
|------|------|--------------------|
| `cli.py` | Click group + the interactive one-shot commands | `mifare` group; `auth`, `read`, `write`, `sector`, `keys` commands |
| `check.py` | Dictionary attack over every sector | `mifare_check_cmd` |
| `dump.py` | Whole-card batch read → file/JSON | `mifare_dump_cmd` |
| `restore.py` | Write a canonical dump back to a card | `mifare_restore_cmd` |
| `code.py` | Offline text → block hex (no device) | `mifare_code_cmd` |
| `write_text.py` | `code` + a real auth/write pass | `mifare_write_text_cmd` |
| `block0.py` | Block 0 dissection (UID/BCC/SAK/ATQA) | `parse_block0(hex) -> Block0 | None` |
| `access_bits.py` | Trailer access-condition decode | `parse_access_bits(hex) -> SectorAccessBits | None` |
| `keyfile.py` | Key-dictionary + `sector:keyA:keyB` loaders | `load_keys`, `load_sector_keys`, `default_keyfile` |
| `common.py` | Sizes, geometry, hex validation | `_sector_first_block`, `_MIFARE_CHECK_MAX_SECTORS`, … |
| `session.py` | The one place that touches hardware | `_mifare_session`, `_read_one_sector`, `_auth_sector` |
| `display.py` | Human rendering of sectors/trailers | `_print_decoded_sector`, `_substitute_trailer_keys` |
| `data/mifare_default_keys.keys` | Bundled dictionary (2481 keys) | — |

Every command reaches the device through **`session.py`**, so tests patch that
single module (`resolve_port`/`DeviceLink`) to run without hardware.

### Command signatures (host-facing contract)

- **`check`** — `--keys FILE` (repeatable; default = bundled dict, and passing
  `--keys` *replaces* it), `--sectors 1..32` (default 16), `--key-type A|B|both`
  (default both), `--json`, `--out FILE` (flat keyfile, one 12-hex key/line),
  `-o/--output-keys FILE` (`sector:keyA:keyB`), `--force`, `-t/--timeout`.
  Exit **0** iff every requested (sector, key type) was recovered, else **1**.
- **`dump`** — `-k/--keys-file FILE` (**required**, `sector:keyA:keyB`),
  `--sectors 1..32` (default 16), `--out FILE` (canonical JSON), `--mfd FILE`
  (raw binary), `--eml FILE` (hex/line), `--force`, `--json`, `--decode`,
  `-t/--timeout`. Exit **0** iff every sector was read, else **1**.
- **`sector`** — `--sector`, `--key-type A|B`, `--key` **or** `--keys-file`,
  `--json`. **`auth`** — `--block --key-type --key`. **`read`** — `--block`.
  **`write`** — `--block --data`. **`keys`** — lists firmware default keys.

---

## 2. Stable data contracts (do not break — plan principle #3)

### 2.1 Key dictionary (`.keys`/`.dic`/`.md`) — `load_keys`
Any file; only lines that are **exactly 12 hex chars** (after strip) are kept,
uppercased, de-duplicated, first-seen order preserved. Everything else
(frontmatter, prose, `#` comments, wikilinks) is silently ignored — so
mfoc/proxmark keyfiles and the raw vault note both load unmodified.

### 2.2 Per-sector keyfile (`sector:keyA:keyB`) — `load_sector_keys`
One line per sector: `"<sector>:<keyA or blank>:<keyB or blank>"`. Either key may
be blank (that slot wasn't recovered → stored as `None`); the sector and both
separators are always present. Blank lines and `#` comments are skipped; **any
other malformed line raises `SectorKeyfileError`** (loud, unlike the dictionary).
Written by `check --output-keys`; read by `dump`/`sector`/`code`/`write-text`.

### 2.3 Canonical dump JSON — `dump --out`
```json
{
  "uid": "DECAFBAD",
  "size": "1K",
  "sectors_read": 16,
  "sectors_total": 16,
  "read_at": "2026-09-24T00:00:00Z",
  "source_keyfile": "card_1k.keys",
  "sectors": [
    {"sector": 0, "opened_with": "A",
     "blocks": ["<block0>", "<block1>", "<block2>", "<trailer>"]}
  ],
  "failed_sectors": [{"sector": 15, "reason": "no key available"}]
}
```
The trailer block carries the **real** keys (substituted in from the keyfile),
not the zeros the card returns. `--mfd`/`--eml` are raw formats that **cannot**
distinguish "not read" from real zeros, so gaps there are zero-filled; only the
JSON declares gaps honestly via `failed_sectors`. This JSON is the offline input
for the Fase 4 analyzer.

### 2.4 Trailer / access bits
Trailer layout (32 hex): `keyA(12) | AC(6) | GPB(2) | keyB(12)`. `parse_block0`
handles 4-byte-UID block 0 only (returns `None` otherwise); `parse_access_bits`
decodes the 3 AC bytes into per-block + trailer permissions and a `valid` flag
(C-bits vs. their stored inverses). Key A never reads back under any condition;
key B reads back only when the access bits permit it — this is what `check`'s
trailer-read key-B recovery relies on (and it is **not** a Crypto-1 attack).

---

## 3. Geometry limits (current ceiling)

`_sector_first_block(s) = s * 4`, valid for **4-block sectors only**, capped at
`_MIFARE_CHECK_MAX_SECTORS = 32` (1K = 16, 2K = 32). **4K's 16-block sectors
32–39 are not addressable by any CLI command today.** A 4K card can therefore be
represented as a dump *file* (offline analysis) but not `check`ed/`dump`ed live.
Lifting this is a deliberate future edit, pinned by
`test_baseline_2k_is_the_current_cli_ceiling`.

---

## 4. Hardware-free test base (added in Fase 0)

- **`tests/mifare_card.py`** — a `Card`/`Sector` model and a `CardLink(FakeLink)`
  responder that answers `mifare auth`/`mifare sector` from the model instead of
  a hand-written script. `auth` succeeds **iff the caller already knows the
  key** — the plan's frontier, never Crypto-1. Includes correct-by-construction
  encoders (`encode_access_bits`, `mad1_blocks` with AN10787 CRC, `value_block`)
  and the two reference cards `card_1k()` / `card_4k()`.
- **`tests/fixtures/mifare/`** — committed 1K + 4K dumps and keyfiles generated
  from those cards (see that folder's README).
- **`tests/test_mifare_phase0.py`** — fixture-integrity, mock-fidelity and
  baseline tests. Runs with **no PN7150 attached**.

Run: `pytest tests/test_mifare_phase0.py -q` (needs `requirements-dev.txt`).

---

## 5. Baseline metrics (current state)

Measured on the 1K reference card via `CardLink`, counting `mifare auth` lines
(`test_baseline_*`). **I²C wall-clock time is deliberately not pinned** — it is a
hardware property of the PN7150 link and cannot be measured off-device; the auth
**count** is the hardware-independent proxy Fase 1 optimizes.

| Scenario | `mifare auth` sent | Notes |
|----------|-------------------:|-------|
| 1K, `--key-type A` | **2514** | 15 sectors open early; sector 15 (unknown) sweeps the full 2481-key dict |
| 1K, `--key-type both` | **5028** | sector 15 swept for both slots |
| 2K (32 sectors), `--key-type A`, all keys known | **70** | all found early via known-keys-first |

### Key finding for Fase 1
`check.py` **already** implements the core of Fase 1: known-keys-first ordering
(`candidates = list(known) + rest`), cross-slot reuse (a key found as A is tried
first as B, since `known` is shared across key types), and early-cut per sector
(break on first success). The 2K/70 figure is the proof — 32 sectors, mostly one
auth each. Fase 1's remaining, non-duplicative work is therefore: **exposing the
"auths saved vs. a naive full sweep" statistic**, and confirming no coverage is
lost. It should *not* re-implement known-first from scratch.

The dominant cost is any **unknown** sector, which sweeps the whole dictionary
(×2 for both slots) — nothing on the host can reduce that without cracking, which
is out of scope. Fases 2–3 (bigger dictionaries, UID-derived candidates) push
that cost **up**; the known-first ordering is what keeps it bounded.
