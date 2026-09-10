#!/usr/bin/env python3

# Electronic Cats
# Distributed as-is; no warranty is given.

# `mifare write-text` — `code`'s encoder wired to a real write pass: the same
# UTF-8 → block hex layout, then an auth with the keys you hold (--keys-file
# or --key) and one `mifare write` per block. What you'd get by copy-pasting
# `mifare code --keys-file`'s auth/write lines, in a single command.

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import click

from ...utils.cli_options import device_options
from ...utils.detection_cli import (
    print_field as _print_field,
    verbosity as _verbosity,
)
from ...utils.output import (
    console,
    make_tracer,
    print_error,
    print_info,
    print_subtitle,
    print_success,
)
from .common import (
    _MIFARE_CHECK_MAX_SECTORS,
    _MIFARE_KEY_HEX_LEN,
    _load_sector_key_pair,
    _mifare_validate_hex,
)
from .session import (
    _MIFARE_KEY_TYPE_OPTION,
    _MIFARE_TIMEOUT_OPTION,
    _auth_sector,
    _mifare_session,
)
from .code import (
    _MIFARE_BLOCK_BYTES,
    _MIFARE_PAD_HEX_LEN,
    _encode_block_hex,
    _sector_data_blocks,
)


def _resolve_text_target(
    sector: Optional[int], block: Optional[int]
) -> Tuple[int, List[int]]:
    """Turn --sector/--block into ``(sector, blocks)``: the sector to
    authenticate and the data blocks usable from the starting point on.

    With --block the sector is derived from it (4 blocks per sector) and the
    run starts at that block; with --sector it starts at the sector's first
    data block. A block that can't hold user data — a trailer, or sector 0's
    manufacturer block — is refused here rather than by the firmware."""
    if block is not None:
        sector = block // 4
        available = _sector_data_blocks(sector)
        if block not in available:
            what = (
                "sector 0's manufacturer block (UID/BCC/SAK/ATQA)"
                if block == 0
                else f"sector {sector}'s trailer (keys + access bits)"
            )
            print_error(
                f"block {block} can't hold text: it is {what} — data blocks of "
                f"sector {sector} are {', '.join(str(b) for b in available)}"
            )
            raise SystemExit(1)
        return sector, available[available.index(block) :]
    assert sector is not None
    return sector, _sector_data_blocks(sector)


def _resolve_write_keys(
    keys_file: Optional[str], key: Optional[str], key_type: str, sector: int
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the sector's write keys from --keys-file or --key, the same way
    `mifare sector` resolves its read keys. Returns ``(key_a, key_b)`` with
    the slot(s) available to try."""
    if keys_file:
        key_a, key_b = _load_sector_key_pair(keys_file, sector)
        if not key_a and not key_b:
            print_error(f"sector {sector} has no key A or key B in {keys_file}")
            raise SystemExit(1)
        return key_a, key_b
    err = _mifare_validate_hex(key, _MIFARE_KEY_HEX_LEN, "--key")
    if err:
        print_error(err)
        raise SystemExit(1)
    if key_type.upper() == "A":
        return key.upper(), None
    return None, key.upper()


@click.command("write-text")
@click.argument("text")
@click.option(
    "--sector",
    type=click.IntRange(0, _MIFARE_CHECK_MAX_SECTORS - 1),
    default=None,
    help="Target sector — the text starts at its first data block.",
)
@click.option(
    "--block",
    type=click.IntRange(0, _MIFARE_CHECK_MAX_SECTORS * 4 - 1),
    default=None,
    help="Start at this absolute block instead of the sector's first data "
    "block; its sector is derived (4 blocks per sector).",
)
@_MIFARE_KEY_TYPE_OPTION
@click.option("--key", metavar="HEX12", help="6-byte key, 12 hex chars.")
@click.option(
    "-k",
    "--keys-file",
    "keys_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    metavar="FILE",
    help="A `sector:keyA:keyB` file (see `mifare check --output-keys`). Uses "
    "the target sector's key A, falling back to key B, instead of --key.",
)
@click.option(
    "--pad",
    default="00",
    show_default=True,
    metavar="HEX2",
    help="Filler byte for the tail of the last block, 2 hex chars.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the write result as JSON.")
@_MIFARE_TIMEOUT_OPTION
@device_options
@click.pass_context
def mifare_write_text_cmd(
    ctx,
    text,
    sector,
    block,
    key_type,
    key,
    keys_file,
    pad,
    as_json,
    timeout,
    verbose,
    port,
    device_id,
):
    """Encode TEXT and write it to a sector's data blocks in one pass.

    \b
    ⚠ AUTHORIZED USE ONLY. Write only to cards you own or have explicit
    permission to write.

    `mifare code` + `mifare write`: TEXT is UTF-8 encoded and padded out to
    whole 16-byte blocks, the sector is authenticated with the key you pass
    (--key) or the one listed for it in --keys-file, and each block is written
    in order. Pick the destination with --sector (starts at its first data
    block) or --block (starts there, its sector derived). Trailers and sector
    0's block 0 are never written — use `mifare restore` for those. A write
    that's denied stops the run and reports the blocks written so far.
    """
    if (sector is None) == (block is None):
        print_error("pass exactly one of --sector or --block")
        raise SystemExit(1)
    if keys_file and key:
        print_error("pass either --key or --keys-file, not both")
        raise SystemExit(1)
    if not keys_file and not key:
        print_error("one of --key or --keys-file is required")
        raise SystemExit(1)
    err = _mifare_validate_hex(pad, _MIFARE_PAD_HEX_LEN, "--pad")
    if err:
        print_error(err)
        raise SystemExit(1)

    payload = text.encode("utf-8")
    if not payload:
        print_error("TEXT is empty — nothing to write")
        raise SystemExit(1)

    blocks = _encode_block_hex(payload, int(pad, 16))
    sector, available = _resolve_text_target(sector, block)
    if len(blocks) > len(available):
        capacity = len(available) * _MIFARE_BLOCK_BYTES
        print_error(
            f"text is {len(payload)} byte(s) ({len(blocks)} block(s)) but "
            f"only {len(available)} data block(s) of sector {sector} are "
            f"available from block {available[0]} on ({capacity} byte(s))"
        )
        raise SystemExit(1)
    targets = available[: len(blocks)]
    key_a, key_b = _resolve_write_keys(keys_file, key, key_type, sector)

    level = _verbosity(ctx, verbose)
    written: List[Dict[str, object]] = []
    with _mifare_session(port, device_id, trace=make_tracer(level)) as (target, link):
        if not as_json:
            print_info(
                f"Writing {len(blocks)} block(s) to sector {sector} "
                f"(blocks {targets[0]}-{targets[-1]}) on {target}"
            )
        used_kt, reason = _auth_sector(link, sector, key_a, key_b, timeout, first=True)
        if used_kt is not None:
            for target_block, data in zip(targets, blocks):
                w = link.command(f"mifare write {target_block} {data}")
                if not w.ok:
                    reason = f"block {target_block} write failed: {w.message}"
                    break
                written.append({"block": target_block, "data": data})

    complete = reason == "ok"
    result: Dict[str, object] = {
        "text": text,
        "encoding": "utf-8",
        "bytes": len(payload),
        "padded_bytes": len(blocks) * _MIFARE_BLOCK_BYTES,
        "pad": pad.upper(),
        "sector": sector,
        "key_type": used_kt,
        "blocks_total": len(blocks),
        "blocks_written": len(written),
        "blocks": written,
        "reason": reason,
        "written_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    if as_json:
        print(json.dumps(result))
        raise SystemExit(0 if complete else 1)

    console.print("")
    _print_field("text", text)
    _print_field(
        "bytes",
        f"{len(payload)} → {len(blocks) * _MIFARE_BLOCK_BYTES} padded with "
        f"0x{pad.upper()} ({len(blocks)} block(s))",
    )
    _print_field("sector", str(sector))
    if used_kt:
        _print_field(
            "key",
            f"key {used_kt}" + (f", from {keys_file}" if keys_file else ""),
        )
    if written:
        print_subtitle("Blocks written")
        for entry in written:
            _print_field(f"block {entry['block']}", str(entry["data"]))
    if not complete:
        print_error(f"sector {sector}: {reason}")
        raise SystemExit(1)
    print_success(f"wrote {len(written)} block(s) to sector {sector}")
