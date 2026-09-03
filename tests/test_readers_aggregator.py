#!/usr/bin/env python3

# Electronic Cats
# test_readers_aggregator.py — ReaderAggregator (modules/readers/aggregator.py),
# the per-fingerprint summary behind `bombercat readers scan`.
# docs/IMPLEMENTATION_PLAN_DetectReaders_CLI.md §5 phase 3.

from modules.readers.aggregator import ReaderAggregator
from modules.readers.parser import Reader


def test_two_ppse_selects_in_a_row_collapse_into_one_row():
    agg = ReaderAggregator()
    for _ in range(2):
        agg.add(
            Reader(
                label="emv-payment",
                tech="NFC-A",
                protocol="ISODEP",
                intf="ISODEP",
                apdu="00A4040007A0000000031010",
            )
        )

    assert len(agg) == 1
    assert agg.total_detections == 2
    row = agg.to_dict()[0]
    assert row["count"] == 2
    assert row["label"] == "emv-payment"


def test_unknown_readers_of_different_tech_stay_separate_rows():
    agg = ReaderAggregator()
    agg.add(Reader(label="unknown", tech="NFC-A", protocol="ISODEP"))
    agg.add(Reader(label="unknown", tech="NFC-B", protocol="FRAME"))

    assert len(agg) == 2
    assert agg.total_detections == 2


def test_add_creates_one_row_per_fingerprint():
    agg = ReaderAggregator()
    agg.add(Reader(label="visa", aid="A0000000031010", tech="NFC-A", protocol="ISODEP"))
    agg.add(
        Reader(
            label="mastercard", aid="A0000000041010", tech="NFC-A", protocol="ISODEP"
        )
    )

    assert len(agg) == 2
    assert agg.total_detections == 2


def test_to_dict_preserves_first_seen_order():
    agg = ReaderAggregator()
    agg.add(Reader(label="visa", tech="NFC-A", protocol="ISODEP"))
    agg.add(Reader(label="mastercard", tech="NFC-A", protocol="ISODEP"))
    agg.add(Reader(label="visa", tech="NFC-A", protocol="ISODEP"))

    labels = [row["label"] for row in agg.to_dict()]
    assert labels == ["visa", "mastercard"]


def test_first_and_last_seconds_are_populated():
    agg = ReaderAggregator()
    agg.add(Reader(label="ndef", tech="NFC-A", protocol="ISODEP"))

    row = agg.to_dict()[0]
    assert row["first_s"] >= 0
    assert row["last_s"] >= row["first_s"]


def test_extra_keys_colliding_with_computed_columns_are_renamed():
    """A device sending `count=`/`label=` in extra must not corrupt the real
    computed columns of the same name (M13, mirrors TagAggregator)."""
    agg = ReaderAggregator()
    agg.add(
        Reader(
            label="emv-payment",
            tech="NFC-A",
            protocol="ISODEP",
            extra={"count": "99", "label": "spoofed"},
        )
    )

    row = agg.to_dict()[0]
    assert row["count"] == 1  # the real computed count, untouched
    assert row["label"] == "emv-payment"
    assert row["x_count"] == "99"
    assert row["x_label"] == "spoofed"


def test_empty_aggregator():
    agg = ReaderAggregator()
    assert len(agg) == 0
    assert agg.total_detections == 0
    assert agg.to_dict() == []
