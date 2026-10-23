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

import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, FrozenSet, Optional, Tuple

from ..firmware.releases import parse_descriptions as _parse_descriptions

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
CAP_MAGSPOOF = "magspoof"  # magstripe emulation control (`bombercat magspoof …`)
CAP_MIFARE = "mifare"  # Mifare Classic auth/read/write (`bombercat tags mifare …`)
CAP_EMVY = "emvy"  # EMVy swiss-army surface (`bombercat emvy …`)


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
        id="mifareclassic",
        display="MifareClassic",
        uf2="MifareClassic.uf2",
        has_repl=True,  # answers the BomberCatControl REPL (ping/info/identify)
        # Not CAP_TAGS: it emits ':tag' too, but `tags read/watch` is not this
        # firmware's point and `test_only_detecttags_claims_the_tags_capability`
        # keeps that capability exclusive to DetectTags.
        capabilities=frozenset({CAP_MONITOR, CAP_IDENTIFY, CAP_MIFARE}),
        banners=("Mifare Classic reader with PN7150/60",),
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
        capabilities=frozenset({CAP_MONITOR, CAP_IDENTIFY, CAP_MAGSPOOF}),
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
    # EMVy Controller's own "swiss-army" firmware (firmware/EMVyBomberCat). Not an
    # Electronic Cats official image: it speaks the BomberCatControl discovery
    # contract (ping→+OK bombercat / info→fw_name emvybombercat / identify / -ERR)
    # so `status`/`device list` recognise it, but its operational surface (EMV
    # read, APDU passthrough, tag/mag/NDEF/EMV emulation) uses EMVy's own serial
    # dialect, driven by emvy/link.py — NOT the vendor's tags/mifare/relay/
    # magspoof protocols. That surface is CAP_EMVY, its own capability: the
    # vendor's gated commands still correctly refuse against it, and `bombercat
    # emvy …` can require (and auto-flash) exactly this image. Registered here
    # because EMVy vendors these tools and this is the documented place to add a
    # firmware. Its `id` must match the `fw_name` its `info` reports.
    Firmware(
        id="emvybombercat",
        display="EMVyBomberCat",
        # Built by the firmware repo's build-firmware.yml action alongside the
        # Electronic Cats sketches, so it ships as a prebuilt .uf2 release asset
        # that `bombercat flash EMVyBomberCat` (and auto-flash) can install.
        uf2="EMVyBomberCat.uf2",
        has_repl=True,  # answers the BomberCatControl REPL (ping/info/identify)
        capabilities=frozenset({CAP_MONITOR, CAP_IDENTIFY, CAP_PASSTHROUGH, CAP_EMVY}),
        banners=("EMVyBomberCat",),  # best-effort: printed by setup() at boot
        description=(
            "EMVy Controller's swiss-army firmware: EMV read, APDU passthrough and "
            "tag/magstripe/NDEF/EMV-card emulation, driven from EMVy Controller."
        ),
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
    return replace(fw, description=desc)


# ── Lookup helpers ───────────────────────────────────────────────────────────


def by_id(firmware_id: str) -> Firmware:
    return REGISTRY.get(firmware_id, UNKNOWN)


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
    _resolve_device_by_id,
    bombercat_ports,
    describe_devices,
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

# How long to keep re-asking a board that answered `ping` but not `info`.
# The MifareClassic firmware serves its REPL only between operations, and one
# auto-probe sweep of a 1K card takes ~11 s (longer for a card whose sectors
# nothing opens), so the wait has to outlast a sweep before the board can be
# called "busy".
BUSY_RETRY_SECONDS = 12.0
BUSY_RETRY_DELAY_SECONDS = 1.0


@dataclass
class DetectionResult:
    """What `detect_firmware` concluded about one port."""

    firmware: Firmware
    confidence: str  # HANDSHAKE | BANNER | USB | NONE
    port: Optional[str] = None
    version: Optional[str] = None  # firmware version, when the REPL reports it
    usb_present: bool = False  # a BomberCat USB id is on this port
    # The board answered `ping` but never answered `info`: it is alive and
    # busy (the MifareClassic firmware blocks its REPL while it sweeps a card),
    # so its firmware is UNKNOWN — not "an old NFCGate" and not "absent".
    # Callers must not offer to flash a busy board (docs/AUTOFLASH_PLAN.md
    # D-3.4: flashing a healthy board is destructive and pointless).
    busy: bool = False


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
    busy_timeout: Optional[float] = None,
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

    A board that is PRESENT but does not answer is not the same as a board
    running a different firmware: the MifareClassic firmware blocks its whole
    control REPL while it sweeps a card (11 s measured on a 1K card, minutes
    on a card whose sectors nothing opens), so a probe that lands in that
    window sees a silent board. Such a board is reported as `busy` — never
    inferred to be some other firmware — because inferring it used to make
    the CLI offer to flash a healthy board, and the flash resets the board
    (docs/AUTOFLASH_PLAN.md D-3.4: flashing a healthy board is destructive).

    Never raises for a missing/silent board — that is a NONE/USB result, not an
    error. Serial/OS trouble opening the port is swallowed the same way.
    """
    descriptions = load_descriptions()

    def enriched(fw: Firmware) -> Firmware:
        return _enriched(fw, descriptions)

    if busy_timeout is None:
        busy_timeout = BUSY_RETRY_SECONDS

    link = None
    version = None
    try:
        link = DeviceLink(port, baudrate).open()

        def from_info(info) -> Optional[DetectionResult]:
            """The verdict for a board that answered `info` — None when it
            named a firmware this CLI does not know (that stays UNKNOWN)."""
            named = info.data.get("fw_name")
            found = info.data.get("fw")
            # The board is the authority on its own identity; the registry is
            # consulted only to learn what that name is able to do.
            fw = by_id(named) if named and named in REGISTRY else None
            if fw is not None:
                return DetectionResult(
                    firmware=enriched(fw),
                    confidence=HANDSHAKE,
                    port=port,
                    version=found,
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
                    version=found,
                    usb_present=True,
                )
            return None  # names something unknown: not ours, do not infer

        def handshake():
            """(ping, info) — None when the board did not answer `ping`."""
            if not link.ping():
                return None
            info = link.info()
            if not info.ok:
                # `ping` answered but `info` did not: the board is ALIVE but
                # busy. Retry a bounded window — a sweep in progress ends by
                # itself — before concluding anything.
                deadline = time.monotonic() + busy_timeout
                while time.monotonic() < deadline:
                    time.sleep(BUSY_RETRY_DELAY_SECONDS)
                    info = link.info()
                    if info.ok:
                        break
            return info

        info = handshake()
        if info is not None:
            if not info.ok:
                # Alive, busy, and still not answering `info`: present and
                # unidentified, but explicitly NOT a firmware mismatch.
                return DetectionResult(
                    firmware=enriched(UNKNOWN),
                    confidence=USB,
                    port=port,
                    usb_present=True,
                    busy=True,
                )
            version = info.data.get("fw")
            verdict = from_info(info)
            if verdict is not None:
                return verdict
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
        if info is None and usb_present:
            # Present but silent. Before calling it unidentified, give the
            # board a window to come back: a busy board (sweeping a card)
            # answers nothing at all, and mistaking that for "some other
            # firmware" is what led to flashing a healthy board.
            deadline = time.monotonic() + busy_timeout
            while time.monotonic() < deadline:
                time.sleep(BUSY_RETRY_DELAY_SECONDS)
                info = handshake()
                if info is not None:
                    break
            if info is not None:
                if not info.ok:
                    return DetectionResult(
                        firmware=enriched(UNKNOWN),
                        confidence=USB,
                        port=port,
                        usb_present=True,
                        busy=True,
                    )
                version = info.data.get("fw")
                verdict = from_info(info)
                if verdict is not None:
                    return verdict
            # Still silent after the window: present, unidentified, and most
            # likely busy — flagged as such so nobody flashes it.
            return DetectionResult(
                firmware=enriched(UNKNOWN),
                confidence=USB,
                port=port,
                usb_present=True,
                busy=True,
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
        dev = _resolve_device_by_id(device_id, DeviceError)
        return dev.port, dev.usb_tagged

    devices = find_devices()
    if not devices:
        raise DeviceError("no BomberCat found; pass --port (e.g. --port /dev/ttyACM0)")
    if len(devices) > 1:
        raise DeviceError(
            f"multiple BomberCats found ({describe_devices(devices)}); "
            "pass --device/-d <id> (or --port)"
        )
    return devices[0].port, devices[0].usb_tagged
