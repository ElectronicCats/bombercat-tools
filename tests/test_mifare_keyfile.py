#!/usr/bin/env python3

# Electronic Cats
# test_mifare_keyfile.py — modules/tags/keyfile.py loads MIFARE Classic key
# dictionaries (.keys/.dic/.md), keeping only 12-hex-char lines. See
# docs/CLI_IMPROVEMENTS_MifareCheck.md §5/§6.1/§8.

from modules.tags.keyfile import default_keyfile, load_keys


def test_bundled_default_keyfile_has_2477_unique_keys():
    keys = load_keys([str(default_keyfile())])
    assert len(keys) == 2477
    assert len(set(keys)) == len(keys)
    assert keys[0] == "FFFFFFFFFFFF"


def test_loader_ignores_frontmatter_prose_and_comments(tmp_path):
    md = tmp_path / "keys.md"
    md.write_text(
        "---\n"
        "tags:\n"
        "  - referencia\n"
        "---\n"
        "\n"
        "Diccionario de claves conocidas. Ver [[HotelKeys]].\n"
        "\n"
        "# Standard keys\n"
        "FFFFFFFFFFFF\n"
        "A0A1A2A3A4A5\n"
        "\n"
        "# Keys from:\n"
        "# http://pastebin.com/wcTHXLZZ\n"
        "not-a-key\n"
        "A64598A77478\n",
        encoding="utf-8",
    )
    assert load_keys([str(md)]) == ["FFFFFFFFFFFF", "A0A1A2A3A4A5", "A64598A77478"]


def test_loader_uppercases_and_dedups_preserving_order(tmp_path):
    f = tmp_path / "keys.keys"
    f.write_text("ffffffffffff\nA0A1A2A3A4A5\nFFFFFFFFFFFF\n", encoding="utf-8")
    assert load_keys([str(f)]) == ["FFFFFFFFFFFF", "A0A1A2A3A4A5"]


def test_loader_unions_multiple_files(tmp_path):
    a = tmp_path / "a.keys"
    b = tmp_path / "b.keys"
    a.write_text("FFFFFFFFFFFF\nA0A1A2A3A4A5\n", encoding="utf-8")
    b.write_text("A0A1A2A3A4A5\nD3F7D3F7D3F7\n", encoding="utf-8")
    assert load_keys([str(a), str(b)]) == [
        "FFFFFFFFFFFF",
        "A0A1A2A3A4A5",
        "D3F7D3F7D3F7",
    ]


# ── load_sector_keys: `sector:keyA:keyB` files (mifare sector --keys-file) ────
# docs/CLI_IMPROVEMENTS_MifareCheck.md — the per-sector keys file written by
# `check --output-keys` and consumed by `sector --keys-file`.

import pytest

from modules.tags.keyfile import SectorKeyfileError, load_sector_keys


def test_load_sector_keys_round_trips_both_keys(tmp_path):
    f = tmp_path / "claves.txt"
    f.write_text(
        "0:A0A1A2A3A4A5:787788C10203\n1:D3F7D3F7D3F7:FFFFFFFFFFFF\n",
        encoding="utf-8",
    )
    assert load_sector_keys(str(f)) == {
        0: ("A0A1A2A3A4A5", "787788C10203"),
        1: ("D3F7D3F7D3F7", "FFFFFFFFFFFF"),
    }


def test_load_sector_keys_treats_a_blank_key_as_none(tmp_path):
    # `check` recovered only key A for sector 1, only key B for sector 2.
    f = tmp_path / "claves.txt"
    f.write_text("1:D3F7D3F7D3F7:\n2::FFFFFFFFFFFF\n3::\n", encoding="utf-8")
    assert load_sector_keys(str(f)) == {
        1: ("D3F7D3F7D3F7", None),
        2: (None, "FFFFFFFFFFFF"),
        3: (None, None),
    }


def test_load_sector_keys_uppercases_and_skips_blanks_and_comments(tmp_path):
    f = tmp_path / "claves.txt"
    f.write_text(
        "# hand-edited keys\n\n0:a0a1a2a3a4a5:ffffffffffff\n",
        encoding="utf-8",
    )
    assert load_sector_keys(str(f)) == {0: ("A0A1A2A3A4A5", "FFFFFFFFFFFF")}


def test_load_sector_keys_raises_on_a_malformed_line(tmp_path):
    # An 11-hex key (short) must fail loudly rather than silently drop the
    # sector the user asked for.
    f = tmp_path / "claves.txt"
    f.write_text("0:FFFFFFFFFFFF:FFFFFFFFFFFF\n1:FFFFFFFFFFF:FFFFFFFFFFF\n")
    with pytest.raises(SectorKeyfileError) as excinfo:
        load_sector_keys(str(f))
    # message carries path:line:reason so the CLI can show it verbatim
    assert f"{f}:2:" in str(excinfo.value)
