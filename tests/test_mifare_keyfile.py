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
