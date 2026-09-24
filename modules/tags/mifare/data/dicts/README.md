# Named key dictionaries (`mifare check --dict`)

These are **optional, opt-in** candidate
sets layered on top of the bundled default dictionary
(`../mifare_default_keys.keys`). If you select none, `check` behaves exactly as
before.

Nothing here is a cryptographic attack. Every key is a **published, known**
value; the families only change *which known keys are tried and in what order*.
The Crypto-1 wall (nested/darkside/hardnested) stays out of reach by design —
see the plan's guiding principles.

## Families

| Family      | File            | Tier          | Sector bias | What it is |
|-------------|-----------------|---------------|-------------|------------|
| `transport` | `transport.dic` | 0 (default)   | —           | Factory/transport defaults every card ships with. Highest probability. |
| `mad`       | `mad.dic`       | 1 (app)       | 0, 16       | MIFARE Application Directory keys (NXP AN10787), tried first on the MAD sectors. |
| `ndef`      | `ndef.dic`      | 1 (app)       | —           | NFC Forum / NDEF public key. |
| `locks`     | `locks.dic`     | 2 (vendor)    | —           | Access-control / lock vendor defaults. Lowest priority; operator-curated. |

`--dict all` selects every registered family. The combined queue is ordered by
**probability**: known keys already found on the card → sector-biased keys on
their sector → tier 0 → tier 1 → tier 2 → the base dictionary → any
`--dict-file`. Early-cut per sector means the most likely keys are spent first.

## Adding your own family

1. Drop a `<name>.dic` (one 12-hex key per line; `#` comments/blanks ignored) in
   this folder.
2. Register it in `../../dicts.py` (`REGISTRY`): pick a `tier` and, if the keys
   belong to specific sectors, a `sectors=` bias.

For a one-off set you don't want to register, skip both steps and pass it with
`mifare check --dict-file <path>` (loaded at the lowest, generic priority).

## Provenance & licence

The key **values** in `transport.dic`, `mad.dic` and `ndef.dic` are published
factory/standard defaults. They are facts (6-byte numbers), not copyrightable,
and are mirrored identically across the open-source ecosystem:

- libnfc / mfoc / mfcuk default key lists — GPL/LGPL.
- Proxmark3 `client/dictionaries/mfc_default_keys.dic` — GPL-3.0.
- Flipper Zero `mf_classic_dict.nfc` — GPL-3.0.
- NXP application notes **AN10787** (MAD) and **AN1305** (NDEF) — public specs.

`locks.dic` ships only small, generic/example defaults (see its header); real
product keys are added by the operator for their own authorized engagements.

Distributing these known-default lists for authorized security testing is the
same footing as every tool above; no key here is a secret or a Crypto-1
recovery. Use only on cards you own or are explicitly permitted to test.
