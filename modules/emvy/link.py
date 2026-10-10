#!/usr/bin/env python3

# Electronic Cats
# link.py — EmvyLink: a serial transport that speaks EMVyBomberCat's *operative
# dialect* (WAIT / APDU: / RESP: / SCAN + JSON_START/END / TAG: / EMU:…), which
# does NOT use the canonical +OK/-ERR/:key framing that DeviceLink
# (core/bombercat.py) understands. DeviceLink would treat `RESP:…`, `READY:` or
# `EMU:RX …` as log noise and time out waiting for a `+`/`-` terminator, so the
# operative verbs need their own client.
#
# EmvyLink composes over serial.Serial (via core/usb_connection.open_serial) and
# offers two primitives: `exchange()` for a one-line request that ends at a known
# terminator, and `stream_until()` for the asynchronous, cancellable EMU:/SCAN
# streams. Discovery (ping/info/identify) still belongs to DeviceLink — this
# client only carries the operative surface. Under the Opción B decision EmvyLink
# is the *transitional bridge* while the firmware migrates to the canonical
# framing; see docs/IMPLEMENTATION_PLAN_EMVyBomberCat_CLI.md §4/§6 Fase 1 and D7.
# Distributed as-is; no warranty is given.

import sys
import time
from typing import Callable, List, Optional, Sequence

import serial

from ..core.usb_connection import (
    DEFAULT_BAUDRATE,
    DEFAULT_TIMEOUT,
    open_serial,
)
from .parser import EmvyError

# Same guard DeviceLink uses: cap one readline() so a wedged firmware spewing
# non-newline bytes can't grow memory without bound (see core/bombercat.py).
_MAX_LINE_BYTES = 4096

# How many times stream_until() reopens the port and resends the trigger when
# the MCU resets mid-stream (USB CDC re-enumerates on reset, so the open fd
# dies with a SerialException). Bounded so a board that is really gone surfaces
# as an error instead of looping forever.
_MAX_RECONNECTS = 5
# Attempts/delay when reopening the port after a reset: re-enumeration can take
# a moment, so a single try is not enough; the sleeps keep the poll gentle.
_REOPEN_ATTEMPTS = 20
_REOPEN_DELAY_S = 0.5

# Prefixes that end a plain `exchange()`. A command whose reply ends on a
# different marker (e.g. `TAG` → `TAG:…`, `CARDSCAN` → `EMU:SCANNED`) passes its
# own `terminators=`. `ERR:` is always terminal.
DEFAULT_TERMINATORS: Sequence[str] = ("RESP:", "ERR:", "OK", "READY:", "NFCINFO:")

# The `#` progress logs the firmware interleaves are useful for the user under
# `-v`, but never terminate an exchange and are never parsed.


def _is_terminator(line: str, terminators: Sequence[str]) -> bool:
    return any(line.startswith(t) for t in terminators)


class EmvyLink:
    """Operative-dialect serial client for EMVyBomberCat.

    Lifecycle mirrors DeviceLink (`open()`/`close()`/context manager). Two I/O
    primitives:

      * ``exchange(line, terminators, timeout)`` — send ``line`` and collect
        reply lines until one matches a terminator prefix (or the deadline).
      * ``stream_until(sentinel, on_line, stop_cmd, timeout)`` — read a stream,
        calling ``on_line`` per line, until a line starts with ``sentinel``;
        on Ctrl-C it sends ``stop_cmd`` (e.g. ``STOP``) before re-raising.

    Both return the raw lines read (including `#` logs and the terminator), which
    the helpers in parser.py turn into values.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = DEFAULT_BAUDRATE,
        timeout: float = DEFAULT_TIMEOUT,
        trace: Optional[Callable[[str, str], None]] = None,
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._trace = trace
        self._ser: Optional[serial.Serial] = None

    # -- tracing ---------------------------------------------------------------
    def _tx(self, text: str) -> None:
        if self._trace is not None:
            self._trace("tx", text)

    def _rx(self, text: str) -> None:
        if self._trace is not None:
            self._trace("rx", text)

    # -- lifecycle -------------------------------------------------------------
    def open(self) -> "EmvyLink":
        self._ser = open_serial(self.port, self.baudrate, self.timeout)
        # Let the CDC settle and drop any boot banner so the first exchange reads
        # its own reply, not stale output (same as DeviceLink.open).
        time.sleep(0.3)
        self._ser.reset_input_buffer()
        return self

    def close(self) -> None:
        if self._ser is not None:
            self._ser.close()
            self._ser = None

    def __enter__(self) -> "EmvyLink":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- reconnect ---------------------------------------------------------
    @staticmethod
    def _notify(on_line: Optional[Callable[[str], None]], text: str) -> None:
        """Surface a reconnect notice: through the stream's on_line callback
        when there is one (the CLI renders it like any other stream line), or
        to stderr when streaming without a callback."""
        if on_line is not None:
            on_line(text)
        else:
            print(text, file=sys.stderr)

    def _reopen(self, budget_s: Optional[float] = None) -> None:
        """Reopen the port after the MCU reset dropped it.

        USB re-enumeration takes a moment, so the open is retried
        ``_REOPEN_ATTEMPTS`` times (``_REOPEN_DELAY_S`` apart) before giving
        up. ``budget_s`` bounds the total wait for callers that have their own
        deadline (a stream ``timeout``): the poll stops early when the budget
        runs out. Raises EmvyError when the port never comes back. Only the
        original port path is retried — a board that re-enumerates under a
        *different* device name is not rediscovered here.
        """
        self.close()
        last: Optional[Exception] = None
        remaining = budget_s
        for _ in range(_REOPEN_ATTEMPTS):
            try:
                self.open()
                return
            except (serial.SerialException, EmvyError) as exc:
                last = exc
                if remaining is not None and remaining <= 0:
                    break
                delay = _REOPEN_DELAY_S
                if remaining is not None:
                    delay = min(delay, remaining)
                    remaining -= delay
                time.sleep(delay)
        raise EmvyError(
            f"port {self.port!r} did not come back: {last} "
            "(if the board re-enumerated under a different name, re-run "
            "the command)"
        )

    # -- low-level I/O ---------------------------------------------------------
    def send(self, line: str) -> None:
        """Write one command line (`\\n`-terminated). Used before a stream, and
        internally by `exchange`. Raises EmvyError on a write failure."""
        if self._ser is None:
            raise EmvyError("link not open")
        sent = line.strip()
        try:
            self._ser.write((sent + "\n").encode("ascii", "replace"))
            self._ser.flush()
            self._tx(sent)
        except serial.SerialTimeoutException as exc:
            raise EmvyError(
                f"device did not accept {line!r} (write timed out); it may be "
                "wedged or not running EMVyBomberCat"
            ) from exc
        except serial.SerialException as exc:
            raise EmvyError(f"serial error sending {line!r}: {exc}") from exc

    def _readline(self, line_ctx: str) -> str:
        """One decoded, newline-stripped line ("" on a read-timeout tick)."""
        if self._ser is None:
            raise EmvyError("link not open")
        try:
            raw = self._ser.readline(_MAX_LINE_BYTES)
        except serial.SerialException as exc:
            raise EmvyError(
                f"serial error reading reply to {line_ctx!r}: {exc}"
            ) from exc
        text = raw.decode("ascii", "replace").strip("\r\n")
        if text:
            self._rx(text)
        return text

    # -- protocol --------------------------------------------------------------
    def exchange(
        self,
        line: str,
        terminators: Sequence[str] = DEFAULT_TERMINATORS,
        timeout: Optional[float] = None,
    ) -> List[str]:
        """Send ``line`` and return every reply line up to and including the
        first that starts with a ``terminators`` prefix.

        Raises EmvyError if the deadline passes with no terminator seen. Blank
        reads (readline timeout ticks) are skipped, so the deadline is the real
        bound. The returned list keeps `#` logs and the terminator line intact
        for the parser layer.
        """
        if self._ser is None:
            raise EmvyError("link not open")
        self._ser.reset_input_buffer()  # strict req/response: drop stale noise
        self.send(line)

        deadline = time.monotonic() + (timeout or self.timeout * 4)
        lines: List[str] = []
        while time.monotonic() < deadline:
            text = self._readline(line)
            if not text:
                continue  # timeout tick; keep waiting until the deadline
            lines.append(text)
            if _is_terminator(text, terminators):
                return lines
        raise EmvyError(f"timed out waiting for a reply to {line!r}")

    def stream_until(
        self,
        sentinel: str,
        on_line: Optional[Callable[[str], None]] = None,
        stop_cmd: Optional[str] = None,
        timeout: Optional[float] = None,
        restart_cmd: Optional[str] = None,
        max_reconnects: int = _MAX_RECONNECTS,
    ) -> List[str]:
        """Read a stream, calling ``on_line`` per non-empty line, until a line
        starts with ``sentinel`` (e.g. ``JSON_END`` for SCAN, ``EMU:DONE`` for
        emulation). Send the trigger command with `send()`/`exchange()` first.

        On KeyboardInterrupt (Ctrl-C) it sends ``stop_cmd`` — so the firmware
        stops emulating instead of streaming on — and re-raises so the CLI exits.
        ``timeout`` bounds a stream that never reaches its sentinel; with the
        default None it waits as long as the underlying readline timeout allows
        between lines (suitable for a genuinely open-ended EMU stream).

        With ``restart_cmd`` (the trigger line that started the stream, e.g.
        ``EMUEMV:…``) the stream survives an MCU reset mid-run: the EMVyBomberCat
        firmware resets when USB re-enumerates (or on some errors), which kills
        the open fd with a SerialException — instead of aborting, the port is
        reopened (bounded retries) and the trigger re-sent so the emulation
        session resumes, each attempt reported as a ``# RECONNECT …`` notice.
        After ``max_reconnects`` failed attempts the last error is raised — the
        reopen failure, when that is what kept failing. The reopen wait counts
        against ``timeout`` when one is set, so a deadline stays a real bound.
        Only the original port path is retried: a board that re-enumerates
        under a different device name is not rediscovered.

        ``restart_cmd`` also marks the stream as owning its trigger, which
        unlocks one more firmware quirk: a leftover emulation orphaned by a
        previous run is preempted by our trigger with an ``EMU:DONE
        reason=restart`` *before* the new session's ``EMU:START`` — that DONE
        belongs to the old session and must not end the stream (without this
        the CLI would exit rc=0 with the new emulation running unobserved).
        Only a restart-DONE seen before our own ``EMU:START`` is ignored; a
        genuine restart later in the session still terminates the stream.
        Callers that don't pass ``restart_cmd`` keep the previous fail-fast
        behavior, on both counts.
        """
        if self._ser is None:
            raise EmvyError("link not open")
        deadline = (time.monotonic() + timeout) if timeout is not None else None
        lines: List[str] = []
        reconnects = 0
        reopen_fail: Optional[EmvyError] = None
        started = False  # an EMU:START of *our* session has been seen

        def _consume(text: str) -> bool:
            """Deliver one stream line; True when it terminates the stream."""
            nonlocal started
            lines.append(text)
            if on_line is not None:
                on_line(text)
            if text.startswith("EMU:START"):
                started = True
            if not text.startswith(sentinel):
                return False
            if restart_cmd is not None and not started and "reason=restart" in text:
                # Our trigger preempted an orphaned emulation: this DONE is
                # the old session's epilogue and the new EMU:START comes next.
                return False
            return True

        try:
            while deadline is None or time.monotonic() < deadline:
                try:
                    text = self._readline(sentinel)
                except EmvyError as exc:
                    # A mid-stream SerialException (MCU reset / USB
                    # re-enumeration) is only recoverable when the caller gave
                    # us the trigger line to re-send after reopening the port.
                    if restart_cmd is None:
                        raise
                    if reconnects >= max_reconnects:
                        # The readline that lands here after a failed reopen
                        # only says "link not open" — surface the reason.
                        raise (reopen_fail or exc)
                    reconnects += 1
                    self._notify(
                        on_line,
                        f"# RECONNECT attempt={reconnects}/{max_reconnects} "
                        f"({exc}); reopening {self.port}",
                    )
                    budget = None
                    if deadline is not None:
                        budget = max(0.0, deadline - time.monotonic())
                    try:
                        self._reopen(budget_s=budget)
                    except EmvyError as reopen_exc:
                        # Not back yet: count it as an attempt and let the loop
                        # retry (the next readline fails on the closed port and
                        # lands back here until max_reconnects exhausts).
                        reopen_fail = reopen_exc
                        self._notify(on_line, f"# RECONNECT {reopen_exc}")
                        continue
                    reopen_fail = None
                    started = False  # the re-sent trigger preempts again
                    self._notify(on_line, "# RECONNECT ok: resuming the session")
                    self.send(restart_cmd)
                    continue
                if not text:
                    continue
                if _consume(text):
                    return lines
        except KeyboardInterrupt:
            if stop_cmd is not None:
                try:
                    self.send(stop_cmd)
                except EmvyError:
                    pass  # best-effort: link may already be gone
            raise
        raise EmvyError(f"timed out waiting for {sentinel!r}")


__all__ = ["EmvyLink", "EmvyError", "DEFAULT_TERMINATORS"]
