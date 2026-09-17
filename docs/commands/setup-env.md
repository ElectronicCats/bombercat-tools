# `bombercat setup-env`

> Grant this machine access to a BomberCat: udev rules and groups. (Linux only.)

## Quick Start

```sh
sudo bombercat setup-env
# then log out and back in
bombercat device list
```

From a source checkout, name the interpreter so `sudo` finds the dependencies:

```sh
sudo $(which python3) bombercat.py setup-env
```

---

## What it does

1. Writes `/etc/udev/rules.d/99-bombercat.rules` — the same rules the `.deb`
   and Arch packages install: the BomberCat CDC port (every firmware
   personality, plus the Arduino USB identity) to `dialout`, and the BOOTSEL
   mass-storage volume that [`flash`](flash.md) writes to, to `plugdev`.
2. Adds you to `dialout` and `plugdev`, creating either group if your distro
   does not ship it (`plugdev` is absent on Arch and Fedora).
3. Reloads udev (`udevadm control --reload-rules` + `trigger`) so a board
   that is already plugged in picks the rules up.

```sh
sudo bombercat setup-env
```

```
✓ Udev rules installed to /etc/udev/rules.d/99-bombercat.rules
✓ User 'darcko' added to group 'dialout'
✓ User 'darcko' added to group 'plugdev'
✓ Udev rules reloaded
✓ Environment setup complete!
ℹ Log out and back in for the group changes to take effect, then:
  bombercat device list
```

---

### Notes

- Linux only — udev and `usermod` do not exist on macOS or Windows, so the
  command is not registered there.
- Needs root; without it you get the exact `sudo` line to re-run.
- The `.deb` and Arch packages already do all of this at install time. Run it
  after a source checkout, a `pip install`, or on a distro we do not package.
- Group membership is applied at login, so the log out / log back in is not
  optional. For a one-off session, `newgrp dialout` works in that shell only.
- Safe to re-run: the rules file is rewritten and `usermod -aG` is additive.
- If it exits with an error, every step it *could* do was still done — the
  message names the one that failed.
