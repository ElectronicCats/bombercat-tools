# Troubleshooting

Common failure modes when driving a BomberCat from the CLI, and how to fix them.
Every handled error is a clean one-line message (leading `✗`) with exit code 1 —
you should never see a Python traceback. If you do, that's a bug worth reporting.

- [Serial permission denied](#serial-permission-denied)
- [No BomberCat found / board not detected](#board-not-detected)
- [Board present by USB id but no handshake](#board-present-but-no-handshake)
- [Wrong board answers to `-d`](#wrong-board)
- [Old firmware without `identify` / `capture`](#old-firmware)
- [`flash`: no `RPI-RP2` drive appears](#no-rpi-rp2-drive)
- [`run` times out](#run-times-out)
- [`peer present` stays `no`](#peer-stays-no)
- [Capture: Wireshark doesn't open / no frames](#capture-issues)
- [`tags`: no tags detected](#no-tags-detected)
- [`tags`: UID shows "unavailable" for NFC-B/NFC-F](#tags-uid-unavailable)
- [`magspoof`: the physical button only plays track 2](#magspoof-button-track-2)
- [`magspoof`: every command answers `-ERR unknown command`](#magspoof-unknown-command)
- [`magspoof nfc visa`/`nfc read`: tap wait times out with no error](#magspoof-nfc-tap-timeout)
- [`magspoof show`: "chip required — swipe may be refused"](#magspoof-chip-required-warning)
- [`testserver` errors](#testserver-issues)

---

<a id="serial-permission-denied"></a>
## Serial permission denied

Symptom: a `PermissionError` / `SerialException` opening `/dev/ttyACM*`.

On Linux, serial access needs your user in the `dialout` group:

```sh
sudo usermod -aG dialout $USER
# then log out and back in (group membership is applied at login)
```

Verify with `groups | grep dialout`. A quick one-off without re-login:
`sudo chmod a+rw /dev/ttyACM0` (resets on replug).

<a id="board-not-detected"></a>
## No BomberCat found / board not detected

Symptom:

```
✗ no BomberCat found; pass --port (e.g. --port /dev/ttyACM0)
```

Check, in order:

1. **Is it enumerated at all?** `bombercat device list -a` shows every serial
   port (including non-candidates). If your board isn't there, it's a cable /
   power / driver problem, not a CLI one. On Linux, `ls /dev/ttyACM*`.
2. **Is it a candidate?** In `bombercat device list`, a BomberCat should show a
   `✓` (answered) or `USB id` (recognized but silent). Neither → its USB
   VID/PID isn't recognized. If you re-flashed it with a custom USB identity,
   declare it:
   ```sh
   BOMBERCAT_VID=0x1209 BOMBERCAT_PID=0x005E bombercat device list
   ```
3. **Bypass discovery** by naming the port directly: `bombercat device info -p /dev/ttyACM0`.

<a id="board-present-but-no-handshake"></a>
## Board present by USB id but no handshake

Symptom: `device list` shows `USB id` (yellow) instead of `✓`, or:

```
✗ a BomberCat is connected at /dev/ttyACM0 (USB …) but it did not answer the
  handshake — is it running the NFCGate relay firmware?
```

The board is there, but its firmware isn't serving the control REPL. Usually one
of:

- **It isn't running the NFCGate relay firmware.** Check with `bombercat
  status`, and flash it: `bombercat flash NFCGate`
  ([flash](commands/flash.md)). Afterwards `bombercat device info` should
  show a `fw` version and `state idle`. To build from source instead, see
  [`firmware/NFCGate/README.md`](../../firmware/NFCGate/README.md).
- **`RELAY_AUTOSTART = 1` with a non-empty SSID.** The sketch then blocks on the
  WiFi/TCP bring-up in `setup()` before the REPL starts, so the CLI can't reach
  it. Set `RELAY_AUTOSTART = 0` (the default, required for CLI-driven use) and
  reflash.
- **Wrong sketch / a wedged firmware** that stops draining its USB-OUT endpoint —
  the CLI bounds writes with a timeout and reports:
  ```
  ✗ device did not accept 'ping' (write timed out); it may be wedged or not
    running the relay firmware
  ```
  Power-cycle the board and reflash the relay firmware.

<a id="wrong-board"></a>
## Wrong board answers to `-d`

If a command hits the wrong physical board, confirm the mapping — blink each ID:

```sh
bombercat identify -d 1
bombercat identify -d 2
```

IDs are derived from a stable USB identity and survive replugs/reboots **as long
as the same set of boards is attached**. Adding or removing a board can renumber
the rest. Also: if **no** port carries a BomberCat USB id, every candidate port
gets numbered and `device list` warns you — the IDs are then just "whatever
serial ports exist", so verify before trusting `-d`.

<a id="old-firmware"></a>
## Old firmware without `identify` / `capture`

The CLI can be newer than the board's firmware. You'll see:

- `identify` → `✗ identify failed: unknown command` and a hint that the firmware
  predates `identify` (needs ≥ 0.7.0). `config` still works — the LED-blink after
  a successful config is skipped with a warning.
- `capture start` → `✗ could not arm capture: unknown command` and a hint to
  reflash `firmware/NFCGate` (needs ≥ 0.8.0).

Fix: reflash the current NFCGate firmware — `bombercat flash NFCGate`
([flash](commands/flash.md)), or build it from source with
[`firmware/NFCGate/README.md`](../../firmware/NFCGate/README.md). Check the
version afterwards with `bombercat device info` (the `fw` field).

<a id="no-rpi-rp2-drive"></a>
## `flash`: no `RPI-RP2` drive appears

Symptom: `bombercat flash <name>` reboots the board and then gives up on the
bootloader drive:

```
╭─ Board did not enter bootloader mode ────────────────────────────────────╮
│  ✗  No RPI-RP2 drive appeared after the 1200-bps reset.                  │
│  …                                                                       │
│  How to fix it                                                           │
│    1. Double-tap the RESET button on the board.                          │
│    2. Check that a drive named RPI-RP2 appears.                          │
│    3. Run bombercat flash NFCGate again — it will find the drive and     │
│       copy straight to it.                                               │
╰──────────────────────────────────────────────────────────────────────────╯
```

Flashing needs the RP2040's UF2 bootloader to show up as a mounted drive named
`RPI-RP2`. Two different things stop that:

**1. The board never entered bootloader mode.** The 1200-bps touch is a
convention the running firmware implements; a sketch built against a different
core simply ignores it. The bootloader itself is in ROM and always reachable by
hand:

1. **Double-tap the RESET button** on the board.
2. Check that the drive appears (`ls /media/$USER/RPI-RP2`, or your file
   manager).
3. Run the same `bombercat flash <name>` again — it detects the drive and
   copies straight to it, without touching the serial port.

That manual route also works when the board is running a firmware with no
serial port at all.

**2. The drive is not auto-mounted.** On a headless box (or WSL) without
`udisks`, the kernel sees the bootloader but nothing mounts it. `flash` detects
this case and says so, because mounting needs privileges it will not take on its
own:

```
╭─ Bootloader drive not mounted ───────────────────────────────────────────╮
│  ✗  The board is in bootloader mode, but RPI-RP2 is not mounted.         │
╰──────────────────────────────────────────────────────────────────────────╯
```

Mount it and retry:

```sh
udisksctl mount -b /dev/sdX1                  # or:
sudo mkdir -p /mnt/RPI-RP2 && sudo mount /dev/sdX1 /mnt/RPI-RP2
```

The panel prints the actual device node in place of `/dev/sdX1`, and the exact
commands to run. `flash` looks the drive up in `/proc/mounts` and, failing that,
globs `/media/*`, `/media/*/*`, `/run/media/*/*`, `/mnt/*` and `/mnt/RPI-RP2`,
so any of those mount points works.

Related: a copy that ends in `OSError` after every byte was written is **not** a
failure — the bootloader restarts the board the moment it has the last block,
sometimes before the kernel finishes the write. `flash` treats that as success
and only reports an error on a short write.

<a id="run-times-out"></a>
## `run` times out

`run` waits up to **45 s** for the relay to reach `relaying`. If it doesn't:

```
✗ relay did not reach 'relaying' in time.
ℹ still 'connecting nfcgate-server' after 45s — the bring-up is slow or stuck
  (the device is still responsive).
```

The device is **not** wedged — the REPL stayed live, so keep diagnosing:

- **WiFi**: wrong SSID/password? `bombercat relay config show` and re-run `relay config wifi`.
- **Server reachable?** From the same network: `nc -vz <host> <port>`. Is the
  `nfcgate-server` actually listening? For a bench server, `bombercat testserver run`.
- **PN7150 / NFC bring-up**: `bombercat relay monitor` shows where it's stuck.
- If `run` was **rejected** outright (`✗ relay rejected 'run': …`), the config is
  incomplete (e.g. empty SSID) or it's already running — check `config show` /
  `status`.

<a id="peer-stays-no"></a>
## `peer present` stays `no`

Both peers must share the **same `--server` and the same `--session`**, with
**opposite roles** (`reader` / `card`). A mismatched session byte means each peer
joins a different session and they never see each other. `session == 0` is treated
as a disconnect — use `1..255`. Confirm both with `bombercat relay config show` on each
board (or check the app's session/role for Path B).

<a id="capture-issues"></a>
## Capture: Wireshark doesn't open / no frames

- **"Wireshark not found"** — install Wireshark, or capture to a file only with
  `-o file.pcap` (no Wireshark needed). The CLI probes the usual install
  locations and `PATH`.
- **"nothing to do"** — you passed neither `-ws` nor `-o`. Pass at least one.
- **Wireshark opens but no packets** — no APDUs are flowing. Capture only shows
  frames while the relay is actually relaying: `bombercat relay status` should be
  `relaying` with a peer present, and a real transaction must be happening (card
  on the reader, terminal on the card board).
- **"Wireshark did not attach to the pipe in time"** — it took longer than 30 s
  to open the FIFO; with `-o` also given, capture falls back to the file.
- A **classic pcap** written by the CLI vs a **pcapng** saved from Wireshark is
  expected — see [Capture / Wireshark](commands/capture.md#classic-pcap-vs-pcapng).

<a id="no-tags-detected"></a>
## `tags`: no tags detected

`bombercat tags watch`/`scan` looks idle with a card actually on the reader:

- **It might not be idle — just quiet.** `watch` hides firmware boot/idle
  chatter (`Restarting…`, `Waiting for a Card…`, `Card removed!`) by default.
  Pass `--no-quiet-noise` (or `-v`, which always shows it) to see the raw
  loop — if that's flooding by but no `:tag`/`Remote … activated` line ever
  shows up, the PN7150 isn't seeing the card, not the CLI dropping it.
- **Confirm the link itself with `-v`.** It traces every `>`/`<` line the
  device sends over serial to stderr (`-vv` adds a timestamp), so you can see
  whether the board is talking at all, independent of how `tags` parses it.
- **Check it's actually DetectTags.** `bombercat status` — a board on a
  different firmware won't emit tag lines no matter how long you wait; `tags`
  itself catches this earlier at the handshake, not here.
- **`tags read` has a 15s default timeout** (`-t`/`--timeout` to raise it) —
  a slow tap can just run out the clock; retry or use `watch`, which has no
  deadline.
- Card positioning/antenna range issues are a firmware/hardware matter, not
  something the CLI can diagnose — try `bombercat tags info` first to at
  least confirm the board answers and check its `events` mode
  ([tags info](commands/tags.md#tags-info)).

<a id="tags-uid-unavailable"></a>
## `tags`: UID shows "unavailable" for NFC-B/NFC-F

```
uid           unavailable (NFC-B: firmware prints no ID)
```

Not a bug — every *published* DetectTags `.uf2` parses the older,
human-readable `displayCardInfo()` text (see
[Firmwares](commands/status.md#firmware-capability-table)), and on that build **NFC-B** and
**NFC-F** never printed a UID field at all — there was nothing for the CLI to
read. `tags` reports this honestly rather than inventing a value or leaving
the field blank. NFC-A, MIFARE and ISO15693 all print their UID and are
unaffected.

The fix is on the firmware side, and it now exists in
`DetectTags.ino` source: NFC-B's UID is the PUPI (bytes 1-4 of the ATQB
response) and NFC-F's is the IDm (bytes 1-8 of the SENSF_RES response) —
both already captured by the PN7150 library but never extracted before. A
board built from the updated sketch reports the real UID (plus `attrib=`/
`bitrate=` extras) instead of `unavailable`. It isn't in a published release
yet, so boards running today's `.uf2` still show this message until
reflashed.

<a id="magspoof-button-track-2"></a>
## `magspoof`: the physical button only plays track 2

```
$ bombercat magspoof show
button        track 2 only
```

The button mode is a store-wide setting in the firmware, and `track 2 only`
pins every press to track 2 — so a two-track financial card gets swiped
half-way and a track-1-only membership card does nothing at all.

The CLI never sets this. The firmware default is `alternating 1 and 2`, which
on current images means "play whatever the active card carries" — both tracks
for a financial card, the lone track for a membership one — so a board only
reaches `track 2 only` if someone sent `magbtn 2` over a raw serial console.

There is no `magspoof` command to undo it (that is deliberate: nothing in the
CLI can push a board into the pinned mode either). Send the reset from the same
raw console — screen, minicom, the Arduino IDE monitor, 115200 baud:

```
magbtn alt
```

`bombercat magspoof show` should then report `alternating 1 and 2`, and the
button follows the active card again. A firmware factory reset clears it too.

<a id="magspoof-unknown-command"></a>
## `magspoof`: every command answers `-ERR unknown command`

```
✗ play failed: unknown command
ℹ this firmware predates magplay/magset/magget/magbtn — reflash the magspoof
  image (bombercat flash magspoof) to use it.
```

Two different causes give the same reply:

- **Old `magspoof` image.** Boards built before the `magplay`/`magset`/`magget`/
  `magbtn` REPL hook was added answer every `magspoof …` subcommand this way —
  and it's `1.1.1.0` either side of that change, so the version alone doesn't
  tell you. Reflash: `bombercat flash magspoof`.
- **Wrong firmware entirely.** `MagspoofCVSAttack` and `MagSpoofMqtt` boot with
  the same banner as `magspoof` but don't expose this REPL surface at all —
  confirm what's actually flashed with `bombercat status` before assuming the
  image is just stale. See [`magspoof`](commands/magspoof.md#subcommands).

<a id="magspoof-nfc-tap-timeout"></a>
## `magspoof nfc visa`/`nfc read`: tap wait times out with no error

Symptom: the command sits on `waiting for a contactless tap (up to 15s)...` /
`waiting for a card (up to 8s)...` and then reports failure — no crash, no
malformed data, just nothing detected in the window.

- **Antenna positioning/range** is a hardware matter the CLI can't diagnose —
  present the card/reader flat against the PN7150 antenna, not at an angle or
  edge-on.
- **Check what mode the PN7150 is actually in first**:
  `bombercat magspoof nfc info` reports `mode` (`reader`/emulation) and
  `SEL_RES` without touching either — if a prior [`nfc selres`](commands/magspoof.md#magspoof-nfc-selres)
  override or a different command left it in an unexpected state, that
  explains a silent timeout better than retrying blind.
- **`nfc visa` needs a reader on the other end**, not another BomberCat in
  reader mode — it emulates a card, so tap it *to* a terminal/PN7150 running
  as reader. `nfc read` is the reverse: it expects a physical EMV/Visa card
  presented *to* the BomberCat.
- Retrying costs nothing — both commands just re-arm the same wait.

<a id="magspoof-chip-required-warning"></a>
## `magspoof show`: "chip required — swipe may be refused"

```
security      ⚠ chip required — swipe may be refused
chip required yes
PIN required  no

ℹ use: bombercat magspoof card normalize-sc --apply
```

Not an error — [`magspoof show`](commands/magspoof.md#magspoof-show) is reading the
card's own Track 2 Service Code and reporting, honestly, that a terminal may
reject a plain magstripe swipe of this card because its Service Code demands
chip (and/or PIN) verification. This is expected for any real financial card
data; a magstripe-only swipe genuinely can't satisfy that requirement.

The CLI hint points at [`card normalize-sc`](commands/magspoof.md#magspoof-card-normalize-sc),
which rewrites the Service Code to waive chip/PIN so a swipe is accepted —
**FOR AUTHORIZED TESTING ONLY**, since it's deliberately weakening the card's
stated security requirements. [`card require-sc`](commands/magspoof.md#magspoof-card-require-sc)
reverses it if you need the original requirement back.

<a id="testserver-issues"></a>
## `testserver` errors

- **`testserver run`**: needs Docker on `PATH` **and** the server fetched once
  (`tools/testserver/fetch_server.sh`) — the clone at `<repo>/server` is the
  Docker *build context*, not just a dependency of `smoke`. Full list in the
  [testserver run](commands/testserver.md#testserver-run).
  The CLI pre-checks all of it *before* building and prints a framed panel with
  the numbered commands that fix it, so you should never see a raw `docker
  build` error. What each panel means:
  - **The nfcgate-server sources are missing** — the server was never fetched.
    On a terminal the CLI offers to run `tools/testserver/fetch_server.sh` for
    you; answer `n` to do it yourself. The clone at `<repo>/server` is the
    Docker *build context*, not just a dependency of `smoke`.
  - **Docker is not installed, or not on your PATH** — install Docker Engine,
    or run the server without Docker (see
    [`testserver/README.md`](../testserver/README.md)).
  - **Docker refused the connection: permission denied on its socket** — the
    panel tells you which of the three cases you are in, because the fix
    differs: not in the `docker` group (`sudo usermod -aG docker "$USER"`), a
    member but in a session that predates it (`newgrp docker` — membership is
    only applied at login), or the group is already active and the socket
    refuses anyway (unusual ownership, or rootless Docker).
    If `newgrp` is not installed (it comes from `shadow-utils`, `login` on
    Debian/Ubuntu — trimmed installs and container images often lack it), the
    panel offers `sg docker -c "$SHELL"` instead, and when that is missing too
    it says so and points at the only remaining options: log out and back in
    (or reboot), install the package, or run this once under `sudo -E` (the
    panel prints the exact command, with absolute paths, since `sudo` resets
    `PATH`).
  - **The Docker daemon is not running** — `sudo systemctl start docker`, or
    launch Docker Desktop on macOS/Windows.
  - **Host port N is already in use** — the panel distinguishes a test server
    you left running (`docker rm -f bombercat-nfcgate-server-run`) from any
    other program holding the port (`testserver run -p <port>`).
  - `bash is not installed` — the CLI launches the server through
    `tools/testserver/run.sh`, so it needs a shell to do it.

  Running `tools/testserver/run.sh` directly gets the same checks in terse
  form, without the panels.
- **`testserver smoke`**: needs the server fetched once
  (`tools/testserver/fetch_server.sh`) and the `protobuf==3.20.3` runtime. The CLI
  bootstraps a throwaway venv (`tools/.venv-smoke`, override with
  `BOMBERCAT_SMOKE_VENV`) if its own interpreter lacks protobuf. See
  [`testserver/README.md`](../testserver/README.md).
