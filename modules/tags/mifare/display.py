#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

# Rendering helpers shared by the `tags mifare` commands: sector dumps with
# their key/access-bit coloring, decoded access conditions and block 0.

from typing import Dict, List, Optional, Tuple

from ...utils.detection_cli import print_field as _print_field
from ...utils.output import (
    print_subtitle,
    print_warning,
)
from .access_bits import SectorAccessBits, parse_access_bits
from .block0 import Block0, parse_block0
from .common import (
    _MIFARE_BLOCK_HEX_LEN,
    _MIFARE_DATA_BLOCK_INDICES,
    _MIFARE_KEY_HEX_LEN,
    _MIFARE_TRAILER_AC_LEN,
    _sector_first_block,
)


def _substitute_trailer_keys(
    trailer: str, key_a: Optional[str], key_b: Optional[str]
) -> Tuple[str, str, str]:
    """Split a trailer block into `(key_a, ac, key_b)`, substituting the real
    keys from a keyfile in place of the zeros a card reads back for them. A
    card never reads Key A back (and often not Key B either); the
    access-conditions bytes in the middle always stay exactly as read."""
    shown_a = key_a or trailer[:_MIFARE_KEY_HEX_LEN]
    ac = trailer[_MIFARE_KEY_HEX_LEN : _MIFARE_KEY_HEX_LEN + _MIFARE_TRAILER_AC_LEN]
    shown_b = key_b or trailer[_MIFARE_KEY_HEX_LEN + _MIFARE_TRAILER_AC_LEN :]
    return shown_a, ac, shown_b


def _sector_display_lines(
    sector: int,
    data_hex: str,
    key_a: Optional[str] = None,
    key_b: Optional[str] = None,
) -> Tuple[List[Tuple[str, str]], Optional[SectorAccessBits]]:
    """Split a sector's dump (4 blocks x 32 hex chars) into the `(label,
    line)` rows `mifare sector` prints — labeled with the sector's real block
    numbers (the same `sector * 4` mapping `_sector_first_block` uses; 1K/2K
    sectors 0-31 only, 4K's 16-block sectors 32-39 aren't supported yet) —
    plus the decoded access bits for those blocks (None if the trailer wasn't
    a full block).

    Block 0 is colored magenta only for sector 0 (the only sector where it's
    factory UID data, not user data). A card never reads its Key A back (and
    often not Key B either) — the trailer's key bytes come back as zeros.
    `key_a`/`key_b`, when given (from `--keys-file`), are substituted into the
    display so the real keys show instead of those zeros; the
    access-conditions bytes always stay as read."""
    blocks = [
        data_hex[i : i + _MIFARE_BLOCK_HEX_LEN]
        for i in range(0, len(data_hex), _MIFARE_BLOCK_HEX_LEN)
    ]
    blocks += [""] * (4 - len(blocks))
    block0, block1, block2, trailer = blocks[:4]
    base = _sector_first_block(sector)

    line0 = f"[magenta]{block0}[/magenta]" if sector == 0 and block0 else block0

    shown_a, ac, shown_b = _substitute_trailer_keys(trailer, key_a, key_b)
    line3 = (
        f"[green]{shown_a}[/green][dark_orange3]{ac}[/dark_orange3]"
        f"[green]{shown_b}[/green]"
    )

    lines = [
        (f"block {base}", line0),
        (f"block {base + 1}", block1),
        (f"block {base + 2}", block2),
        (f"block {base + 3} (trailer)", line3),
    ]
    access = parse_access_bits(ac[:6]) if len(ac) == _MIFARE_TRAILER_AC_LEN else None
    return lines, access


def _print_access_bits(access: SectorAccessBits, base: int) -> None:
    """Render a sector's decoded access conditions under its block dump."""
    print_subtitle("Access conditions")
    for i, blk in enumerate(access.blocks):
        _print_field(
            f"block {base + i}",
            f"read {blk.read} · write {blk.write} · increment {blk.increment} · "
            f"decr/transfer/restore {blk.decrement}",
        )
    t = access.trailer
    _print_field(
        f"block {base + 3} (trailer)",
        f"key A: write {t.key_a_write} · AC: read {t.ac_read}, write {t.ac_write} "
        f"· key B: read {t.key_b_read}, write {t.key_b_write}",
    )
    if not access.valid:
        print_warning(
            "access bits fail their own inverse check — bytes may be corrupt "
            "or this trailer wasn't actually read"
        )


def _print_block0(b0: Block0) -> None:
    """Render a dissected block 0 under the raw hex the caller already
    printed - `mifare read --block 0` and `mifare sector --sector 0` both
    have it, one straight off the card, the other as the first 32 hex chars
    of the sector dump."""
    print_subtitle("Block 0 (UID sector)")
    _print_field("uid", b0.uid)
    _print_field("bcc", f"{b0.bcc}  ({'OK' if b0.bcc_valid else 'MISMATCH ⚠'})")
    _print_field("sak", f"{b0.sak}  -> {b0.sak_name}")
    _print_field("atqa", f"{b0.atqa}  -> {b0.atqa_name}")
    _print_field("mfg data", b0.manufacturer_data)
    if not b0.bcc_valid:
        print_warning(
            "BCC does not match the UID — possibly a magic/clone card with a "
            "hand-written block 0"
        )


def _ascii_render(block_hex: str) -> str:
    """Render a 32-hex-char block (16 bytes) as printable ASCII, non-printable
    bytes shown as '.' — the classic hex-dump right column. Returns "" for a
    block that isn't full/valid hex (a gap), so the caller can skip it."""
    try:
        raw = bytes.fromhex(block_hex)
    except ValueError:
        return ""
    return "".join(chr(b) if 32 <= b < 127 else "." for b in raw)


def _print_decoded_sector(entry: Dict[str, object]) -> None:
    """Print one read sector for `mifare dump --decode`: the raw block hex
    (same coloring as `mifare sector`) with an ASCII column beside each data
    block, plus the dissected block 0 for sector 0. The trailer keeps its
    key/access-bit coloring but gets no ASCII column (its bytes are keys, not
    text). `entry` is a `sector_results` item — its trailer already has the
    real keys substituted in, so no keyfile is needed here."""
    sector = entry["sector"]
    blocks = entry["blocks"]
    data_hex = "".join(blocks)
    print_subtitle(f"Sector {sector}")
    lines, _access = _sector_display_lines(sector, data_hex)
    for i, (label, line) in enumerate(lines):
        if i in _MIFARE_DATA_BLOCK_INDICES:
            _print_field(label, f"{line}  [dim]|{_ascii_render(blocks[i])}|[/dim]")
        else:
            _print_field(label, line)
    if sector == 0:
        b0 = parse_block0(blocks[0])
        if b0 is not None:
            _print_block0(b0)
