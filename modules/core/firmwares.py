#!/usr/bin/env python3

# Electronic Cats
# firmwares.py — the single source of truth about *which* firmwares exist and
# what the host can do with each one. The CLI used to assume every board runs
# NFCGate; this registry lets `bombercat status` report what is actually
# flashed and lets each command adapt to (or degrade honestly for) the rest.
#
# See docs/GENERALIZE_CLI_PLAN.md §2.1–2.2. The registry is the ONE place to
# edit when a firmware is added: `.uf2` name, whether it speaks the control
# REPL, its capabilities, and any boot banners we can sniff best-effort.
# Distributed as-is; no warranty is given.

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Optional, Tuple

# ── Capabilities vocabulary ──────────────────────────────────────────────────
# What a firmware lets the host *do*. `status` uses this to suggest the next
# command; commands can use it to refuse/degrade gracefully.
CAP_RELAY = "relay"  # full NFCGate relay control (config/run/stop/status)
CAP_CONFIG = "config"  # WiFi/relay params over the REPL
CAP_MONITOR = "monitor"  # readable serial output worth streaming
CAP_IDENTIFY = "identify"  # can blink its LED on command
CAP_CAPTURE = "capture"  # APDU capture to pcap
CAP_PASSTHROUGH = "passthrough"  # transparent serial bridge (no REPL)
CAP_TAGS = "tags"  # NFC tag detection (`bombercat tags read/watch`)
CAP_READERS = (
    "readers"  # NFC reader/terminal detection (`bombercat readers read/watch`)
)


@dataclass(frozen=True)
class Firmware:
    """One firmware image and how the host can see and use it."""

    id: str  # stable slug: "nfcgate", "detecttags", …
    display: str  # human name: "NFCGate"
    uf2: str  # image name, must match descriptions.json / flash
    has_repl: bool  # speaks the ping/info control protocol?
    capabilities: FrozenSet[str] = frozenset()
    banners: Tuple[str, ...] = ()  # boot-output substrings for best-effort sniff
    description: str = ""  # filled from descriptions.json when available

    def can(self, capability: str) -> bool:
        return capability in self.capabilities


# ── The registry ─────────────────────────────────────────────────────────────
# Order is the order `status`/help list them in. Only NFCGate has a REPL today;
# the rest are recognised best-effort by USB presence (+ optional banner).
_ENTRIES = (
    Firmware(
        id="nfcgate",
        display="NFCGate",
        uf2="NFCGate.uf2",
        has_repl=True,
        capabilities=frozenset(
            {CAP_RELAY, CAP_CONFIG, CAP_MONITOR, CAP_IDENTIFY, CAP_CAPTURE}
        ),
        # No boot banner on purpose: "+OK bombercat" is the *reply* to `ping`,
        # not something the sketch prints at boot, and listing it here would let
        # it shadow a genuine banner match (see _match_banner).
        banners=(),
    ),
    Firmware(
        id="detecttags",
        display="DetectTags",
        uf2="DetectTags.uf2",
        has_repl=True,  # answers the BomberCatControl REPL (ping/info/identify)
        capabilities=frozenset({CAP_MONITOR, CAP_IDENTIFY, CAP_TAGS}),
        banners=("Detect NFC tags with PN7150", "Detect NFC tags"),
    ),
    Firmware(
        id="detectreaders",
        display="DetectReaders",
        uf2="DetectReaders.uf2",
        has_repl=True,  # answers the BomberCatControl REPL (ping/info/identify)
        capabilities=frozenset({CAP_MONITOR, CAP_IDENTIFY, CAP_READERS}),
        banners=("Detect NFC readers",),
    ),
    Firmware(
        id="magspoof",
        display="magspoof",
        uf2="magspoof.uf2",
        has_repl=True,  # answers the BomberCatControl REPL (ping/info/identify)
        capabilities=frozenset({CAP_MONITOR, CAP_IDENTIFY}),
        banners=("Default tracks:",),  # printed by setupTracks() at boot
    ),
    Firmware(
        id="magspoofcvsattack",
        display="MagspoofCVSAttack",
        uf2="MagspoofCVSAttack.uf2",
        has_repl=True,  # answers the BomberCatControl REPL (ping/info/identify)
        capabilities=frozenset({CAP_MONITOR, CAP_IDENTIFY}),
        banners=("MagSpoof Attack!!",),
    ),
    Firmware(
        id="magspoofmqtt",
        display="MagSpoofMqtt",
        uf2="MagSpoofMqtt.uf2",
        has_repl=True,  # answers the BomberCatControl REPL (ping/info/identify)
        capabilities=frozenset({CAP_MONITOR, CAP_IDENTIFY}),
        banners=("Ready MQTT MagSpoof",),
    ),
    Firmware(
        id="nfcgate_wifiwebserver",
        display="WiFiWebServer",
        uf2="WiFiWebServer.uf2",
        has_repl=True,  # answers the BomberCatControl REPL (ping/info/identify)
        capabilities=frozenset(
            {CAP_IDENTIFY}
        ),  # driven from a browser, nothing on serial
        # Its boot output is magspoof's, verbatim (both call the same
        # setupTracks()), so no banner can tell them apart. It does not need
        # one: it answers the REPL and names itself.
        banners=(),
    ),
    Firmware(
        id="host_relay_nfc",
        display="host_Relay_NFC",
        uf2="host_Relay_NFC.uf2",
        has_repl=False,  # legacy sketch: no SerialControl/BomberCatControl
        capabilities=frozenset({CAP_MONITOR}),
        banners=("Host Relay NFC",),  # end of setup(), next to its CLI greeting
    ),
    Firmware(
        id="client_relay_nfc",
        display="client_Relay_NFC",
        uf2="client_Relay_NFC.uf2",
        has_repl=False,  # legacy sketch: no SerialControl/BomberCatControl
        capabilities=frozenset({CAP_MONITOR}),
        banners=("Client Relay NFC",),  # only when its `debug` flag is on
    ),
    Firmware(
        id="esp32passthrough",
        display="ESP32SerialPassthroughFlash",
        uf2="ESP32SerialPassthroughFlash.uf2",
        has_repl=False,
        capabilities=frozenset({CAP_PASSTHROUGH}),
        banners=(),  # raw ESP32 UART; nothing we can match reliably
    ),
)

# A board that is present (USB) but whose firmware we cannot name. `status`
# reports this honestly instead of guessing.
UNKNOWN = Firmware(
    id="unknown",
    display="Unknown / none",
    uf2="",
    has_repl=False,
    capabilities=frozenset(),
    banners=(),
    description="A BomberCat is present but its firmware could not be identified.",
)

REGISTRY: Dict[str, Firmware] = {fw.id: fw for fw in _ENTRIES}
_BY_UF2: Dict[str, Firmware] = {fw.uf2.lower(): fw for fw in _ENTRIES}


# ── Descriptions from descriptions.json (best-effort enrichment) ──────────────
# The firmware repo ships descriptions.json ({board: [{filename, description}]}).
# We reuse it so the registry doesn't duplicate prose, but the CLI never
# *depends* on the file being present — a missing file just leaves descriptions
# empty. Search order: env override, the sibling firmware checkout, the flash
# release cache.
_DESC_ENV = "BOMBERCAT_DESCRIPTIONS"


def _candidate_description_paths() -> Tuple[Path, ...]:
    paths = []
    override = os.environ.get(_DESC_ENV)
    if override:
        paths.append(Path(override))
    here = Path(__file__).resolve()
    # tools/modules/core/firmwares.py -> repo roots to try for the sibling repo.
    tools_root = here.parents[2]
    paths.append(tools_root.parent / "bombercat-firmware" / "descriptions.json")
    # The flash release cache (see firmware/releases.py CACHE_ENV default).
    cache = os.environ.get("BOMBERCAT_FIRMWARE_CACHE")
    cache_root = Path(cache) if cache else Path.home() / ".bombercat" / "firmware"
    if cache_root.is_dir():
        for sub in sorted(cache_root.glob("*/descriptions.json")):
            paths.append(sub)
    return tuple(paths)


def _parse_descriptions(payload: bytes) -> Dict[str, str]:
    """{board: [{filename, description}]} -> {filename.lower(): description}."""
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        # Reachable in practice: this reads user-editable, remote-persisted
        # cache files, not just malicious JSON (docs/AUDIT_ERROR_HANDLING.md M2).
        return {}
    out: Dict[str, str] = {}
    for entries in data.values():
        for entry in entries or []:
            filename = (entry or {}).get("filename")
            if filename:
                out[filename.lower()] = entry.get("description", "")
    return out


def load_descriptions() -> Dict[str, str]:
    """First readable descriptions.json wins; {} if none is found."""
    for path in _candidate_description_paths():
        try:
            return _parse_descriptions(path.read_bytes())
        except OSError:
            continue
    return {}


def _enriched(fw: Firmware, descriptions: Dict[str, str]) -> Firmware:
    desc = descriptions.get(fw.uf2.lower())
    if not desc:
        return fw
    return Firmware(
        id=fw.id,
        display=fw.display,
        uf2=fw.uf2,
        has_repl=fw.has_repl,
        capabilities=fw.capabilities,
        banners=fw.banners,
        description=desc,
    )


# ── Lookup helpers ───────────────────────────────────────────────────────────


def by_id(firmware_id: str) -> Firmware:
    return REGISTRY.get(firmware_id, UNKNOWN)


def by_uf2(uf2_name: str) -> Optional[Firmware]:
    """Match a `.uf2` file name (case-insensitive) to its firmware."""
    return _BY_UF2.get((uf2_name or "").lower())


def repl_firmwares() -> Tuple[Firmware, ...]:
    """Firmwares that answer the control handshake.

    NFCGate (full SerialControl) plus the sketches that embed the small
    BomberCatControl REPL. The legacy relay pair and the ESP32 passthrough have
    no REPL at all, so they can only be recognised by banner — which is why
    `has_repl` must stay honest here.
    """
    return tuple(fw for fw in _ENTRIES if fw.has_repl)


def all_firmwares(enrich: bool = True) -> Tuple[Firmware, ...]:
    """The registry, optionally enriched with descriptions.json prose."""
    if not enrich:
        return _ENTRIES
    descriptions = load_descriptions()
    return tuple(_enriched(fw, descriptions) for fw in _ENTRIES)


# ── Firmware detection ────────────────────────────────────────────────────────
# A firmware that speaks the REPL names itself (`info` -> `:fw_name`), so it is
# identified with certainty. For everything else — the legacy relay pair, the
# passthrough, and any board still running an image built before the REPL
# existed — we fall back to the boot banner, and failing that we degrade with
# honesty ("a BomberCat is present, firmware not identifiable") rather than
# guess. detect_firmware returns a confidence level so callers can say exactly
# how sure they are. See docs/GENERALIZE_CLI_PLAN.md §2.2.

# Imported here (not at top) so this module stays importable even if the serial
# transport isn't — and to keep the registry usable in tests without hardware.
from .bombercat import DeviceError, DeviceLink  # noqa: E402
from .usb_connection import (  # noqa: E402
    DEFAULT_BAUDRATE,
    bombercat_ports,
    describe_devices,
    find_device,
    find_devices,
)

# Confidence levels, most to least certain.
HANDSHAKE = "handshake"  # answered the control REPL and named itself — certain
INFERRED = "inferred"  # answered the REPL but did not name itself — see below
BANNER = "banner"  # matched a boot banner — likely
USB = "usb"  # present by USB id, firmware unknown
NONE = "none"  # nothing there

# The one firmware whose builds could answer the REPL before `info` learned to
# report `fw_name`. The other five gained their REPL in the very same firmware
# refactor that added the field, so a board that answers `ping` yet reports no
# name is an older NFCGate. Sound, but an inference — hence INFERRED.
_PRE_FW_NAME_REPL = "nfcgate"


@dataclass
class DetectionResult:
    """What `detect_firmware` concluded about one port."""

    firmware: Firmware
    confidence: str  # HANDSHAKE | BANNER | USB | NONE
    port: Optional[str] = None
    version: Optional[str] = None  # firmware version, when the REPL reports it
    usb_present: bool = False  # a BomberCat USB id is on this port

    @property
    def identified(self) -> bool:
        """Did we end up with a name? (Not necessarily a certain one.)"""
        return self.confidence in (HANDSHAKE, INFERRED, BANNER)


def _match_banner(lines) -> Optional[Firmware]:
    """The firmware whose banner appears in the sniffed output, if exactly one.

    Every entry is eligible, `has_repl` or not: we only get here because `ping`
    went unanswered, and a board running a build made before its firmware grew
    the REPL is precisely the case this level exists for. (Filtering on
    `has_repl` made this function unreachable the day six firmwares gained one.)

    Two firmwares matching the same output is not a tie to break by registry
    order — it is a lack of evidence, and the caller degrades to "present, not
    identified" instead of naming one at random.
    """
    blob = "\n".join(lines)
    matches = [fw for fw in _ENTRIES if any(b and b in blob for b in fw.banners)]
    return matches[0] if len(matches) == 1 else None


def detect_firmware(
    port: str,
    sniff: bool = True,
    usb_present: bool = True,
    baudrate: int = DEFAULT_BAUDRATE,
) -> DetectionResult:
    """Identify what firmware a board is running, by levels of confidence.

    1. Handshake (certain): the control REPL answers `ping`, and `info` says
       which firmware it is (`fw_name`) and at what version.
    1b. Inferred: the REPL answers but reports no `fw_name` — an image built
       before the field existed, which narrows it to NFCGate. Named, but said
       to be an inference. A board that reports a name we do not know is not
       inferred at all: it is something else entirely (GENERALIZE_CLI_PLAN §2.5).
    2. Banner (likely, opt-out via ``sniff=False``): no REPL, but a boot-output
       substring matches a registry banner.
    3. USB (present, unknown): a BomberCat USB id is here but nothing identified
       it -> UNKNOWN, honestly.
    4. None: not a BomberCat / nothing answered.

    Never raises for a missing/silent board — that is a NONE/USB result, not an
    error. Serial/OS trouble opening the port is swallowed the same way.
    """
    descriptions = load_descriptions()

    def enriched(fw: Firmware) -> Firmware:
        return _enriched(fw, descriptions)

    link = None
    version = None
    try:
        link = DeviceLink(port, baudrate).open()
        if link.ping():
            info = link.info()
            version = info.data.get("fw") if info.ok else None
            named = info.data.get("fw_name") if info.ok else None
            # The board is the authority on its own identity; the registry is
            # consulted only to learn what that name is able to do.
            fw = by_id(named) if named and named in REGISTRY else None
            if fw is not None:
                return DetectionResult(
                    firmware=enriched(fw),
                    confidence=HANDSHAKE,
                    port=port,
                    version=version,
                    usb_present=True,
                )
            if not named:
                # Silent about its identity -> a pre-`fw_name` build. Reporting
                # it as NFCGate is right for every image published so far, but
                # this must never be dressed up as HANDSHAKE certainty now that
                # six firmwares answer the same handshake.
                return DetectionResult(
                    firmware=enriched(by_id(_PRE_FW_NAME_REPL)),
                    confidence=INFERRED,
                    port=port,
                    version=version,
                    usb_present=True,
                )
            # It named itself something the registry has never heard of: a
            # newer or custom firmware. Inferring anything here would be wrong.
            usb_present = True
        if sniff:
            match = _match_banner(link.read_lines())
            if match is not None:
                return DetectionResult(
                    firmware=enriched(match),
                    confidence=BANNER,
                    port=port,
                    version=version,
                    usb_present=usb_present,
                )
    except (DeviceError, OSError):
        # Opening or probing failed; fall through to the USB/none verdict.
        pass
    finally:
        if link is not None:
            try:
                link.close()
            except Exception:
                pass

    if usb_present:
        return DetectionResult(
            firmware=UNKNOWN,
            confidence=USB,
            port=port,
            version=version,  # set when the REPL named a firmware we do not know
            usb_present=True,
        )
    return DetectionResult(
        firmware=UNKNOWN, confidence=NONE, port=port, version=version, usb_present=False
    )


def resolve_status_port(
    preferred: Optional[str] = None, device_id: Optional[int] = None
) -> Tuple[str, bool]:
    """Pick a port for `status` WITHOUT a handshake, returning (port, usb_tagged).

    Unlike core.bombercat.resolve_port, this must work for boards running any of
    the non-REPL firmwares, so it never probes the control protocol — it selects
    purely by USB enumeration (`bombercat device list` numbering).

      * ``preferred`` (--port) wins, used as-is.
      * ``device_id`` (--device/-d) selects a numbered board.
      * otherwise: the single attached BomberCat, or a DeviceError asking for -d.
    """
    if preferred and device_id is not None:
        raise DeviceError("--port and --device are mutually exclusive; pass one")
    if preferred:
        tagged = {p.device for p in bombercat_ports()}
        return preferred, preferred in tagged

    if device_id is not None:
        dev = find_device(device_id)
        if dev is not None:
            return dev.port, dev.usb_tagged
        known = find_devices()
        if known:
            raise DeviceError(
                f"no BomberCat with ID {device_id}; attached: "
                f"{describe_devices(known)} (see `bombercat device list`)"
            )
        raise DeviceError(
            f"no BomberCat with ID {device_id}: none is attached "
            "(see `bombercat device list`)"
        )

    devices = find_devices()
    if not devices:
        raise DeviceError("no BomberCat found; pass --port (e.g. --port /dev/ttyACM0)")
    if len(devices) > 1:
        raise DeviceError(
            f"multiple BomberCats found ({describe_devices(devices)}); "
            "pass --device/-d <id> (or --port)"
        )
    return devices[0].port, devices[0].usb_tagged
