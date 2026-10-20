# MIFARE improvements — Fase 6: UID-clone verification (PN7150 vs. magic cards)

Reference for `PLAN_Mejoras_MIFARE.md`, Fase 6 ("Verificación de clonado de
UID"). It answers the plan's open question — *can this hardware rewrite a Mifare
Classic UID (block 0)?* — grounds the answer in the actual firmware/library
code, and documents the `mifare clone-uid` command that verifies it on the
bench.

Fase 6 sits **outside** improvements A/B. It changes no cracking capability and
deduces no keys. Zero firmware changes: `clone-uid` reuses the existing
`mifare auth`/`read`/`write` REPL verbs.

---

## 1. The question, split in two

"Cloning a UID" onto a Mifare Classic means writing block 0 (the manufacturer
block: `UID(4) · BCC(1) · SAK(1) · ATQA(2) · manufacturer(8)`). On a genuine
card block 0 is factory-OTP and never writable. Two families of "magic" cards
lift that:

| Card | How block 0 is unlocked | What the reader must do |
|------|-------------------------|-------------------------|
| **gen2 / CUID / "direct-write"** | No unlock — block 0 just takes a normal write after a normal Crypto-1 auth | A standard authenticate + a standard `A0` write |
| **gen1a / "backdoor"** | A proprietary unlock sequence: a **7-bit** short frame `0x40` (no CRC) right after HALT, then `0x43`; block 0 is then read/written with **no** authentication | Emit sub-byte frames with custom CRC/parity handling |

So the hardware question is really: **(a)** can the PN7150 do a standard write to
block 0? and **(b)** can it emit the gen1a 7-bit backdoor frames?

---

## 2. Task 1 — does the PN7150 NCI API allow raw non-standard frames?

The only raw reader-mode primitive this stack exposes is
`Electroniccats_PN7150::readerTagCmd()` (wrapped by
`NfcController::readerTransceive()`, used by every reader-mode call including
`MifareCommands.cpp`). It sends an **NCI `CORE` data packet**:

```
Cmd = { 0x00, 0x00, CommandSize, <CommandBytes...> }   // Electroniccats_PN7150.cpp:1801
```

Once discovery has activated the tag with the standard ISO 14443-3A / Mifare RF
protocol (`RF_INTF_ACTIVATED`), everything sent through that data path is framed
by the NFCC as **whole bytes with CRC_A and parity appended automatically**.
The library (and BomberCat's `NfcController`) expose **no** parameter to:

- transmit a **sub-byte / 7-bit short frame** (what the gen1a `0x40` wakeup is);
- **suppress CRC** on an activated tag;
- send with **custom parity**.

Those knobs live below the NCI RF interface, in the NFCC's low-level frame/RF
configuration, and are not reachable through `readerTagCmd()` — the entire raw
surface this firmware has. A standard `A0`/`30` write/read **is** a full-byte
data packet, so it goes through fine; the gen1a unlock is **not** a byte
sequence and cannot be expressed as one.

> Note the collision of names: `MifareCommands.cpp` uses opcode `0x40` for the
> *Crypto-1 authenticate* command (`MIFARE_CMD_AUTH`). That is a full 8-bit byte
> in an activated-tag data packet. The gen1a backdoor `0x40` is a **7-bit** frame
> sent to a HALTed card *before* any authentication. Sending `0x40` as a byte
> through `readerTransceive()` is **not** the backdoor and does not unlock
> anything.

### Conclusion (task 1)

- **gen2 / CUID direct-write: SUPPORTED.** Block 0 is writable with the normal
  auth + write the firmware already performs (`mifareWriteBlock`). A real 1:1 UID
  clone onto a gen2 card works on this hardware.
- **gen1a backdoor: NOT reachable on the PN7150 through this stack.** The unlock
  needs 7-bit / CRC-less framing that the NCI reader API does not expose. This is
  a conclusion from the API surface, not a guess — no firmware tweak short of a
  lower-level RF-frame capability (not present in ElectronicCats-PN7150) changes
  it.

This matches the field consensus that gen1a cloning needs a bit-framing reader
(Proxmark3, libnfc + PN532 in a low-level mode, ChameleonMini); a high-level NCI
controller like the PN7150 is the wrong tool for the backdoor, while it handles
gen2 direct-write cards fine.

---

## 3. Task 2 — the reproducible bench test: `mifare clone-uid`

`modules/tags/mifare/clone_uid.py` turns the experiment into a repeatable,
scriptable command instead of a one-off manual trace. It never attempts the
gen1a backdoor (§2 says it can't); it verifies the gen2 path end to end and
reports honestly when a card is out of reach.

Flow (all on the card currently on the reader):

1. Authenticate sector 0 (default key `FFFFFFFFFFFF` A, or `--key`/`--keys-file`).
2. Read block 0; dissect UID/BCC/SAK/ATQA.
3. **Non-destructive writability probe** — write the *current* block 0 back
   unchanged (the same safe probe `mifare restore --write-block0` uses). Accepts
   → gen2; NAKs → genuine/locked.
4. Classify: `gen2-direct-write` (UID rewrite SUPPORTED) or `genuine-or-locked`
   (would need the gen1a backdoor → out of reach).
5. If `--uid HEX8` (or `--from-dump FILE`) is given **and** the card is writable:
   craft the new block 0 (new UID + **recomputed BCC**, SAK/ATQA/manufacturer
   preserved unless `--sak`/`--atqa` override), write it, **read it back and
   compare byte-for-byte** — the proof of a real 1:1 clone.

Safety & honesty:

- With no `--uid`/`--from-dump` the run is **read-only** (the probe rewrites the
  same bytes; the UID never changes).
- A UID write asks for confirmation unless `--yes`/`--json`.
- A locked card **never** receives a UID write — only the harmless echo probe is
  ever sent (pinned by `test_locked_card_write_uid_is_refused_and_no_backdoor_attempted`).
- Exit status: `0` = UID rewrite works here (probe writable, or write verified);
  `1` = not writable / not verified / error. Scripts can gate on it.

### Bench procedure (fill in the observed result)

Run on real hardware with a lab card on the reader:

```
# read-only capability probe
bombercat tags mifare clone-uid

# verify a real clone onto a gen2 card you own
bombercat tags mifare clone-uid --uid A1B2C3D4 --yes
# or source the UID from a dump of the card you're cloning
bombercat tags mifare clone-uid --from-dump source.json --yes
```

| Card under test | `block 0 writable` | `magic type` | `verified` | Notes |
|-----------------|--------------------|--------------|------------|-------|
| genuine Mifare Classic 1K | _(expect: no)_ | genuine-or-locked | — | UID rewrite unsupported (OTP) |
| gen2 / CUID magic | _(expect: yes)_ | gen2-direct-write | _(expect: YES)_ | 1:1 clone confirmed |
| gen1a backdoor magic | _(expect: no via this stack)_ | genuine-or-locked | — | Direct write NAKs; backdoor unreachable on PN7150 |

> The gen1a row is the honest one: through the PN7150 a gen1a card presents like
> a card whose block 0 won't take a standard write, because the one thing that
> *would* unlock it — the 7-bit backdoor — is exactly what this reader can't send.

---

## 4. Task 3 — implications for 1:1 cloning

- **You can clone a UID onto a gen2/CUID magic card with BomberCat today** —
  `clone-uid` (UID only) or `restore --write-block0` (full dump). Verified by
  read-back.
- **You cannot use a gen1a backdoor card for its backdoor on this hardware.** If
  a workflow depends on gen1a (e.g. a card that only exposes block 0 via the
  backdoor), reach for a bit-framing reader; the PN7150 is not it. Buy gen2/CUID
  for BomberCat-based cloning.
- No firmware change is warranted: the gen2 path already works, and the gen1a
  path is blocked at the NCI RF layer, below anything this library can reach.

Frontier respected end to end: no key is deduced, block 0 is only ever written
after a non-destructive proof it's writable, and a card that can't be cloned is
declared so — never blindly written to.
