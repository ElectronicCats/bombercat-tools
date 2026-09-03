# Current limitations

Where the tool stands today (`VERSION` 1.1.0.0) — known constraints, not bugs.

- [Platform](#platform)
- [Host requirements it cannot work around](#host-requirements-it-cannot-work-around)
- [Devices and serial](#devices-and-serial)
- [Relay scope](#relay-scope)

---

<a id="platform"></a>
## Platform

Everything here is developed and validated on **Linux**; that is the only OS the
whole tool is exercised on. The control plane is plain `pyserial` with nothing
Linux-specific in it, but several commands shell out to `bash` scripts or to
POSIX-only APIs, so support thins out elsewhere:

| Feature | Linux | macOS | Windows |
|---|---|---|---|
| `device`, `status`, `relay …` (`config`/`run`/`stop`/`status`/`monitor`) | tested | should work, untested | should work, untested |
| `tags …` (`read`/`watch`/`scan`/`info`) | tested | should work, untested | should work, untested |
| `readers …` (`read`/`watch`/`scan`/`info`) | tested | should work, untested | should work, untested |
| `magspoof …` (`play`/`show`/`watch`/`info`, `nfc *`, `card *`) | tested | should work, untested | should work, untested |
| `capture start -o file.pcap` | tested | should work, untested | should work, untested |
| `capture start -ws` (live Wireshark) | tested | FIFO path, untested | needs `pywin32`, untested |
| `completion install` | bash/zsh/fish | bash/zsh/fish | not offered |
| `proto gen` | tested | should work | needs `bash` (WSL / Git Bash) |
| `testserver run` | tested | needs Docker Desktop | needs `bash` + Docker |
| `tools/tests/` unit tests (`pytest`) | tested | should work | FIFO/`pywin32` tests skip themselves |
| `tools/tests/` host tests | tested | should work | `serialctl_hosttest.py` needs `os.openpty()` |
| `testserver/codec_hosttest` | tested | needs `g++` | needs `bash` + `g++` |

Concretely, the non-Linux gaps are:

- `bombercat completion` is only registered on Linux/macOS
  ([modules/core/cli.py](../modules/core/cli.py)); on Windows it is absent from
  `--help` and refuses to install.
- `proto gen` and `testserver run` are wrappers that run `bash gen_proto.sh` /
  `bash testserver/run.sh`, so they need a POSIX shell.
- `testserver run` needs Docker **and** a user who can reach its socket. The
  preflight diagnoses `docker`-group membership through the `grp` module, which
  only exists on Unix; elsewhere it can only report "unknown" and give a generic
  hint.
- The Wireshark launcher knows install paths for Windows/Linux/Darwin only; any
  other OS gets *"We don't support this OS yet"*.
- Serial access on Linux needs the `dialout` group, and the firmware flasher
  ([`flash_bombercat.sh`](https://github.com/ElectronicCats/bombercat-firmware/blob/main/flash_bombercat.sh)) auto-installs
  its dependencies through `apt` — on non-Debian distros you install
  `arduino-cli` yourself.

<a id="host-requirements-it-cannot-work-around"></a>
## Host requirements it cannot work around

- **Live capture needs Wireshark installed locally**, in one of the usual
  install paths or on `PATH`. A flatpak/snap install that exports no `wireshark`
  wrapper is not detected. There is no remote capture (no SSH, no extcap).
- **The Windows named pipe name is fixed** (`\\.\pipe\fbombercat`) with no flag
  to change it, so on Windows there can be **only one live capture per host**;
  capturing both boards live at once collides on it — capture one side with
  `-o file.pcap` and the other with `-ws`, or do them one after the other. On
  Unix the FIFO now lives in a private, per-invocation temp directory, so
  concurrent live captures no longer collide there.
- **Classic pcap only.** The writer emits classic pcap with `DLT_ISO_14443`; no
  pcapng, no per-packet comments
  ([capture.md](commands/capture.md#classic-pcap-vs-pcapng)).
- `tools/tests/capture_hosttest.py` checks the dissection only when `tshark` is
  installed; without it that half of the test is skipped.
- `testserver smoke` needs the classic protobuf 3.x runtime. If the interpreter
  running the CLI lacks it, a throwaway venv is bootstrapped in
  `tools/.venv-smoke` (same idea as `.venv-proto` for `proto gen`).

<a id="devices-and-serial"></a>
## Devices and serial

- **One command per board at a time.** The port is not opened exclusively, so a
  second command against the same board steals bytes from the first — `monitor`
  and `capture start` cannot share one BomberCat. Two *different* boards in
  parallel are fine.
- **`monitor` raises the firmware log level to Debug** while it runs (restored
  on exit), which makes the relay's hot path chattier — worth remembering before
  measuring latency with it open.
- **Device IDs are stable only while the set of attached boards is unchanged.**
  They are derived from the USB iSerial (or the USB topology location) and then
  numbered in order, so plugging in or removing a board renumbers the rest.
  Re-check with `bombercat device list` before reusing a `-d` from an earlier
  session.
- **VID/PID matching is a hint, not proof.** Sketches built against the stock
  Arduino Mbed profile enumerate as `2341:005E`, so a real Arduino Nano RP2040
  Connect on the same host is tagged as a candidate too. Only the ✓ in
  `device list` (the control handshake) confirms a board is a BomberCat.
- **Firmware floors.** `capture` needs firmware ≥ v0.8.0, and `identify` /
  `loglevel` need a recent build; against older firmware those commands fail
  with `-ERR unknown command`
  ([troubleshooting.md](troubleshooting.md#old-firmware)).
- **Every published DetectTags `.uf2` predates structured `:tag` events.**
  `tags` falls back to parsing the older `displayCardInfo()` text, which never
  prints a UID at all for NFC-B/NFC-F tags
  ([troubleshooting.md](troubleshooting.md#tags-uid-unavailable)).
  `bombercat tags info` reports which mode a given board speaks.
- **`flash` writes published images, it does not build them.** `bombercat
  flash` downloads the prebuilt `.uf2` files from the
  [bombercat-firmware](https://github.com/ElectronicCats/bombercat-firmware)
  releases and copies them to the board's UF2 bootloader — so it can only give
  you what a release published. Compiling your own changes is still
  [`flash_bombercat.sh`](https://github.com/ElectronicCats/bombercat-firmware/blob/main/flash_bombercat.sh)'s job in the
  firmware repo (`bash` + `arduino-cli`). The bootloader drive must auto-mount; on a headless host
  without `udisks` you mount it yourself
  ([troubleshooting.md](troubleshooting.md#no-rpi-rp2-drive)).

<a id="relay-scope"></a>
## Relay scope

- **Exactly two peers per session** — one `--role reader` and one `--role card`,
  sharing a `--server` and a `--session` (1–255). No multi-peer sessions, and no
  more than one relay session per board.
- **`run` has a fixed 45 s bring-up budget**, not adjustable by flag. A timeout
  does not wedge the board: the REPL stays live, so `status`/`monitor` keep
  answering ([troubleshooting.md](troubleshooting.md#run-times-out)).
- **Nothing in the chain is authenticated or encrypted.** The serial control
  channel is plaintext (the WiFi passphrase travels in the clear and is
  persisted to the board's flash unless you pass `--no-save`), and the
  peer↔server link is plain TCP, exactly as upstream NFCGate. Use it on a
  network you control.
- **Path B, variant B1** (BomberCat as `reader` against the NFCGate Android app
  emulating the card) requires a **rooted** phone with Xposed and NFCGate's
  native hook — it is not possible on a stock phone. Variant B2 (BomberCat as
  `card`, phone as reader) works on a stock device. See
  [docs/HARDWARE_TESTING.md](HARDWARE_TESTING.md).

---

Deploying your own permanent relay server? See [Deployment](deployment.md).
