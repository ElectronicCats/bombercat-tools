#!/usr/bin/env python3

# Electronic Cats
# test_magspoof_parser.py — MagEventParser (modules/magspoof/parser.py) turns
# magspoof serial output into MagEvent objects. Structured ":mag" events
# only — no legacy text mode.
# docs/IMPLEMENTATION_PLAN_MagSpoof_CLI.md §5 phase 2.

from modules.magspoof.parser import MagEvent, MagEventParser


# ── structured ":mag" events ─────────────────────────────────────────────────


def test_track_1_event_is_parsed():
    parser = MagEventParser()

    event = parser.feed(":mag 1234 1")

    assert event.ts_ms == 1234
    assert event.track == 1
    assert event.extra == {}
    assert parser.structured is True


def test_track_2_event_is_parsed():
    parser = MagEventParser()

    event = parser.feed(":mag 500 2")

    assert event.ts_ms == 500
    assert event.track == 2


def test_prose_and_noise_lines_are_ignored():
    parser = MagEventParser()

    assert parser.feed("Activating MagSpoof...") is None
    assert parser.feed("Default tracks:") is None
    assert parser.feed("Track 1: %B123456781234567^LASTNAME/FIRST^...") is None
    assert parser.feed("Updated tracks:") is None
    assert parser.feed("Press the MagSpoof button") is None
    assert parser.structured is False


def test_unrecognised_track_is_kept_as_raw_and_field_stays_none():
    parser = MagEventParser()

    event = parser.feed(":mag 10 3")

    assert event.track is None
    assert event.extra["raw_track"] == "3"
    assert parser.structured is True


def test_crlf_is_tolerated():
    parser = MagEventParser()

    event = parser.feed(":mag 10 1\r\n")

    assert event is not None
    assert event.ts_ms == 10
    assert event.track == 1


def test_unknown_extra_kv_tokens_are_kept_in_extra():
    parser = MagEventParser()

    event = parser.feed(":mag 10 1 src=button")

    assert event.extra == {"src": "button"}


def test_keyless_extra_token_is_skipped():
    parser = MagEventParser()

    event = parser.feed(":mag 10 1 =oops")

    assert "=oops" not in event.extra
    assert event.track == 1


def test_structured_mode_is_sticky():
    parser = MagEventParser()
    parser.feed(":mag 10 1")

    assert parser.structured is True
    assert parser.feed("some unrelated prose") is None
    assert parser.structured is True


def test_default_mag_event_fields_are_none():
    event = MagEvent()
    assert event.ts_ms is None
    assert event.track is None
    assert event.extra == {}
