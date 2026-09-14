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


def test_legacy_nfca_full_transcript_yields_tag_on_sel_res_line():
    """NFC-A is held back one line past its UID: SEL RES carries the SAK, and
    closing on the ID line would emit the tag before its own SAK exists."""
    parser = TagParser()
    lines = [
        " - POLL MODE: Remote activated tag type: 2",
        "\tTechnology: NFC-A",
        "\tSENS RES = 0x44 0x00",
        "\tNFC ID = 0x04 0x1a 0x2b 0x3c 0x4d 0x5e 0x6f",
        "\tSEL RES = 0x00",
    ]

    results = [parser.feed(line) for line in lines]

    assert results[:4] == [None, None, None, None]
    assert results[4] == Tag(
        uid="041A2B3C4D5E6F",
        tech="NFC-A",
        protocol="T2T",
        extra={"atqa": "0044", "sak": "00", "model": "MIFARE Ultralight / NTAG"},
    )
    assert parser.structured is False


def test_legacy_named_protocol_mifare():
    parser = TagParser()
    parser.feed(" - POLL MODE: Remote MIFARE card activated")
    parser.feed("\tTechnology: NFC-A")
    parser.feed("\tSENS RES = 0x04 0x00")
    parser.feed("\tID = 0x04 0x1a 0x2b 0x3c")

    tag = parser.feed("\tSEL RES = 0x08")

    assert tag == Tag(
        uid="041A2B3C",
        tech="NFC-A",
        protocol="MIFARE",
        extra={"atqa": "0004", "sak": "08", "model": "MIFARE Classic 1K"},
    )


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
    assert parser.feed("\tTechnology: NFC-A") is None

    tag = parser.feed("\tSEL RES = 0x08")

    assert tag == Tag(uid="041A2B3C", tech="NFC-A", protocol="T2T", extra={"sak": "08"})


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
    parser.feed("\tNFC ID = zz qq")

    tag = parser.feed("\tSEL RES = 0x08")

    assert tag.uid is None
    assert tag.extra["raw_uid"] == "ZZQQ"


def test_legacy_id_line_of_null_yields_tag_with_no_uid():
    parser = TagParser()
    parser.feed("\tTechnology: NFC-A")
    parser.feed("\tNFC ID = null")

    tag = parser.feed("\tSEL RES = 0x08")

    assert tag.uid is None


# ── Tag.pretty_uid ────────────────────────────────────────────────────────────


def test_pretty_uid_formats_bytes_with_colons():
    assert Tag(uid="041A2B3C").pretty_uid == "04:1A:2B:3C"


def test_pretty_uid_unavailable_without_tech():
    assert "unknown tech" in Tag(uid=None).pretty_uid


# ── chip fingerprinting: SENS RES / SEL RES folded into a structured event ───


def test_structured_event_picks_up_atqa_sak_and_model_from_the_prose():
    """The sketch prints both values just before emitTagEvent(), so they are
    buffered and attached to the ':tag' that follows."""
    parser = TagParser()
    parser.feed("\tSENS RES = 0x04 0x00")
    parser.feed("\tSEL RES = 0x08")

    tag = parser.feed(":tag 1234 NFC-A MIFARE A3912200")

    assert tag.uid == "A3912200"
    assert tag.extra == {
        "atqa": "0004",
        "sak": "08",
        "model": "MIFARE Classic 1K",
    }


def test_fingerprint_works_on_the_very_first_detection():
    """The first detection's prose arrives while the parser is still in legacy
    mode — it must be buffered anyway."""
    parser = TagParser()
    assert parser.structured is False
    parser.feed("\tSENS RES = 0x44 0x00")
    parser.feed("\tSEL RES = 0x00")

    tag = parser.feed(":tag 10 NFC-A T2T 041A2B3C4D5E6F")

    assert tag.extra["model"] == "MIFARE Ultralight / NTAG"


def test_unknown_atqa_sak_pair_still_yields_the_tag_without_a_model():
    parser = TagParser()
    parser.feed("\tSENS RES = 0xBE 0xEF")
    parser.feed("\tSEL RES = 0x99")

    tag = parser.feed(":tag 1234 NFC-A T2T 041A2B3C")

    assert tag.uid == "041A2B3C"
    assert "model" not in tag.extra
    assert tag.extra["atqa"] == "EFBE"


def test_tag_without_any_prose_carries_no_fingerprint_keys():
    parser = TagParser()

    tag = parser.feed(":tag 1234 NFC-A T2T 041A2B3C")

    assert tag == Tag(uid="041A2B3C", tech="NFC-A", protocol="T2T", ts_ms=1234)


def test_fingerprint_does_not_leak_from_one_detection_to_the_next():
    parser = TagParser()
    parser.feed("\tSENS RES = 0x04 0x00")
    parser.feed("\tSEL RES = 0x08")
    parser.feed(":tag 1 NFC-A MIFARE A3912200")

    second = parser.feed(":tag 2 NFC-F T3T 0102030405060708")

    assert second.extra == {}


def test_nfcb_sensb_res_is_not_mistaken_for_an_atqa():
    """NFC-B prints a longer SENSB_RES under the same prose label — it must
    not be truncated into a bogus ATQA."""
    parser = TagParser()
    parser.feed("\tSENS RES = 0x50 0x11 0x22 0x33 0x44")

    tag = parser.feed(":tag 1234 NFC-B ISODEP 11223344")

    assert tag.extra == {}


def test_device_supplied_keys_win_over_the_inferred_ones():
    parser = TagParser()
    parser.feed("\tSENS RES = 0x04 0x00")
    parser.feed("\tSEL RES = 0x08")

    tag = parser.feed(":tag 1234 NFC-A MIFARE A3912200 model=FM11RF08")

    assert tag.extra["model"] == "FM11RF08"


def test_legacy_mode_gets_the_same_fingerprint_as_structured_mode():
    """Every published .uf2 today parses as legacy text, so the fingerprint
    has to work there too - it is the mode real hardware is actually in."""
    parser = TagParser()
    parser.feed(" - POLL MODE: Remote MIFARE card activated")
    parser.feed("\tTechnology: NFC-A")
    parser.feed("\tSENS RES = 0x04 0x00")
    parser.feed("\tNFC ID = 0x32 0x91 0x13 0x20")

    tag = parser.feed("\tSEL RES = 0x08")

    assert tag.uid == "32911320"
    assert tag.extra["model"] == "MIFARE Classic 1K"


def test_tag_event_after_its_own_prose_keeps_the_fingerprint():
    """On a board that emits both, the ':tag' for a detection arrives *after*
    the prose that already closed it in legacy mode — the ATQA/SAK buffer must
    survive that close so the structured event carries them too."""
    parser = TagParser()
    for line in (
        " - POLL MODE: Remote MIFARE card activated",
        "\tTechnology: NFC-A",
        "\tSENS RES = 0x04 0x00",
        "\tNFC ID = 0x32 0x91 0x13 0x20",
        "\tSEL RES = 0x08",
    ):
        parser.feed(line)

    tag = parser.feed(":tag 4521 NFC-A MIFARE 32911320")

    assert tag.extra["model"] == "MIFARE Classic 1K"


def test_a_new_legacy_detection_clears_the_previous_fingerprint():
    parser = TagParser()
    parser.feed(" - POLL MODE: Remote MIFARE card activated")
    parser.feed("\tTechnology: NFC-A")
    parser.feed("\tSENS RES = 0x04 0x00")
    parser.feed("\tNFC ID = 0x32 0x91 0x13 0x20")
    parser.feed("\tSEL RES = 0x08")

    parser.feed(" - POLL MODE: Remote activated tag type: 3")
    parser.feed("\tTechnology: NFC-F")
    tag = parser.feed("\tIDm = 0x01 0x02 0x03 0x04 0x05 0x06 0x07 0x08")

    assert tag.extra == {}


def test_deferred_nfca_is_flushed_when_the_next_detection_starts():
    """Belt and braces: even with neither SEL RES nor `Remove the Card`, the
    held-back tag is emitted rather than dropped."""
    parser = TagParser()
    parser.feed(" - POLL MODE: Remote activated tag type: 2")
    parser.feed("\tTechnology: NFC-A")
    parser.feed("\tNFC ID = 0x04 0x1a 0x2b 0x3c")

    tag = parser.feed(" - POLL MODE: Remote MIFARE card activated")

    assert tag == Tag(uid="041A2B3C", tech="NFC-A", protocol="T2T")
