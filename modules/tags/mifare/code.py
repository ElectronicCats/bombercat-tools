#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

# `mifare code` — offline text → block hex. Turns a plain string into the
# 32-hex-char blocks `mifare write` expects, laid out over the data blocks of
# the sector whose keys you hold. Touches no device: it only formats, the
# writing is still `mifare write` (or `mifare restore` for a whole dump).

import json
from typing import Dict, List, Optional

import click

from ...utils.detection_cli import print_field as _print_field
from ...utils.output import (
    console,
    print_dim,
    print_error,
    print_subtitle,
)
from .common import (
    _MIFARE_BLOCK_HEX_LEN,
    _MIFARE_CHECK_MAX_SECTORS,
    _MIFARE_DATA_BLOCK_INDICES,
    _load_sector_key_pair,
    _mifare_validate_hex,
    _sector_first_block,
)


_MIFARE_BLOCK_BYTES = _MIFARE_BLOCK_HEX_LEN // 2  # 16 bytes per block
_MIFARE_PAD_HEX_LEN = 2  # filler byte, hex-encoded


def _sector_data_blocks(sector: int) -> List[int]:
    """Block numbers of SECTOR that can hold user data: the three blocks
    before the trailer, minus block 0 of sector 0 (the factory
    UID/manufacturer block — `mifare restore --write-block0` territory, not
    somewhere to park a string)."""
    base = _sector_first_block(sector)
    return [
        base + i for i in _MIFARE_DATA_BLOCK_INDICES if not (sector == 0 and i == 0)
    ]


def _encode_block_hex(payload: bytes, pad: int) -> List[str]:
    """Split `payload` into 16-byte blocks, padding the last one with `pad` up
    to the block boundary — a card is only ever written a whole block at a
    time, so a partial tail isn't a thing."""
    padded = payload + bytes([pad]) * (-len(payload) % _MIFARE_BLOCK_BYTES)
    return [
        padded[i : i + _MIFARE_BLOCK_BYTES].hex().upper()
        for i in range(0, len(padded), _MIFARE_BLOCK_BYTES)
    ]


@click.command("code", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("text")
@click.option(
    "--sector",
    type=click.IntRange(0, _MIFARE_CHECK_MAX_SECTORS - 1),
    default=None,
    help="Lay the blocks out over this sector's data blocks and label them "
    "with their real block numbers. Without it, blocks are listed unplaced.",
)
@click.option(
    "-k",
    "--keys-file",
    "keys_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    metavar="FILE",
    help="A `sector:keyA:keyB` file (see `mifare check --output-keys`). Needs "
    "--sector; prints the ready-to-run `auth`/`write` lines for that sector.",
)
@click.option(
    "--pad",
    default="00",
    show_default=True,
    metavar="HEX2",
    help="Filler byte for the tail of the last block, 2 hex chars.",
)
@click.option(
    "--json", "as_json", is_flag=True, help="Emit the encoding as one JSON object."
)
def mifare_code_cmd(text, sector, keys_file, pad, as_json):
    """Encode TEXT as the block hex a Mifare Classic sector stores.

    Offline — no card, no device. TEXT is UTF-8 encoded and padded out to
    whole 16-byte blocks (32 hex chars each), the shape `mifare write --data`
    takes. With --sector the blocks are placed on that sector's data blocks
    (its trailer is never touched, nor block 0 of sector 0), and with
    --keys-file the exact `auth`/`write` command lines are printed too.
    """
    err = _mifare_validate_hex(pad, _MIFARE_PAD_HEX_LEN, "--pad")
    if err:
        print_error(err)
        raise SystemExit(1)
    if keys_file and sector is None:
        print_error("--keys-file needs --sector")
        raise SystemExit(1)

    payload = text.encode("utf-8")
    if not payload:
        print_error("TEXT is empty — nothing to encode")
        raise SystemExit(1)

    blocks = _encode_block_hex(payload, int(pad, 16))

    targets: List[Optional[int]] = [None] * len(blocks)
    if sector is not None:
        available = _sector_data_blocks(sector)
        if len(blocks) > len(available):
            capacity = len(available) * _MIFARE_BLOCK_BYTES
            print_error(
                f"text is {len(payload)} byte(s) ({len(blocks)} block(s)) but "
                f"sector {sector} holds {capacity} byte(s) in "
                f"{len(available)} data block(s)"
            )
            raise SystemExit(1)
        targets = list(available[: len(blocks)])

    key_type: Optional[str] = None
    key: Optional[str] = None
    if keys_file:
        key_a, key_b = _load_sector_key_pair(keys_file, sector)
        if key_a:
            key_type, key = "A", key_a
        elif key_b:
            key_type, key = "B", key_b
        else:
            print_error(f"sector {sector} has no key A or key B in {keys_file}")
            raise SystemExit(1)

    commands: List[str] = []
    if targets[0] is not None:
        if key:
            commands.append(
                f"bombercat tags mifare auth --block {targets[0]} "
                f"--key-type {key_type} --key {key}"
            )
        for block, data in zip(targets, blocks):
            commands.append(
                f"bombercat tags mifare write --block {block} --data {data}"
            )

    if as_json:
        out: Dict[str, object] = {
            "text": text,
            "encoding": "utf-8",
            "bytes": len(payload),
            "padded_bytes": len(blocks) * _MIFARE_BLOCK_BYTES,
            "pad": pad.upper(),
            "sector": sector,
            "blocks": [
                {"index": i, "block": block, "data": data}
                for i, (block, data) in enumerate(zip(targets, blocks))
            ],
        }
        if commands:
            out["commands"] = commands
        print(json.dumps(out))
        return

    console.print("")
    _print_field("text", text)
    _print_field(
        "bytes",
        f"{len(payload)} → {len(blocks) * _MIFARE_BLOCK_BYTES} padded with "
        f"0x{pad.upper()} ({len(blocks)} block(s))",
    )
    if sector is not None:
        _print_field("sector", str(sector))
    if key:
        _print_field("key", f"{key} (key {key_type}, from {keys_file})")
    print_subtitle("Blocks")
    for i, (block, data) in enumerate(zip(targets, blocks)):
        _print_field(f"block {block}" if block is not None else f"[{i}]", data)
    if commands:
        print_subtitle("Write it")
        for line in commands:
            # soft_wrap: a wrapped command line can't be copy-pasted.
            console.print(f"  {line}", style="dim", soft_wrap=True)
    elif sector is None:
        print_dim("pass --sector to place these on a sector's data blocks")
