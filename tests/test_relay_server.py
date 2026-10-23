#!/usr/bin/env python3

# Electronic Cats
# test_relay_server.py — the embedded nfcgate-server behind
# `bombercat relay server run` (modules/nfcgate/server.py): frame decoding, the
# relay between two real sockets on loopback, and the CLI's bind errors.

import socket
import struct
import threading
import time

import pytest

from conftest import flat
from modules.nfcgate.cli import relay
from modules.nfcgate.server import MAX_FRAME, RelayServer, describe

# ServerData{OP_PSH, NFCData{CARD, INITIAL, data=00a4, timestamp=…}}, as the
# real protobuf runtime serialises it (timestamp is a field we skip).
PSH_CARD_FRAME = bytes.fromhex("120d08011a0200a420cb89ec8ff723")


# ── describe ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "frame, expected",
    [
        (PSH_CARD_FRAME, "OP_PSH C: (initial) 00a4"),
        (bytes.fromhex("0801"), "OP_SYN"),
        (bytes.fromhex("120708011001 1a01ff".replace(" ", "")), "OP_PSH C: ff"),
        (bytes.fromhex("1204 1a0290 00".replace(" ", "")), "OP_PSH R: (initial) 9000"),
    ],
)
def test_describe_matches_the_upstream_log_plugin(frame, expected):
    assert describe(frame) == expected


@pytest.mark.parametrize(
    "garbage",
    [
        b"\x12\x50\x08",  # length runs past the end
        b"\x08",  # truncated varint
        b"\x08\x09",  # opcode outside OP_PSH..OP_FIN
        b"\x0d\x00\x00\x00\x00",  # fixed32 wire type: not ours
        b"\xff" * 12,  # endless varint
    ],
)
def test_describe_falls_back_to_hex_on_a_malformed_frame(garbage):
    assert describe(garbage) == f"undecodable frame: {garbage.hex()}"


# ── the relay ────────────────────────────────────────────────────────────────


@pytest.fixture
def server():
    lines = []
    srv = RelayServer(("127.0.0.1", 0), out=lines.append)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    srv.lines = lines
    yield srv
    srv.shutdown()
    srv.server_close()


def _peer(srv):
    return socket.create_connection(srv.server_address, timeout=2)


def _send(sock, payload, session=1):
    sock.sendall(struct.pack("!IB", len(payload), session) + payload)


def _join(srv, sock, payload=b"\x08\x01", session=1):
    """Send a frame and wait until the server has put `sock` in the session.

    Each peer has its own handler thread, so without this a later peer's frame
    can be relayed before an earlier peer has even joined.
    """
    before = sum("joined session" in line for line in srv.lines)
    _send(sock, payload, session)
    deadline = time.monotonic() + 2
    while sum("joined session" in line for line in srv.lines) == before:
        assert time.monotonic() < deadline, "peer never joined"
        time.sleep(0.01)


def _recv(sock):
    header = sock.recv(4, socket.MSG_WAITALL)
    (length,) = struct.unpack("!I", header)
    return sock.recv(length, socket.MSG_WAITALL)


def test_peers_in_a_session_relay_to_each_other(server):
    reader, card = _peer(server), _peer(server)
    with reader, card:
        _join(server, reader)  # session 1, nobody to relay to yet
        _send(card, b"\x08\x02")  # joins; relayed to the reader
        assert _recv(reader) == b"\x08\x02"

        _send(reader, PSH_CARD_FRAME)
        assert _recv(card) == PSH_CARD_FRAME

    assert any("OP_PSH C: (initial) 00a4" in line for line in server.lines)


def test_a_frame_is_not_echoed_back_nor_crosses_sessions(server):
    a, b, other = _peer(server), _peer(server), _peer(server)
    with a, b, other:
        _join(server, other, session=2)
        _join(server, a)
        _send(b, b"\x08\x01")
        assert _recv(a) == b"\x08\x01"  # b's join reached a…

        other.settimeout(0.3)
        with pytest.raises(socket.timeout):  # …but not session 2
            other.recv(1)
        a.settimeout(0.3)
        with pytest.raises(socket.timeout):  # and a never hears itself
            a.recv(1)


def test_an_oversized_length_header_drops_the_peer(server):
    peer = _peer(server)
    with peer:
        peer.sendall(struct.pack("!IB", MAX_FRAME + 1, 1))
        assert peer.recv(1) == b""  # closed, nothing allocated


def test_quiet_skips_the_per_frame_log(server):
    server.log_frames = False
    a, b = _peer(server), _peer(server)
    with a, b:
        _join(server, a, PSH_CARD_FRAME)
        _send(b, PSH_CARD_FRAME)
        _recv(a)

    assert not any("OP_PSH" in line for line in server.lines)


# ── bombercat relay server run ───────────────────────────────────────────────


def test_run_explains_a_port_already_in_use(runner):
    with socket.socket() as holder:
        holder.bind(("0.0.0.0", 0))
        holder.listen()
        port = holder.getsockname()[1]

        result = runner.invoke(relay, ["server", "run", "-p", str(port)])

    assert result.exit_code == 1
    assert f"Port {port} is already in use" in flat(result.output)


@pytest.mark.parametrize("bad_port", ["0", "65536"])
def test_run_rejects_a_port_outside_1_65535(runner, bad_port):
    result = runner.invoke(relay, ["server", "run", "-p", bad_port])
    assert result.exit_code == 2


def test_run_serves_until_ctrl_c(runner, monkeypatch):
    def interrupted(self, *a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(RelayServer, "serve_forever", interrupted)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    result = runner.invoke(relay, ["server", "run", "-p", str(port)])

    assert result.exit_code == 0
    out = flat(result.output)
    assert f"listening on port {port}" in out
    assert "--server " in out and f":{port} --session 1" in out
    assert "server stopped" in out
