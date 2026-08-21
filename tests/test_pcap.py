#!/usr/bin/env python3

# Electronic Cats
# test_pcap.py — the pcap encoder relayed APDUs are written through
# (modules/capture/pcap.py). Unit-level checks of the byte layout Wireshark's
# iso14443 dissector expects; capture_hosttest.py proves the same bytes really
# dissect under tshark.

import struct

import pytest

from modules.capture.pcap import (
    DLT_ISO_14443,
    EVT_PCD_TO_PICC,
    EVT_PICC_TO_PCD,
    PCAP_MAGIC,
    PcapBuilder,
    global_header,
    iso14443_payload,
    record,
)

APDU = bytes.fromhex("00a404000e325041592e5359532e444446303100")


# ── global header ────────────────────────────────────────────────────────────


def test_global_header_is_classic_pcap_with_the_iso14443_link_type():
    magic, major, minor, zone, sigfigs, snaplen, dlt = struct.unpack(
        "<LHHIILL", global_header()
    )
    assert (magic, major, minor) == (PCAP_MAGIC, 2, 4)
    assert (zone, sigfigs) == (0, 0)
    assert snaplen == 0xFFFF
    assert dlt == DLT_ISO_14443 == 264


def test_global_header_accepts_another_link_type():
    assert struct.unpack("<LHHIILL", global_header(147))[-1] == 147


# ── frame payload ────────────────────────────────────────────────────────────


def test_command_payload_carries_the_pcd_to_picc_event():
    payload = iso14443_payload(True, APDU)
    version, event, length = (
        payload[0],
        payload[1],
        struct.unpack(">H", payload[2:4])[0],
    )

    assert version == 0x00
    assert event == EVT_PCD_TO_PICC == 0xFE
    assert length == len(APDU) + 1  # + the I-block prologue byte
    assert payload[5:] == APDU


def test_response_payload_carries_the_picc_to_pcd_event():
    assert iso14443_payload(False, APDU)[1] == EVT_PICC_TO_PCD == 0xFF


@pytest.mark.parametrize("block_no, pcb", [(0, 0x02), (1, 0x03)])
def test_iblock_prologue_encodes_the_block_number(block_no, pcb):
    assert iso14443_payload(True, APDU, block_no)[4] == pcb


def test_empty_apdu_still_produces_a_well_formed_iblock():
    payload = iso14443_payload(True, b"")
    assert struct.unpack(">H", payload[2:4])[0] == 1 and payload[4] == 0x02


# ── records ──────────────────────────────────────────────────────────────────


def test_record_prefixes_a_packet_header_with_split_timestamps():
    payload = b"\x01\x02\x03"
    sec, usec, incl, orig = struct.unpack(
        "<LLLL", record(payload, 1_700_000_000.25)[:16]
    )

    assert (sec, usec) == (1_700_000_000, 250_000)
    assert incl == orig == len(payload)
    assert record(payload, 1.0)[16:] == payload


def test_record_carries_rounded_microseconds_into_the_seconds_field():
    """0.9999999 s rounds to 1_000_000 µs, which must not be written as-is."""
    sec, usec = struct.unpack("<LL", record(b"\x00", 10.9999999)[:8])
    assert (sec, usec) == (11, 0)


# ── PcapBuilder ──────────────────────────────────────────────────────────────


def test_builder_toggles_the_block_number_on_every_command():
    builder = PcapBuilder()
    first = builder.frame("cmd", APDU, 1.0)
    second = builder.frame("cmd", APDU, 2.0)

    assert first[16 + 4] == 0x03  # first exchange:  block 1
    assert second[16 + 4] == 0x02  # second exchange: block 0


def test_response_echoes_the_block_number_of_its_command():
    builder = PcapBuilder()
    cmd = builder.frame("cmd", APDU, 1.0)
    resp = builder.frame("resp", APDU, 1.1)

    assert resp[16 + 4] == cmd[16 + 4]
    assert cmd[16 + 1] == EVT_PCD_TO_PICC
    assert resp[16 + 1] == EVT_PICC_TO_PCD


def test_builders_are_independent():
    assert PcapBuilder().frame("cmd", APDU, 1.0) == PcapBuilder().frame(
        "cmd", APDU, 1.0
    )
