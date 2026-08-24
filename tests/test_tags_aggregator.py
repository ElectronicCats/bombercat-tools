#!/usr/bin/env python3

# Electronic Cats
# test_tags_aggregator.py — TagAggregator (modules/tags/aggregator.py), the
# per-tag summary behind `bombercat tags scan`.
# docs/CLI_IMPROVEMENTS_DetectTags.md §3.3 (Phase 4).

from modules.tags.aggregator import TagAggregator
from modules.tags.parser import Tag


def test_add_creates_one_row_per_uid():
    agg = TagAggregator()
    agg.add(Tag(uid="041A2B3C", tech="NFC-A", protocol="T2T"))
    agg.add(Tag(uid="A3912200", tech="NFC-A", protocol="MIFARE"))

    assert len(agg) == 2
    assert agg.total_detections == 2


def test_add_collapses_repeats_of_the_same_uid_into_a_count():
    agg = TagAggregator()
    for _ in range(3):
        agg.add(Tag(uid="041A2B3C", tech="NFC-A", protocol="T2T"))

    assert len(agg) == 1
    assert agg.total_detections == 3
    row = agg.to_dict()[0]
    assert row["count"] == 3


def test_to_dict_keeps_tech_and_protocol_from_the_first_detection():
    agg = TagAggregator()
    agg.add(
        Tag(uid="041A2B3C", tech="NFC-A", protocol="T2T", extra={"sens_res": "4400"})
    )
    agg.add(Tag(uid="041A2B3C", tech="NFC-A", protocol="T2T"))

    row = agg.to_dict()[0]
    assert row["tech"] == "NFC-A"
    assert row["protocol"] == "T2T"
    assert row["sens_res"] == "4400"


def test_first_and_last_seconds_are_populated():
    agg = TagAggregator()
    agg.add(Tag(uid="041A2B3C"))

    row = agg.to_dict()[0]
    assert row["first_s"] >= 0
    assert row["last_s"] >= row["first_s"]


def test_tags_with_no_uid_do_not_collapse_into_one_bucket():
    """NFC-B/F in legacy mode report uid=None — each such detection must stay
    its own row instead of merging under one 'unavailable' entry."""
    agg = TagAggregator()
    agg.add(Tag(uid=None, tech="NFC-B", protocol=None, ts_ms=1))
    agg.add(Tag(uid=None, tech="NFC-B", protocol=None, ts_ms=2))

    assert len(agg) == 2
    assert agg.total_detections == 2


def test_to_dict_preserves_first_seen_order():
    agg = TagAggregator()
    agg.add(Tag(uid="AAAA"))
    agg.add(Tag(uid="BBBB"))
    agg.add(Tag(uid="AAAA"))

    uids = [row["uid"] for row in agg.to_dict()]
    assert uids == ["AAAA", "BBBB"]


def test_empty_aggregator():
    agg = TagAggregator()
    assert len(agg) == 0
    assert agg.total_detections == 0
    assert agg.to_dict() == []
