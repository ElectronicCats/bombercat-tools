#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

# Sizes, sector geometry and key-file lookups shared by every `tags mifare`
# command. Pure helpers: nothing here talks to the device.

import re
from typing import Optional, Tuple

from ...utils.output import print_error
from .keyfile import SectorKeyfileError, load_sector_keys


_MIFARE_KEY_HEX_LEN = 12  # 6-byte key, hex-encoded
_MIFARE_BLOCK_HEX_LEN = 32  # 16-byte block, hex-encoded
_MIFARE_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")


_MIFARE_TRAILER_AC_LEN = 8  # 4-byte access conditions, hex-encoded


def _sector_first_block(sector: int) -> int:
    """Block that opens SECTOR's keys — 1K/2K (4-block sectors) mapping only.
    4K's sectors 32-39 (16 blocks each) aren't supported yet; isolating the
    mapping here means extending it later won't touch the sweep loop."""
    return sector * 4


# Limit of the "block = 4 * sector" mapping above (1K = 16, 2K = 32 sectors).
_MIFARE_CHECK_MAX_SECTORS = 32


def _mifare_validate_hex(value: str, expected_len: int, label: str) -> Optional[str]:
    if len(value) != expected_len or not _MIFARE_HEX_RE.match(value):
        return f"{label} must be exactly {expected_len} hex characters"
    return None


def _load_sector_key_pair(
    keys_file: str, sector: int
) -> Tuple[Optional[str], Optional[str]]:
    """Read `keys_file` and return ``(keyA, keyB)`` for `sector`, exiting with
    a clear error if the file is malformed or doesn't list that sector."""
    try:
        table = load_sector_keys(keys_file)
    except OSError as e:
        print_error(f"could not read {keys_file}: {e}")
        raise SystemExit(1)
    except SectorKeyfileError as e:
        print_error(str(e))
        raise SystemExit(1)
    if sector not in table:
        print_error(f"sector {sector} not found in {keys_file}")
        raise SystemExit(1)
    return table[sector]


# In a 4-block sector, index 3 is the trailer (keys + access bits); indices
# 0-2 are data blocks. Block 0 of sector 0 is the manufacturer/UID block, gated
# behind --write-block0.
_MIFARE_TRAILER_BLOCK_INDEX = 3
_MIFARE_DATA_BLOCK_INDICES = (0, 1, 2)
