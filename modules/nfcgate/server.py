#!/usr/bin/env python3

# Electronic Cats
# The nfcgate-server relay, embedded so `bombercat relay server run` needs
# nothing beyond the standard library: no Docker, no server clone, no protobuf.
#
# Adapted from ElectronicCats/nfcgate-server@fc9103d (nfcgate/server@4d32cc1 +
# testserver/latency-fixes.patch), Copyright 2015 NFCGate Team, licensed under
# the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0).
# Changes: no plugin loader and no TLS; the `log` plugin's protobuf decoding is
# replaced by the minimal reader below; session bookkeeping is locked, and frame
# sizes are capped because the port is open to the LAN.
# Distributed as-is; no warranty is given.

import datetime
import os
import socket
import socketserver
import struct
import threading

DEFAULT_PORT = 5566

# Real relay frames are a few hundred bytes at most. The length header comes
# from the network, so cap it instead of trusting a peer to make us allocate
# up to 4 GiB.
MAX_FRAME = 1 << 20

_OPCODES = ("OP_PSH", "OP_SYN", "OP_ACK", "OP_FIN")


# ── frame decoding (what the upstream `log` plugin printed) ───────────────────


def _varint(buf: bytes, i: int):
    value = shift = 0
    while True:
        b = buf[i]  # IndexError on a truncated varint
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _fields(buf: bytes) -> dict:
    """The varint and length-delimited fields of a protobuf message, by number.

    ServerData (c2s.proto) and NFCData (c2c.proto) use nothing else.
    """
    fields, i = {}, 0
    while i < len(buf):
        key, i = _varint(buf, i)
        if key & 7 == 0:
            value, i = _varint(buf, i)
        elif key & 7 == 2:
            n, i = _varint(buf, i)
            if i + n > len(buf):
                raise ValueError("field overruns the message")
            value, i = buf[i : i + n], i + n
        else:
            raise ValueError(f"unexpected wire type {key & 7}")
        fields[key >> 3] = value
    return fields


def describe(data: bytes) -> str:
    """One relayed frame as `OP_PSH C: (initial) 00a4…`, or its hex if malformed."""
    try:
        server_data = _fields(data)
        text = _OPCODES[server_data.get(1, 0)]
        nfc = server_data.get(2, b"")
        if nfc:
            nfc_data = _fields(nfc)
            text += " %s: %s%s" % (
                "C" if nfc_data.get(1, 0) == 1 else "R",  # data_source CARD/READER
                "(initial) " if nfc_data.get(2, 0) == 0 else "",  # data_type
                bytes(nfc_data.get(3, b"")).hex(),
            )
        return text
    except (IndexError, ValueError, TypeError):
        return f"undecodable frame: {data.hex()}"


# ── the relay ─────────────────────────────────────────────────────────────────


class _ClientHandler(socketserver.StreamRequestHandler):
    """One peer: read `!IB` length+session framed messages, relay the payload to
    every other peer in the same session as a `!I` length-prefixed frame."""

    def setup(self):
        super().setup()
        self.session = None
        self.write_lock = threading.Lock()
        self.request.settimeout(300)
        # Disable Nagle (LATENCIA_OPTIMIZACION.md Fase E): the relay is lock-step
        # and every frame is tiny, so Nagle would hold each one for the peer's
        # ~40 ms delayed ACK on every server->board hop.
        try:
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.server.log(self.client_address, "connected")

    def handle(self):
        while True:
            try:
                header = self.rfile.read(5)
            except OSError:  # includes the 300 s idle timeout
                break
            if len(header) < 5:
                break
            length, session = struct.unpack("!IB", header)
            if length == 0 or length > MAX_FRAME:
                break
            data = self.rfile.read(length)
            if len(data) < length:
                break
            # No session number supplied and none set yet.
            if session == 0 and self.session is None:
                break

            if self.session != session:
                self.server.leave(self, self.session)
                self.session = session
                self.server.join(self, session)

            # Fase H: this print sits on the lock-step hot path; --quiet skips it.
            if self.server.log_frames:
                self.server.log(self.client_address, describe(data))
            self.server.publish(session, data, self)

    def finish(self):
        super().finish()
        self.server.leave(self, self.session)
        self.server.log(self.client_address, "disconnected")


class RelayServer(socketserver.ThreadingTCPServer):
    """The nfcgate-server relay: peers that share a session byte see each
    other's frames. `out` receives every log line."""

    # SO_REUSEADDR lets a restart rebind past TIME_WAIT on POSIX; on Windows it
    # would let us silently share a port another server already owns.
    allow_reuse_address = os.name != "nt"
    # Ctrl-C must not wait out a connected peer's 300 s read timeout.
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, log_frames=True, out=print):
        self.log_frames = log_frames
        self._out = out
        self._sessions = {}
        self._lock = threading.Lock()
        super().__init__(address, _ClientHandler)

    def log(self, origin, message):
        stamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        host, port = origin[:2]
        self._out(f"{stamp}  {host}:{port}  {message}")

    def join(self, client, session):
        with self._lock:
            self._sessions.setdefault(session, []).append(client)
        self.log(client.client_address, f"joined session {session}")

    def leave(self, client, session):
        with self._lock:
            peers = self._sessions.get(session)
            if not peers or client not in peers:
                return
            peers.remove(client)
            if not peers:
                del self._sessions[session]
        self.log(client.client_address, f"left session {session}")

    def publish(self, session, data, origin):
        with self._lock:
            peers = [c for c in self._sessions.get(session, ()) if c is not origin]
        # One write per frame so header and payload leave as a single TCP
        # segment (Fase E) instead of two with a delayed-ACK stall between them.
        frame = struct.pack("!I", len(data)) + data
        for peer in peers:
            with peer.write_lock:
                try:
                    peer.wfile.write(frame)
                except OSError:
                    pass  # that peer is going away; its own handler cleans up


def lan_address() -> str:
    """This host's LAN address: what the relay peers must target, not 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            # A UDP connect sends nothing; it only asks the OS which interface
            # routes off-host.
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
