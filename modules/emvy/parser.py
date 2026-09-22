#!/usr/bin/env python3

# Electronic Cats
# parser.py — turns EMVyBomberCat's *operative dialect* serial output into plain
# Python values. Unlike magspoof/tags/readers (born speaking the canonical
# `:key value` markers), EMVyBomberCat answers its operative verbs with free
# text — `RESP:<hex>`, `TAG:<proto> UID:<hex>`, `EMU:RX <hex>`, a `JSON_START`/
# `JSON_END` block, etc. These helpers live next to EmvyLink (link.py) and parse
# the line lists it collects. See docs/IMPLEMENTATION_PLAN_EMVyBomberCat_CLI.md
# §2.3 (verb table) and §6 Fase 1.
#
# NOTE ON ERRORS: the plan sketches the point parsers as returning `bytes|Error`.
# We instead *raise* EmvyError on a firmware `ERR:*` (or a malformed reply), so
# the command layer can wrap one operation in a single try/except and turn it
# into `print_error` + a non-zero exit — the same shape every other module uses.
# Distributed as-is; no warranty is given.

import json
import re
from typing import Dict, Iterable, List, Optional


class EmvyError(Exception):
    """An operative verb failed on the device, or its reply was malformed.

    Carries the firmware's own reason where there is one (e.g. the text after
    `ERR:` — `NOCARD`, `BADAPDU`, `TXFAIL`, `SCANFAIL`), so the CLI can surface
    it verbatim.
    """


def _err_reason(line: str) -> str:
    """The text after an `ERR:` marker, or a generic fallback."""
    return line[len("ERR:") :].strip() or "operation failed"


def raise_for_err(lines: Iterable[str]) -> None:
    """Raise EmvyError if the last collected line is a firmware `ERR:`.

    For the plain OK/ERR exchanges (`WAIT`, `RELEASE`, `MAG:`, `STOP`) that
    carry no payload to parse — just a possible error to surface. A no-op
    when the last line isn't an `ERR:` (including an empty `lines`).
    """
    lines = list(lines)
    if lines and lines[-1].strip().startswith("ERR:"):
        raise EmvyError(_err_reason(lines[-1].strip()))


def parse_apdu_resp(lines: Iterable[str]) -> bytes:
    """Extract the APDU response bytes from an `APDU:<hex>` exchange.

    Returns the raw bytes of the first `RESP:<hex>` line. Raises EmvyError on a
    firmware error line (`ERR:NOCARD` / `ERR:BADAPDU` / `ERR:TXFAIL`), on a
    `RESP:` whose payload is not valid hex, or when no `RESP:` is present.
    """
    for line in lines:
        line = line.strip()
        if line.startswith("RESP:"):
            payload = line[len("RESP:") :].strip()
            try:
                return bytes.fromhex(payload)
            except ValueError as exc:
                raise EmvyError(f"RESP: is not valid hex: {payload!r}") from exc
        if line.startswith("ERR:"):
            raise EmvyError(_err_reason(line))
    raise EmvyError("no RESP: line in APDU reply")


def parse_scan_json(lines: Iterable[str]) -> dict:
    """Extract the EMV scan result from a `SCAN` reply.

    The firmware streams `# …` progress logs and then the card data as a JSON
    document delimited by bare `JSON_START` / `JSON_END` lines (buildWebJson,
    .ino). Returns the decoded object. Raises EmvyError when the run failed
    (a `# ERROR: …` log, e.g. the no-card `# ERROR: Timeout`) or when no
    well-formed JSON block is present.
    """
    lines = [ln.rstrip("\r\n") for ln in lines]
    start = end = None
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if stripped == "JSON_START":
            start = i
        elif stripped == "JSON_END":
            end = i
            break
    if start is not None and end is not None and end > start:
        blob = "\n".join(lines[start + 1 : end]).strip()
        try:
            return json.loads(blob)
        except json.JSONDecodeError as exc:
            raise EmvyError(f"malformed JSON in SCAN reply: {exc}") from exc
    # No JSON block: surface the firmware's own error log if it left one.
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("# ERROR"):
            raise EmvyError(stripped.lstrip("# ").strip() or "scan failed")
    raise EmvyError("no JSON block in SCAN reply")


_TAG_RE = re.compile(r"TAG:(?P<proto>\S+)\s+UID:(?P<uid>\S+)", re.IGNORECASE)


def parse_tag(line: str) -> Dict[str, str]:
    """Parse a `TAG:<proto> UID:<hex>` line into ``{proto, uid}``.

    Raises EmvyError on an `ERR:` line or anything that doesn't match.
    """
    line = line.strip()
    if line.startswith("ERR:"):
        raise EmvyError(_err_reason(line))
    m = _TAG_RE.search(line)
    if not m:
        raise EmvyError(f"unrecognized TAG reply: {line!r}")
    return {"proto": m.group("proto"), "uid": m.group("uid")}


def _kv(tokens: Iterable[str]) -> Dict[str, str]:
    """`aid=… pan=… exp=…` tokens → dict; tokens without `=` are ignored."""
    return dict(
        tok.split("=", 1) for tok in tokens if "=" in tok and not tok.startswith("=")
    )


def parse_cardscan(line: str) -> Dict[str, str]:
    """Parse an `EMU:SCANNED aid=… pan=… exp=… t2=…` line into its fields.

    Raises EmvyError on `ERR:NOCARD` / `ERR:SCANFAIL` (or any `ERR:` line).
    """
    line = line.strip()
    if line.startswith("ERR:"):
        raise EmvyError(_err_reason(line))
    if not line.upper().startswith("EMU:SCANNED"):
        raise EmvyError(f"unrecognized CARDSCAN reply: {line!r}")
    rest = line[len("EMU:SCANNED") :].split()
    return _kv(rest)


def parse_emu_event(line: str) -> Optional[Dict[str, object]]:
    """Parse one `EMU:*` streaming line into a structured event.

    Returns ``{"kind": <KIND>, "raw": <text after kind>, "fields": {k: v},
    "hex": <hex payload if the line is a bare hex blob>}`` for lines such as
    `EMU:START mode=emv`, `EMU:RX 00A4…`, `EMU:TX 9000`, `EMU:MSG-SENT …`,
    `EMU:DONE sent=3`. Returns None for a line that is not an `EMU:` event
    (e.g. a `# …` log), so a stream loop can skip it.
    """
    line = line.strip()
    if not line.upper().startswith("EMU:"):
        return None
    body = line[len("EMU:") :]
    kind, _, rest = body.partition(" ")
    kind = kind.upper()
    rest = rest.strip()
    event: Dict[str, object] = {"kind": kind, "raw": rest}
    fields = _kv(rest.split())
    if fields:
        event["fields"] = fields
    elif rest and re.fullmatch(r"[0-9A-Fa-f]+", rest) and len(rest) % 2 == 0:
        event["hex"] = rest
    return event


__all__ = [
    "EmvyError",
    "raise_for_err",
    "parse_apdu_resp",
    "parse_scan_json",
    "parse_tag",
    "parse_cardscan",
    "parse_emu_event",
]
