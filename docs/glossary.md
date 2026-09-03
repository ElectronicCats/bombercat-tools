# Glossary

Terms used across these docs, in one place. Alphabetical.

---

<a id="apdu"></a>
### APDU

**A**pplication **P**rotocol **D**ata **U**nit — the command/response unit
smart cards and NFC terminals exchange (ISO/IEC 7816-4). A relay session moves
these between a reader and a card/emulator; [`capture`](commands/capture.md)
taps a copy of each one off the control link and writes it to pcap. See the
`:apdu` event in [Control protocol](protocol.md#apdu-capture-events).

<a id="atqb-sensf_res"></a>
### ATQB / SENSF_RES

PN7150 anticollision replies during NFC-B and NFC-F polling, respectively.
NFC-B's UID (the PUPI) lives in bytes 1-4 of the ATQB response; NFC-F's UID
(the IDm) lives in bytes 1-8 of the SENSF_RES response. See
[`tags`: UID shows "unavailable"](troubleshooting.md#tags-uid-unavailable).

<a id="control-plane"></a>
### Control plane

The USB-serial link between the CLI and a board — commands and status only,
115200 baud, line-based ASCII. **Never** carries relayed APDUs; those go over
WiFi/TCP through the [`nfcgate-server`](#nfcgate-server). See
[Control protocol](protocol.md).

<a id="devicelink"></a>
### `DeviceLink`

The host-side Python client (`modules/core/bombercat.py`) that speaks the
[control-plane](#control-plane) protocol: sends commands, parses `+OK`/`-ERR`
replies, and streams raw lines for `monitor`/`capture`. See
[The `DeviceLink` client](protocol.md#the-devicelink-client).

<a id="emv"></a>
### EMV

The chip-card payment standard (named for **E**uropay, **M**astercard,
**V**isa) that a card's [Service Code](#service-code) can require instead of
plain magstripe. `magspoof nfc visa`/`nfc read` speak the
PPSE/VISA-AID/GPO/READ-RECORD exchange over NFC to emulate or read one. See
[`magspoof nfc`](commands/magspoof.md#magspoof-nfc).

<a id="handshake"></a>
### Handshake

The CLI's `ping` → `+OK bombercat` exchange that confirms a board is running
firmware serving the [control REPL](#repl) — distinct from just being
enumerated over USB. See
[Board present by USB id but no handshake](troubleshooting.md#board-present-but-no-handshake).

<a id="iso-dep"></a>
### ISO-DEP

The NFC protocol layer (ISO/IEC 14443-4) that carries [EMV](#emv) APDUs over
contactless. A card advertises ISO-DEP support (or not) via its
[SEL_RES](#sel_res) bit — `chip` means "ISO-DEP/EMV available", `nochip`
forces a terminal to fall back to magstripe. See
[`magspoof nfc selres`](commands/magspoof.md#magspoof-nfc-selres).

<a id="msd"></a>
### MSD

**M**ag**S**tripe **D**ata mode — the contactless emulation profile that
replays a card's magstripe track data (rather than EMV chip data) over NFC.
`magspoof nfc visa` runs a VISA MSD session. See
[`magspoof nfc visa`](commands/magspoof.md#magspoof-nfc-visa).

<a id="ndjson"></a>
### NDJSON

Newline-delimited JSON — one JSON object per line, no enclosing array. Used by
every `--json`/`watch --json` stream in this CLI (`tags watch`, `readers
watch`, `magspoof watch`, …) so output stays pipeable to `jq` line-by-line
without buffering the whole run.

<a id="nfcgate-server"></a>
### `nfcgate-server`

The relay backend two BomberCat peers (or a BomberCat and the NFCGate Android
app) connect to over WiFi/TCP to exchange relayed APDUs. Run locally for
testing with [`testserver`](commands/testserver.md); the control plane never
touches it directly — only `relay run` does, on the board.

<a id="pn7150"></a>
### PN7150

The NXP NFC front-end chip on BomberCat boards. Runs in reader mode (polling
for tags/cards) or emulation mode (card emulation via
[SEL_RES](#sel_res)/[MSD](#msd)), never both at once — switching modes is what
[`magspoof nfc info`](commands/magspoof.md#magspoof-nfc-info) reports.

<a id="ppse"></a>
### PPSE

**P**roximity **P**ayment **S**ystem **E**nvironment — the first APDU exchange
in an [EMV](#emv) contactless transaction, used to discover which payment
application (AID) a card or emulator offers. Part of the
PPSE/VISA-AID/GPO/READ-RECORD sequence `magspoof nfc visa`/`nfc read` run.

<a id="repl"></a>
### REPL

The board firmware's line-based command loop that answers the
[control-plane](#control-plane) protocol (`ping`, `info`, `set`, …, and
firmware-specific commands like `magplay`). "Serving the REPL" is shorthand
for "will answer the handshake" — a board can be powered and enumerated over
USB without its firmware doing this (wrong sketch, autostart blocking
`setup()`, etc.). See
[Board present by USB id but no handshake](troubleshooting.md#board-present-but-no-handshake).

<a id="sel_res"></a>
### SEL_RES

The PN7150's Select Response bit that tells a terminal whether a card/emulator
supports [ISO-DEP](#iso-dep)/[EMV](#emv) (`chip`, `0x33`) or not
(`nochip`/MSD fallback, `0x13`). Set per-session with
[`nfc selres`](commands/magspoof.md#magspoof-nfc-selres) or pinned per-card
with [`card set --nfc`](commands/magspoof.md#magspoof-card-set).

<a id="service-code"></a>
### Service Code

The 3-digit field in a financial card's Track 2 (ISO 7813) that states
whether a terminal must use chip and/or PIN. `magspoof show` decodes it and
warns when a swipe may be refused;
[`card normalize-sc`](commands/magspoof.md#magspoof-card-normalize-sc) /
[`card require-sc`](commands/magspoof.md#magspoof-card-require-sc) rewrite it
(normalize is **FOR AUTHORIZED TESTING ONLY**). See
[`magspoof show`: "chip required"](troubleshooting.md#magspoof-chip-required-warning).

<a id="track-1-track-2"></a>
### Track 1 / Track 2

The two magnetic-stripe data tracks ISO 7813 defines. Track 1 (`%…?`) carries
the cardholder name and is alphanumeric; Track 2 (`;…?`) is numeric-only and
carries the PAN, expiry, and [Service Code](#service-code) — the track
terminals actually read for a swipe. `magspoof card add`/`card set` take
either or both. See [`magspoof card`](commands/magspoof.md#magspoof-card).

<a id="uf2"></a>
### UF2

**U**SB **F**lashing **F**ormat — the file format the RP2040's ROM bootloader
accepts. `bombercat flash` copies a `.uf2` straight to the `RPI-RP2` mass
storage drive that appears in bootloader mode. See
[`flash`: no `RPI-RP2` drive appears](troubleshooting.md#no-rpi-rp2-drive).

<a id="uid"></a>
### UID

The tag/card unique identifier reported by `tags`/`readers`/`magspoof nfc
info`. For most NFC types it's read directly off the anticollision reply; for
NFC-B/NFC-F on older firmware it's [unavailable](troubleshooting.md#tags-uid-unavailable)
because the published sketch never extracted it (see
[ATQB / SENSF_RES](#atqb-sensf_res)).

<a id="vid-pid"></a>
### VID / PID

The USB Vendor ID / Product ID pair a board enumerates with. BomberCat's
stock identity is `1209:005E`; a board reflashed with a custom identity needs
`BOMBERCAT_VID`/`BOMBERCAT_PID` set (both) for discovery to tag its port as a
candidate. See [Environment variables](reference.md#environment-variables).
