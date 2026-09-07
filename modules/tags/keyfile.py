"""Loader for MIFARE Classic key dictionaries (.keys/.dic/.md).

Used by `bombercat tags mifare check` (see docs/CLI_IMPROVEMENTS_MifareCheck.md).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List

_KEY_LINE_RE = re.compile(r"^[0-9A-Fa-f]{12}$")

_DEFAULT_KEYFILE = Path(__file__).parent / "data" / "mifare_default_keys.keys"


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
