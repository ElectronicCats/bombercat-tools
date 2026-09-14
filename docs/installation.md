# Installation and uninstallation

Every push to a build path and every GitHub release runs the four
[`build-*.yml`](../.github/workflows/) workflows, which produce the five
installers below and attach them to the release. Grab the one for your
platform from the
[**releases section of the bombercat-tools repository**](https://github.com/ElectronicCats/bombercat-tools/releases/latest).
Building any of them yourself is covered in [`packaging.md`](packaging.md).

| Platform | Asset | Installs |
|---|---|---|
| Windows 10/11 (x64) | `bombercat-x.x.x.x.exe` | `C:\Program Files\BomberCat\` |
| macOS (Intel) | `bombercat-x.x.x.x-x86_64.pkg` | `/usr/local/opt/bombercat/` + `/usr/local/bin/bombercat` |
| macOS (Apple Silicon) | `bombercat-x.x.x.x-arm64.pkg` | `/usr/local/opt/bombercat/` + `/usr/local/bin/bombercat` |
| Debian / Ubuntu | `bombercat-x.x.x.x.deb` | `/usr/bin/bombercat` + `/usr/lib/python3/dist-packages/bombercat/` |
| Arch Linux | `bombercat-x.x.x.x.pkg.tar.zst` | `/usr/bin/bombercat` + `/usr/lib/python3.x/site-packages/bombercat/` |

> [!Note]
> `VERSION` uses four components (`1.2.0.0`) while git tags use semver
> (`v1.2.0`), so the tag and the file name never match exactly — see
> [`release.md`](release.md).

No drivers to install anywhere: the board enumerates as a standard USB CDC
serial device, picked up by the built-in driver on Windows, macOS and Linux
alike.

---

# Installation instructions

## Windows

> [!Important]
> The BomberCat installer requires administrator rights to install on your
> system.

- System requirements:
  - Windows 10 or 11, 64-bit
  - Administrator rights
  - At least 150 MB of free disk space

1.Download the installer **`bombercat-x.x.x.x.exe`** from the
[**releases section of the bombercat-tools repository**](https://github.com/ElectronicCats/bombercat-tools/releases).

2.Run the installer and follow the installation wizard. Two options are worth
reviewing on the *Select Additional Tasks* page:

  - **Add bombercat to the system PATH** — checked by default, and needed to
    run `bombercat` from any terminal. Leave it on.
  - **Create a Desktop icon** — unchecked by default; the CLI is meant to be
    run from a terminal, so the shortcut only opens a console window.

> [!Note]
> You may be prompted with a window indicating that Microsoft Defender
> SmartScreen prevented the installation — the installer is **unsigned** (no
> Authenticode certificate yet). To bypass this prompt, click on **More info**
> and then on the **Run anyway** button.

3.Complete the installation by clicking the **Finish** button to exit the
installer.

4.Open a **new** terminal (PowerShell or `cmd`) — the `PATH` change only
applies to sessions started after the install — and verify:

```powershell
bombercat --version
bombercat device list
```

> [!Note]
> `bombercat setup-env` and `bombercat completion` do not exist on Windows:
> udev, `usermod` and shell completion are Unix-only. Nothing else is needed —
> Windows 10/11 binds the board to its native `usbser` driver with no `.inf`.

---

## macOS

- System requirements:
  - macOS 13 (Ventura) or newer — the packages are built on GitHub's macOS
    runners, older releases are untested
  - Intel or Apple Silicon (M1, M2, M3, M4) Mac
  - At least 150 MB of free disk space

You need to first verify the architecture of your Mac with the command:

```bash
uname -m
```

The command will return any of the following, depending on the Mac
architecture:

- `arm64` = [Apple Silicon (M1/M2/M3/M4)](#apple-silicon-macs-apple-m-processors)
- `x86_64` = [Intel](#intel-macs-x86_64)

Once you know your architecture, continue with the instructions according to
your variant. There is **no universal2 build** — installing the wrong one
gives you a binary that will not run.

### Intel Macs (x86_64)

1.Download the installer **`bombercat-x.x.x.x-x86_64.pkg`** from the
[**releases section of the bombercat-tools repository**](https://github.com/ElectronicCats/bombercat-tools/releases).

2.Open a new terminal session in the path where the installer is located.

3.Run the command:

```bash
sudo installer -allowUntrusted -pkg bombercat-x.x.x.x-x86_64.pkg -target /
```

*Replace the BomberCat name to match the actual name of the installer file.*

### Apple Silicon Macs (Apple M processors)

1.Download the installer **`bombercat-x.x.x.x-arm64.pkg`** from the
[**releases section of the bombercat-tools repository**](https://github.com/ElectronicCats/bombercat-tools/releases).

2.Open a new terminal session in the path where the installer is located.

3.Run the command:

```bash
sudo installer -allowUntrusted -pkg bombercat-x.x.x.x-arm64.pkg -target /
```

*Replace the BomberCat name to match the actual name of the installer file.*

> [!Note]
> The package is **unsigned and not notarized** (no Apple Developer ID yet).
> If Gatekeeper refuses the file because it was downloaded with a browser,
> strip the quarantine flag first and install again:
>
> ```bash
> xattr -d com.apple.quarantine bombercat-x.x.x.x-arm64.pkg
> ```

### Post-installation (macOS)

1.Verify the installation:

```bash
bombercat --version
bombercat device list
```

2.Nothing else is required: macOS exposes the board as `/dev/cu.usbmodem*`
with no group membership or udev rules involved — `setup-env` is a Linux-only
command and is not registered here. Optionally install
[shell completion](#shell-completion-macos-and-linux-systems).

---

## Linux

### Debian/Ubuntu (.deb)

- System requirements:
  - Debian 12+ or Ubuntu 22.04+ (needs `python3` ≥ 3.12)
  - `sudo` privileges
  - At least 100 MB of free disk space

1.Download the installer **`bombercat-x.x.x.x.deb`** from the
[**releases section of the bombercat-tools repository**](https://github.com/ElectronicCats/bombercat-tools/releases).

2.Open a new Terminal session in the installer file location.

3.Run the command:

```bash
sudo apt install ./bombercat-x.x.x.x.deb
```

*Replace the BomberCat name to match the actual name of the installer file.*

`apt` pulls the recommended `udisks2`, which
[`bombercat flash`](commands/flash.md) uses to auto-mount the board's `RPI-RP2`
bootloader drive. If you install with `sudo dpkg -i` instead, recommendations
are skipped, so install the dependencies afterwards:

```bash
sudo apt-get install -f
sudo apt install udisks2
```

> [!Note]
> You can verify BomberCat is installed by running the command:
>
> ```bash
> bombercat --version
> ```

The package only depends on `python3`: `click`, `rich`, `pyserial` and the
rest are vendored inside it, so there is no venv to manage and no risk of
clashing with your system Python packages.

---

### Arch Linux (.pkg.tar.zst)

- System requirements:
  - Arch Linux (or derivatives like Manjaro)
  - `sudo` privileges
  - `python` ≥ 3.12 (the package is built against the current Arch Python)

1.Download the latest version of the installer package.

```bash
URL=$(curl -s https://api.github.com/repos/ElectronicCats/bombercat-tools/releases/latest | grep "browser_download_url.*pkg.tar.zst" | cut -d '"' -f 4) && wget "$URL"
```

2.Install the package by running the command.

```bash
sudo pacman -U bombercat-x.x.x.x.pkg.tar.zst
```

*Replace the BomberCat name to match the actual name of the installer file.*

3.Optionally install `udisks2`, listed as an optional dependency, so
[`flash`](commands/flash.md) can auto-mount the bootloader drive:

```bash
sudo pacman -S udisks2
```

> [!Note]
> *You can verify BomberCat is installed by running the command:*
>
> ```bash
> bombercat --version
> ```

There is no AUR package yet; the `.pkg.tar.zst` from the releases page is the
supported route.

---

### Install from source (all Linux distributions, macOS, Windows)

Needs **Python 3.12 or newer**.

1.Clone the repository.

```bash
git clone https://github.com/ElectronicCats/bombercat-tools.git
```

2.Navigate to the repository folder.

```bash
cd bombercat-tools
```

3.Create and activate a virtual environment (recommended).

```bash
python3 -m venv .venv
source .venv/bin/activate     # On Windows: .venv\Scripts\activate
```

4.Install the package (this pulls the dependencies from `requirements.txt`).

```bash
python3 -m pip install .
bombercat --version
```

To hack on the code instead, install the dependencies only and run the script
in place:

```bash
python3 -m pip install -r requirements.txt
python3 bombercat.py --help
```

In that mode the command is `python3 bombercat.py …` unless you set up the
[`bombercat` alias](reference.md#invocation). The dev-only commands `proto` and
`testserver` are available from a source checkout, and hidden from `--help` in
every packaged build.

---

### Post-installation (all Linux installations)

1.Run the setup command to configure udev rules and user permissions.

```bash
sudo bombercat setup-env
```

This command will:

- Install udev rules for BomberCat devices to
  `/etc/udev/rules.d/99-bombercat.rules`
- Add your user to the `dialout` and `plugdev` groups (creating them if your
  distro does not ship them)
- Reload the udev rules so a board that is already plugged in picks them up

From a source checkout, name the interpreter so `sudo` finds the dependencies:

```bash
sudo $(which python3) bombercat.py setup-env
```

> [!Note]
> The `.deb` and Arch packages already ship the same rules in
> `/lib/udev/rules.d/` and create both groups at install time, but they cannot
> add *you* to a group — that part still needs either `setup-env` or:
>
> ```bash
> sudo usermod -aG dialout,plugdev $USER
> ```

2.Log out and log back in for group changes to take effect. Group membership
is applied at login, so this is not optional (`newgrp dialout` works for a
single shell as a one-off).

3.Verify everything is in place:

```bash
bombercat device list     # ✓ next to a board means it answered the handshake
bombercat status          # which firmware is flashed and what it can do
```

If the port is listed but access is denied, see
[Troubleshooting → Serial permission denied](troubleshooting.md#serial-permission-denied).

## Extra functions

### Shell completion (macOS and Linux systems)

Install tab completion for your shell.

1.Run the following command.

```bash
bombercat completion install
```

> [!Note]
> If you are using a specific shell, you can specify it explicitly, e.g.:
>
> ```bash
> bombercat completion install --shell bash
> bombercat completion install --shell zsh
> bombercat completion install --shell fish
> ```

2.Restart your shell.

Besides commands and options, this completes firmware names for
[`flash`](commands/flash.md#tab-completion). See
[`completion`](commands/completion.md) for the exact files it writes.

---

### First flash

`bombercat flash` downloads the UF2 image from the firmware repository's
releases, so it needs **network access** the first time; images are cached
under `~/.bombercat/firmware/`. It also needs the board mounted in BOOTSEL
mode as an `RPI-RP2` volume — that is what `udisks2` and the udev rules above
are for.

```bash
bombercat flash --list        # the published images, with descriptions
bombercat flash NFCGate       # download and write it over UF2
```

---

# Uninstallation instructions

## Windows

1.Go to Settings → Apps → Installed Apps → BomberCat.

2.Click on the 3-dot button on the right side and click on Uninstall.

Or run `unins000.exe` from `C:\Program Files\BomberCat\`.

The uninstaller removes the `PATH` entry and the shortcuts it created.

---

## macOS

1.Remove the package receipt.

```bash
sudo pkgutil --forget com.electroniccats.bombercat
```

2.Delete the installed files.

```bash
sudo rm -rf /usr/local/opt/bombercat
sudo rm -f /usr/local/bin/bombercat
```

---

## Linux

1.Run the command in the Terminal:

- Debian/Ubuntu:

  ```bash
  sudo apt remove bombercat
  ```

- Arch Linux:

  ```bash
  sudo pacman -R bombercat
  ```

2.Both remove the packaged udev rules in `/lib/udev/rules.d/`. The copy
`setup-env` writes lives elsewhere and is left behind on purpose — delete it
if you are done with the board:

```bash
sudo rm -f /etc/udev/rules.d/99-bombercat.rules
sudo udevadm control --reload-rules
```

---

## From source / `pip`

```bash
python3 -m pip uninstall bombercat     # or: make uninstall
```

## Leftover user files (every platform)

Uninstalling never touches your own files. Remove them by hand if you want a
clean slate:

- `~/.bombercat/firmware/` — cached UF2 images downloaded by `flash`
- Completion scripts written by `completion install`:
  `~/.local/share/bash-completion/completions/bombercat`, `~/.zfunc/_bombercat`
  (plus its `fpath` line in `~/.zshrc`), or
  `~/.config/fish/completions/bombercat.fish`
