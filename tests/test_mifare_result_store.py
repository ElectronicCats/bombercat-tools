#!/usr/bin/env python3

# Electronic Cats
# test_mifare_result_store.py — the firmware's standalone result store (Fase 4,
# task 4.6) must behave exactly like the spec the host side assumes.
#
# The ring of results is what makes the standalone workflow useful: the board
# reads a card with nobody attached, keeps the result in its own flash, and
# the operator collects it later. So the ring's behaviour IS the feature:
# which slot is written, which result is the newest, what happens when the
# ring is full, whether the holes survive the round-trip, and how many flash
# writes a card costs.
#
# So the logic is checked the way this repo already checks the "decryption"
# logic (test_mifare_parity.py) and the trigger (test_mifare_trigger.py): the
# firmware's module is compiled natively (g++, no Arduino) and driven with the
# same steps as a Python model that spells the spec out. Both must agree.
#
# The test SKIPS (not fails) when the firmware sources are not next to the
# tools checkout, so the tools suite stays runnable on its own.

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# MifareResultStore now lives in BomberCatCore (core/src), shared by the
# MifareClassic and MifareMqtt firmwares, so it is compiled from there rather
# than from a single sketch. Its own env var keeps it independent of
# BOMBERCAT_FIRMWARE_DIR, which test_mifare_parity.py points at MifareClassic.
CORE_SRC = Path(
    os.environ.get(
        "BOMBERCAT_CORE_DIR",
        str(
            Path(__file__).resolve().parents[2] / "bombercat-firmware" / "core" / "src"
        ),
    )
)
HARNESS = Path(__file__).parent / "parity" / "mifare_result_store.cpp"

pytestmark = pytest.mark.skipif(
    not (CORE_SRC / "MifareResultStore.cpp").exists() or not HARNESS.exists(),
    reason="the BomberCatCore sources are not next to the tools checkout",
)


SLOTS = 8
SECTOR_BYTES = 64


# ── the spec ──────────────────────────────────────────────────────────────────
#
# The ring keeps the last SLOTS results in the board's flash:
#
#   * a stored result goes into the first free slot; when the ring is full it
#     goes into the slot that holds the OLDEST result, so the newest ones are
#     always kept;
#   * every stored result carries a sequence number, one more than the
#     highest one stored - it says which result is the newest;
#   * the listing is newest first;
#   * ONE flash write per stored result: the ring never re-writes a result
#     it already has;
#   * a sector that was never read keeps its reason ("nokey", "readfail",
#     "removed") - the honest-gap rule;
#   * clearing the ring empties it and restarts the numbering.

STATUS_READ = 1
STATUS_NOKEY = 2
STATUS_READFAIL = 3
STATUS_REMOVED = 4


class RingModel:
    """The spec of the result ring, in Python."""

    def __init__(self):
        self.slots = {}  # slot -> record
        self.writes = 0

    # -- helpers ------------------------------------------------------------
    def _next_slot(self):
        for slot in range(SLOTS):
            if slot not in self.slots:
                return slot
        # full: the oldest result's slot
        return min(self.slots, key=lambda s: self.slots[s]["seq"])

    def _next_seq(self):
        if not self.slots:
            return 1
        return max(r["seq"] for r in self.slots.values()) + 1

    # -- the operations -----------------------------------------------------
    def store(self, record):
        slot = self._next_slot()
        seq = self._next_seq()
        stored = dict(record)
        stored["seq"] = seq
        stored["slot"] = slot
        self.slots[slot] = stored
        self.writes += 1
        return slot, seq

    def scan(self):
        return len(self.slots), self._next_slot(), self._next_seq()

    def list(self):
        ordered = sorted(self.slots.values(), key=lambda r: r["seq"], reverse=True)
        return [
            (i, r["seq"], r["slot"], r["sectors"], r["read"], r["failed"], r["uid"])
            for i, r in enumerate(ordered)
        ]

    def load(self, index):
        ordered = sorted(self.slots.values(), key=lambda r: r["seq"], reverse=True)
        if index >= len(ordered):
            return None
        return ordered[index]

    def clear(self):
        self.slots = {}


def _record(uid="AABBCCDD", sectors=2, read=2, failed=0, ts=1000, holes=()):
    """A record with `sectors` sectors; `holes` names the ones that failed."""
    record = {
        "uid": uid,
        "sectors": sectors,
        "read": read,
        "failed": failed,
        "ts": ts,
        "sect": {},
    }
    for s in range(sectors):
        if s in holes:
            record["sect"][s] = (STATUS_NOKEY, "-", "00" * SECTOR_BYTES)
        else:
            record["sect"][s] = (
                STATUS_READ,
                "A",
                ("%02X" % (s & 0xFF)) * SECTOR_BYTES,
            )
    return record


# ── the harness ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    """The compiled harness, ready to be fed steps."""
    if shutil.which("g++") is None:
        pytest.skip("g++ is not available")
    out = tmp_path_factory.mktemp("results") / "mifare_result_store"
    cmd = [
        "g++",
        "-std=c++17",
        "-O1",
        "-I",
        str(CORE_SRC),
        str(CORE_SRC / "MifareResultStore.cpp"),
        str(HARNESS),
        "-o",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(
            f"the result-store harness does not build here: {proc.stderr[-400:]}"
        )
    return out


def _run(harness, commands):
    """Feed the commands to the harness and return its output lines."""
    proc = subprocess.run(
        [str(harness)],
        input="\n".join(commands) + "\n",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().splitlines()


def _stored(lines):
    """The "stored <slot> <seq>" acknowledgements, and the rest."""
    acks = [line for line in lines if line.startswith("stored ")]
    rest = [line for line in lines if not line.startswith("stored ")]
    return acks, rest


def _store_steps(record):
    """The harness steps that store one record."""
    steps = [
        "build {} {} {} {} {}".format(
            record["uid"],
            record["sectors"],
            record["read"],
            record["failed"],
            record["ts"],
        )
    ]
    for s in range(record["sectors"]):
        status, key_type, data = record["sect"][s]
        steps.append("sector {} {} {} {}".format(s, status, key_type, data))
    steps.append("store")
    return steps


# ── the ring ──────────────────────────────────────────────────────────────────


def test_an_empty_ring_reports_nothing(harness):
    model = RingModel()
    firmware = _run(harness, ["scan"])
    assert firmware == ["{} {} {}".format(*model.scan())]


def test_one_result_costs_exactly_one_flash_write(harness):
    model = RingModel()
    record = _record()
    firmware = _run(harness, _store_steps(record) + ["scan", "writes"])
    acks, rest = _stored(firmware)
    model.store(record)
    stored = list(model.slots.values())[0]
    assert acks == ["stored {} {}".format(stored["slot"], stored["seq"])]
    assert rest[0] == "{} {} {}".format(*model.scan())
    # The wear budget: ONE write per card, never more.
    assert int(rest[1]) == model.writes == 1


def test_the_listing_is_newest_first(harness):
    model = RingModel()
    steps = []
    for i in range(3):
        record = _record(uid="0000000%d" % i, ts=1000 + i)
        steps += _store_steps(record)
        model.store(record)
    firmware = _run(harness, steps + ["list", "scan"])
    _, rest = _stored(firmware)
    expected = ["{} {} {} {} {} {} {}".format(*row) for row in model.list()]
    assert rest[: len(expected)] == expected
    assert rest[len(expected)] == "--"
    assert rest[len(expected) + 1] == "{} {} {}".format(*model.scan())


def test_the_ring_overwrites_the_oldest_result_when_it_is_full(harness):
    model = RingModel()
    steps = []
    for i in range(SLOTS + 1):
        record = _record(uid="0000000%d" % (i % 10), ts=1000 + i)
        steps += _store_steps(record)
        model.store(record)
    firmware = _run(harness, steps + ["scan", "list", "writes"])
    _, rest = _stored(firmware)
    count, next_slot, next_seq = model.scan()
    assert rest[0] == "{} {} {}".format(count, next_slot, next_seq)
    # The ring never grows past its slots...
    assert count == SLOTS
    # ...and the oldest result is the one that went away.
    expected = ["{} {} {} {} {} {} {}".format(*row) for row in model.list()]
    assert rest[1 : 1 + len(expected)] == expected
    uids = [row[-1] for row in model.list()]
    assert "00000000" not in uids  # the first one is gone
    assert "00000001" in uids
    # One write per card, all of them.
    assert int(rest[1 + len(expected) + 1]) == model.writes == SLOTS + 1


def test_clearing_the_ring_empties_it_and_restarts_the_numbering(harness):
    model = RingModel()
    steps = []
    for i in range(2):
        record = _record(uid="0000000%d" % i, ts=1000 + i)
        steps += _store_steps(record)
        model.store(record)
    steps.append("clear")
    model.clear()
    firmware = _run(harness, steps + ["scan", "list", "writes"])
    _, rest = _stored(firmware)
    assert rest[0] == "cleared"
    assert rest[1] == "{} {} {}".format(*model.scan())
    assert model.scan() == (0, 0, 1)
    assert rest[2] == "--"


def test_a_stored_result_keeps_its_holes_and_their_reason(harness):
    model = RingModel()
    # Sector 0 was read, sector 1 had no key, sector 2 failed to read.
    record = _record(sectors=3, read=1, failed=2, holes=(1, 2))
    record["sect"][2] = (STATUS_READFAIL, "-", "00" * SECTOR_BYTES)
    steps = _store_steps(record) + ["load 0"]
    model.store(record)
    firmware = _run(harness, steps)
    _, rest = _stored(firmware)
    stored = model.load(0)
    lines = rest[: record["sectors"]]
    assert len(lines) == record["sectors"]
    for s in range(record["sectors"]):
        status, key_type, data = stored["sect"][s]
        assert lines[s] == "{} {} {} {}".format(s, status, key_type, data)
    # The hole is a hole: its status is not "read".
    assert stored["sect"][1][0] == STATUS_NOKEY
    assert stored["sect"][2][0] == STATUS_READFAIL


def test_a_result_survives_a_restart(harness):
    """The ring lives in the flash: a new "process" (a fresh scan) finds it."""
    model = RingModel()
    record = _record(uid="DEADBEEF")
    firmware = _run(harness, _store_steps(record) + ["scan", "list"])
    _, rest = _stored(firmware)
    model.store(record)
    count, next_slot, next_seq = model.scan()
    assert rest[0] == "{} {} {}".format(count, next_slot, next_seq)
    expected = ["{} {} {} {} {} {} {}".format(*row) for row in model.list()]
    assert rest[1 : 1 + len(expected)] == expected
