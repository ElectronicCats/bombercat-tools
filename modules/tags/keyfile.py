"""Loader for MIFARE Classic key dictionaries (.keys/.dic/.md).

Used by `bombercat tags mifare check` (see docs/CLI_IMPROVEMENTS_MifareCheck.md).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

_KEY_LINE_RE = re.compile(r"^[0-9A-Fa-f]{12}$")

_DEFAULT_KEYFILE = Path(__file__).parent / "data" / "mifare_default_keys.keys"

# `mifare check --output-keys` / `mifare sector --keys-file` line shape:
# "<sector>:<keyA or blank>:<keyB or blank>". Either key may be blank (that
# type wasn't recovered by `check`), but the sector and both separators are
# always present.
_SECTOR_KEYLINE_RE = re.compile(r"^(\d+):([0-9A-Fa-f]{12})?:([0-9A-Fa-f]{12})?$")


def load_keys(paths: Iterable[str]) -> List[str]:
    """Union of one or more .keys/.dic/.md files.

    Keeps only lines that are exactly 12 hex characters (after stripping),
    uppercases them, and deduplicates while preserving first-seen order.
    Everything else (frontmatter, prose, comments, wikilinks, blank lines)
    is silently ignored, so mfoc/proxmark keyfiles and the raw StandardKeys.md
    vault note both work unmodified.
    """
    seen: dict[str, None] = {}
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if _KEY_LINE_RE.match(line):
                    seen.setdefault(line.upper(), None)
    return list(seen)


def default_keyfile() -> Path:
    return _DEFAULT_KEYFILE


class SectorKeyfileError(ValueError):
    """A `sector:keyA:keyB` file was malformed. Carries a `path:line:reason`
    message the CLI turns into a `print_error` + exit rather than a
    traceback."""


def load_sector_keys(path: str) -> Dict[int, Tuple[Optional[str], Optional[str]]]:
    """Load a `mifare check --output-keys` file into ``{sector: (keyA, keyB)}``.

    Each non-blank, non-``#`` line must be ``sector:keyA:keyB`` with each key
    either 12 hex chars or blank (blank = that type wasn't recovered, stored
    as ``None``). Unlike `load_keys`, which silently skips junk in a key
    dictionary, a line that doesn't fit this shape raises `SectorKeyfileError`
    — a hand-edited or wrong-format keys file should fail loudly, not quietly
    drop the sector the user asked for.
    """
    keys: Dict[int, Tuple[Optional[str], Optional[str]]] = {}
    with open(path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = _SECTOR_KEYLINE_RE.match(line)
            if not m:
                raise SectorKeyfileError(
                    f"{path}:{lineno}: expected 'sector:keyA:keyB' (each key 12 "
                    f"hex chars or blank), got {line!r}"
                )
            sector = int(m.group(1))
            key_a = m.group(2).upper() if m.group(2) else None
            key_b = m.group(3).upper() if m.group(3) else None
            keys[sector] = (key_a, key_b)
    return keys
