"""``bombercat setup-env`` — udev rules and group membership on Linux.

What the `.deb`/`.pkg.tar.zst` do at install time (drop
``99-bombercat.rules``, create the groups, reload udev), done on demand for
the installs that have no package manager behind them: a source checkout, a
`pip install`, a distro whose package we do not ship. Registered on the root
group only on Linux — udev and `usermod` exist nowhere else.

The rules text below is the same file the packages install; ``tests/
test_cli_setup_env.py`` compares the two so they cannot drift apart.
"""

import os
import platform
import subprocess
import sys
from pathlib import Path

# POSIX-only, absent on Windows — setup_env() below bails out on
# platform.system() before ever touching grp, so this only needs to not
# crash the import.
try:
    import grp
except ImportError:
    grp = None

# External
import click

from .output import (
    print_success,
    print_warning,
    print_error,
    print_info,
    print_dim,
    print_example,
)

RULES_PATH = Path("/etc/udev/rules.d/99-bombercat.rules")

# Keep byte-for-byte in sync with
# packaging/debian/lib/udev/rules.d/99-bombercat.rules.
UDEV_RULES = """\
# BomberCat — access to the board without root.
#
# BomberCat CDC (pid.codes VID 1209) — every firmware personality
SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="005e", MODE="0660", GROUP="dialout", TAG+="uaccess"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="805e", MODE="0660", GROUP="dialout", TAG+="uaccess"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="015e", MODE="0660", GROUP="dialout", TAG+="uaccess"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="025e", MODE="0660", GROUP="dialout", TAG+="uaccess"
# Arduino identity (electroniccats:mbed_rp2040 core)
SUBSYSTEM=="tty", ATTRS{idVendor}=="2341", ATTRS{idProduct}=="005e", MODE="0660", GROUP="dialout", TAG+="uaccess"
# BOOTSEL (RP2040 mass-storage) — `bombercat flash` writes the .uf2 here
SUBSYSTEM=="block", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="0003", MODE="0660", GROUP="plugdev", TAG+="uaccess"
"""

# `dialout` carries the serial port (every command that talks to the board);
# `plugdev` the BOOTSEL mass-storage volume that `bombercat flash` writes to.
GROUPS = ("dialout", "plugdev")


def _target_user() -> str:
    """The human behind the `sudo`, not root."""
    user = os.environ.get("SUDO_USER")
    if not user:
        import getpass

        user = getpass.getuser()
    return user


@click.command("setup-env")
def setup_env():
    """Grant this machine access to a BomberCat: udev rules and groups.

    Installs the udev rules that make the serial port and the BOOTSEL volume
    reachable without root, then adds you to the `dialout` and `plugdev`
    groups. Needs root, and a log out / log back in afterwards.

    \b
        sudo bombercat setup-env

    The `.deb` and Arch packages already do this at install time; run it after
    a source checkout or a `pip install`.
    """
    if platform.system() != "Linux":
        print_error("setup-env is Linux-only (udev rules and usermod).")
        sys.exit(1)

    if os.geteuid() != 0:
        # From a checkout argv[0] is `bombercat.py`, which is not runnable on
        # its own — hand back the interpreter that is running us.
        invocation = sys.argv[0]
        if invocation.endswith(".py"):
            invocation = f"{sys.executable} {Path(invocation).resolve()}"
        print_error("Root privileges required. Please run with sudo:")
        print_dim(f"sudo {invocation} setup-env")
        sys.exit(1)

    failed = False

    # 1. Install the udev rules.
    try:
        RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
        RULES_PATH.write_text(UDEV_RULES, encoding="utf-8")
        print_success(f"Udev rules installed to {RULES_PATH}")
    except OSError as e:
        print_error(f"Failed to install udev rules: {e}")
        failed = True

    # 2. Add the user to the groups, creating any the distro does not ship
    #    (plugdev is absent on Arch and Fedora).
    user = _target_user()
    for group in GROUPS:
        try:
            grp.getgrnam(group)
        except KeyError:
            try:
                subprocess.run(["groupadd", group], check=True, capture_output=True)
                print_dim(f"created missing group '{group}'")
            except (subprocess.CalledProcessError, OSError) as e:
                print_warning(f"Could not create group '{group}': {e}")
                failed = True
                continue
        try:
            subprocess.run(
                ["usermod", "-aG", group, user], check=True, capture_output=True
            )
            print_success(f"User '{user}' added to group '{group}'")
        except (subprocess.CalledProcessError, OSError) as e:
            print_warning(f"Could not add user '{user}' to group '{group}': {e}")
            failed = True

    # 3. Reload udev so the rules apply to what is already plugged in.
    try:
        subprocess.run(["udevadm", "control", "--reload-rules"], check=True)
        subprocess.run(
            ["udevadm", "trigger", "--subsystem-match=tty", "--subsystem-match=block"],
            check=True,
        )
        print_success("Udev rules reloaded")
    except (subprocess.CalledProcessError, OSError) as e:
        print_warning(f"Could not reload udev rules automatically: {e}")
        print_dim("replug the board, or reboot, to pick them up")

    if failed:
        print_error("Environment setup finished with errors (see above).")
        sys.exit(1)

    print_success("Environment setup complete!")
    print_info("Log out and back in for the group changes to take effect, then:")
    print_example("bombercat device list")
