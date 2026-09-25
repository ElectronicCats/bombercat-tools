# `mifare` — MIFARE Classic tooling (host side)

The `bombercat tags mifare …` command group. Block-level access and the
dictionary/dump/analyze pipeline over the **MifareClassic** firmware's REPL
(one command owns the `mifare` group; the big commands live one per module and
are registered in [`cli.py`](cli.py)).

User-facing docs, examples and output samples: [`docs/commands/tags.md`](../../../docs/commands/tags.md)
(`#tags-mifare*`). Design/IO contracts: [`docs/MIFARE_IMPROVEMENTS_Phase0.md`](../../../docs/MIFARE_IMPROVEMENTS_Phase0.md).
The improvement roadmap and its progress log: [`PLAN_Mejoras_MIFARE.md`](../../../../PLAN_Mejoras_MIFARE.md).

## The boundary (non-negotiable)

Everything here **only tries keys it already has or recomputes from a public
scheme.** It never breaks Crypto-1: the onboard PN7150 runs Crypto-1 in-chip and
only reports auth pass/fail, so *nested / darkside / hardnested* are permanently
out of reach on this hardware. Two consequences run through the whole module:

- No key is ever guessed. `check` sweeps known/public keys; `analyze` reads only
  what a dump already holds; UID-derived schemes **recompute** values a public
  algorithm fully specifies, and refuse any scheme needing a secret master key.
- Sectors that don't open are **declared as gaps**, never concluded "secure".

## Command map

| Command | Module | Needs device? | Role |
|---|---|---|---|
| `auth` / `read` / `write` / `sector` / `keys` | [`cli.py`](cli.py) | yes | Single-block / single-sector access; `keys` lists firmware defaults. |
| `check` | [`check.py`](check.py) | yes | Dictionary attack: try known keys against every sector; recover key B off readable trailers. |
| `dump` | [`dump.py`](dump.py) | yes | Read the whole card in one pass to canonical JSON (`--out`) / `.mfd` / `.eml`. |
| `analyze` | [`analyze.py`](analyze.py) | **no** | Offline security report from a dump JSON (MAD, value blocks, weak keys, access bits, gaps). |
| `restore` | [`restore.py`](restore.py) | yes | Write a dump JSON back to a (magic) card. |
| `code` / `write-text` | [`code.py`](code.py) / [`write_text.py`](write_text.py) | `code` no, `write-text` yes | Encode a string to block hex; `write-text` also writes it. |

### Shared pieces

- [`session.py`](session.py) — opens the firmware's interactive mifare session
  (waits for a card tap), and the `_read_one_sector` / `_run_mifare_command`
  helpers every device command shares. `-t/--timeout` and `--key-type` options.
- [`keyfile.py`](keyfile.py) — the two stable file contracts: flat key
  dictionaries (`load_keys`) and the per-sector `sector:keyA:keyB` file
  (`load_sector_keys`) that ties the pipeline together. The bundled default
  dictionary lives in [`data/mifare_default_keys.keys`](data/mifare_default_keys.keys).
- [`dicts.py`](dicts.py) — named key families (`--dict`) under
  [`data/dicts/`](data/dicts/), layered ahead of the base dictionary by
  probability tier. `CandidatePlan.for_sector` builds the ordered, de-duplicated
  candidate list.
- [`uidkeys.py`](uidkeys.py) — public UID-diversification schemes (`--uid-derived`),
  e.g. `mizip`. Secret-key schemes are registered only to name the boundary and
  refuse.
- [`block0.py`](block0.py) / [`access_bits.py`](access_bits.py) / [`mad.py`](mad.py)
  — dissectors (one per structure): block 0 (UID/BCC/SAK/ATQA), trailer access
  bits, and the Mifare Application Directory (AN10787 CRC).
- [`display.py`](display.py) / [`common.py`](common.py) — Rich rendering and the
  small shared constants (block/key hex lengths, sector→first-block geometry).

## The pipeline

```
check -o card.keys     →     dump -k card.keys --out card.json     →     analyze card.json -k card.keys
   (recover keys)                    (extract to canonical JSON)                 (offline report)
        │                                     │                                       │
        └──── sector:keyA:keyB ───────────────┴──── canonical dump JSON ─────────────┘
```

`check`'s per-sector candidate order — **confirmed keys (reuse) → UID-derived →
named families → base dictionary → `--dict-file`** — plus early-cut is what keeps
the dictionary/UID expansion cheap: only sectors nothing else opened ever sweep
the full pool. See the performance pins in
[`tests/test_mifare_phase5.py`](../../../tests/test_mifare_phase5.py).

## PN7150 limits worth knowing

- **No Crypto-1 recovery** (see the boundary above). Key B is only recoverable
  when the access bits leave it readable with key A (transport configs); key A
  never reads back.
- **Geometry ceiling: 32 sectors.** The CLI addresses 4-block sectors 0–31 (1K,
  and 2K's 32 sectors). 4K's 16-block sectors 32–39 aren't addressable yet
  (`common._sector_first_block`); MAD2 is decoded for sectors 17–31 accordingly.
- **Slow I²C link.** Every candidate is an auth round-trip over a slow bus, so
  candidates are ordered by probability and cut early; `--uid-max` bounds the
  UID-derived additions per sector.
- **UID cloning is uncertain** — probably unsupported by the PN7150 (plan Fase 6,
  not implemented).

## Testing (no hardware)

Everything runs through [`tests/mifare_card.py`](../../../tests/mifare_card.py)'s
`CardLink`, an in-memory card that answers `auth`/`sector` like a real one (and
only ever accepts a key it's told to hold — the boundary, enforced in the mock).
Fixtures: [`tests/fixtures/mifare/`](../../../tests/fixtures/mifare/).

```sh
pytest tests/ -k mifare
```

Baseline auth counts are pinned (Fase 0/2/3/5) so any change to the candidate
ordering is a deliberate, visible edit rather than silent drift.

## Extending

- **A new key family:** drop `<name>.dic` in [`data/dicts/`](data/dicts/) (record
  provenance/licence in its header and [`data/dicts/README.md`](data/dicts/README.md))
  and add one `NamedDict` entry to `REGISTRY` in [`dicts.py`](dicts.py).
- **A new public UID scheme:** add a class with `generate(uid, sector)` and
  register it in `SCHEMES` in [`uidkeys.py`](uidkeys.py), with fixed test vectors.
  Secret-master-key schemes stay out of scope by design.
