#!/usr/bin/env python3

# Electronic Cats
# test_tags_parser.py — TagParser (modules/tags/parser.py) turns DetectTags
# serial output into Tag objects via two backends: structured ":tag" events
# and the legacy displayCardInfo() prose. Fed with transcripts from
# docs/CLI_IMPROVEMENTS_DetectTags.md §1.3 — no hardware, no DeviceLink.

import pytest

from modules.tags.parser import Tag, TagParser, _hex_compact


# ── structured ":tag" events ─────────────────────────────────────────────────


def test_structured_event_is_parsed_immediately():
    parser = TagParser()

    tag = parser.feed(":tag 1234 NFC-A T2T 041A2B3C")

    assert tag == Tag(uid="041A2B3C", tech="NFC-A", protocol="T2T", ts_ms=1234)
    assert parser.structured is True


def test_structured_event_no_uid_uses_dash():
    parser = TagParser()

    tag = parser.feed(":tag 500 NFC-B ISODEP -")

    assert tag.uid is None


def test_structured_event_parses_trailing_key_value_extras():
    parser = TagParser()

    tag = parser.feed(":tag 10 NFC-A T2T 041A2B sens=4400 sel=00")

    assert tag.extra == {"sens": "4400", "sel": "00"}


def test_structured_event_skips_a_keyless_extra_token():
    """A malformed `=value` token (no key) must not become an empty-string
    column in extra - it would surface as an unnamed CSV/JSON column (L12)."""
    parser = TagParser()

    tag = parser.feed(":tag 10 NFC-A T2T 041A2B =oops sens=4400")

    assert tag.extra == {"sens": "4400"}


def test_structured_event_uid_is_uppercased():
    parser = TagParser()

    tag = parser.feed(":tag 10 NFC-A T2T 041a2b")

    assert tag.uid == "041A2B"


def test_structured_mode_is_sticky_and_ignores_later_text_lines():
    parser = TagParser()
    parser.feed(":tag 1234 NFC-A T2T 041A2B3C")

    assert parser.feed("\tTechnology: NFC-A") is None
    assert parser.feed("\tNFC ID = 0x04 0x1a") is None
    assert parser.structured is True


# ── legacy text mode: NFC-A with a full UID ──────────────────────────────────


def test_legacy_nfca_full_transcript_yields_tag_on_id_line():
    parser = TagParser()
    lines = [
        " - POLL MODE: Remote activated tag type: 2",
        "\tTechnology: NFC-A",
        "\tSENS RES = 0x44 0x00",
        "\tNFC ID = 0x04 0x1a 0x2b 0x3c 0x4d 0x5e 0x6f",
        "\tSEL RES = 0x00",
    ]

    results = [parser.feed(line) for line in lines]

    assert results[:3] == [None, None, None]
    assert results[3] == Tag(uid="041A2B3C4D5E6F", tech="NFC-A", protocol="T2T")
    assert results[4] is None  # SEL RES arrives after the tag already closed
    assert parser.structured is False


def test_legacy_named_protocol_mifare():
    parser = TagParser()
    parser.feed(" - POLL MODE: Remote MIFARE card activated")
    parser.feed("\tTechnology: NFC-A")

    tag = parser.feed("\tID = 0x04 0x1a 0x2b 0x3c")

    assert tag == Tag(uid="041A2B3C", tech="NFC-A", protocol="MIFARE")


def test_legacy_named_protocol_iso15693():
    parser = TagParser()
    parser.feed(" - POLL MODE: Remote ISO15693 card activated")
    parser.feed("\tTechnology: NFC-V")

    tag = parser.feed("\tID = 0xe0 0x04 0x01 0x50")

    assert tag == Tag(uid="E0040150", tech="NFC-V", protocol="ISO15693")


# ── legacy text mode: NFC-B / NFC-F ───────────────────────────────────────────


@pytest.mark.parametrize("tech", ["NFC-B", "NFC-F"])
def test_legacy_nfcb_nfcf_without_pupi_idm_line_closes_on_remove_the_card(tech):
    """Firmware predating PUPI/IDm extraction never prints an ID line for
    B/F - "Remove the Card" (always printed after the prose block) is the
    fallback close so the detection isn't lost."""
    parser = TagParser()
    parser.feed(" - POLL MODE: Remote activated tag type: 4")
    parser.feed(f"\tTechnology: {tech}")
    parser.feed("\tSENS RES = 0x50 0x00")

    assert parser.feed("Remove the Card") == Tag(uid=None, tech=tech, protocol="ISODEP")


def test_legacy_nfcb_pupi_line_yields_tag_with_uid():
    parser = TagParser()
    parser.feed(" - POLL MODE: Remote activated tag type: 4")
    parser.feed("\tTechnology: NFC-B")
    parser.feed("\tSENS RES = 0x50 0x11 0x22 0x33 0x44")

    tag = parser.feed("\tPUPI = 0x11 0x22 0x33 0x44")

    assert tag == Tag(uid="11223344", tech="NFC-B", protocol="ISODEP")


def test_legacy_nfcf_idm_line_yields_tag_with_uid():
    parser = TagParser()
    parser.feed(" - POLL MODE: Remote activated tag type: 3")
    parser.feed("\tTechnology: NFC-F")

    tag = parser.feed("\tIDm = 0x01 0x02 0x03 0x04 0x05 0x06 0x07 0x08")

    assert tag == Tag(uid="0102030405060708", tech="NFC-F", protocol="T3T")


# ── legacy text mode: reordered lines (L13) ──────────────────────────────────


def test_legacy_id_line_before_technology_is_buffered_not_lost():
    """Firmware/serial buffering can print the ID line before Technology.
    The detection must still complete instead of being silently dropped."""
    parser = TagParser()
    parser.feed(" - POLL MODE: Remote activated tag type: 2")

    assert parser.feed("\tNFC ID = 0x04 0x1a 0x2b 0x3c") is None

    tag = parser.feed("\tTechnology: NFC-A")

    assert tag == Tag(uid="041A2B3C", tech="NFC-A", protocol="T2T")


def test_legacy_id_line_before_technology_does_not_leak_into_next_detection():
    """A new detection's protocol line must reset the buffered-ID flag so an
    unfinished prior detection can't attach its UID to the next one."""
    parser = TagParser()
    parser.feed(" - POLL MODE: Remote activated tag type: 2")
    parser.feed("\tNFC ID = 0x04 0x1a 0x2b 0x3c")  # buffered, no Technology yet

    parser.feed(" - POLL MODE: Remote activated tag type: 3")  # next detection starts

    assert parser.feed("\tTechnology: NFC-F") is None  # not finalized by stale UID


# ── null / hex parsing ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0x04 0x1a 0x2b", "041A2B"),
        ("null", None),
        ("", None),
    ],
)
def test_hex_compact(text, expected):
    assert _hex_compact(text) == expected


def test_structured_event_rejects_a_non_hex_uid_and_keeps_it_as_raw():
    """Garbage bytes must not become a 'valid-looking' UID (M12)."""
    parser = TagParser()

    tag = parser.feed(":tag 10 NFC-A T2T zzqq")

    assert tag.uid is None
    assert tag.extra["raw_uid"] == "ZZQQ"


def test_legacy_id_line_rejects_garbage_hex_and_keeps_it_as_raw():
    parser = TagParser()
    parser.feed("\tTechnology: NFC-A")

    tag = parser.feed("\tNFC ID = zz qq")

    assert tag.uid is None
    assert tag.extra["raw_uid"] == "ZZQQ"


def test_legacy_id_line_of_null_yields_tag_with_no_uid():
    parser = TagParser()
    parser.feed("\tTechnology: NFC-A")

    tag = parser.feed("\tNFC ID = null")

    assert tag.uid is None


# ── Tag.pretty_uid ────────────────────────────────────────────────────────────


def test_pretty_uid_formats_bytes_with_colons():
    assert Tag(uid="041A2B3C").pretty_uid == "04:1A:2B:3C"


def test_pretty_uid_unavailable_without_tech():
    assert "unknown tech" in Tag(uid=None).pretty_uid
