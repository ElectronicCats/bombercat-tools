#!/usr/bin/env python3

# Electronic Cats
# releases.py — local cache of the `bombercat-firmware` GitHub releases, so
# `bombercat flash` can hand out prebuilt .uf2 images without the arduino-cli
# toolchain (docs/FLASH_PLAN.md §3.2).
#
# This is the testable half of the feature: no click, no rich, no hardware and
# — deliberately — no network until someone asks for it. The constructor only
# looks at disk; `refresh()` is the one method that goes out to GitHub, and the
# `fetch` hook lets the tests replace it with a dict lookup.
# Distributed as-is; no warranty is given.

from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional

# Where the images come from. The env vars exist so a fork (or a checkout with
# a test release) can be used without touching the code.
DEFAULT_REPO = "ElectronicCats/bombercat-firmware"
REPO_ENV = "BOMBERCAT_FIRMWARE_REPO"
CACHE_ENV = "BOMBERCAT_FIRMWARE_CACHE"

INDEX_FILE = "index.json"  # {"tag": "v1.2.0", "checked": "2026-08-21"}
RELEASE_FILE = "release.json"  # the release payload, assets included
DESCRIPTIONS_FILE = "descriptions.json"  # {"bombercat": [{filename, description}]}

# catnip uses timeout=1 for the API, which is too tight for a domestic link
# (FLASH_PLAN §2.3.3). An asset is ~400 KB, hence the longer budget.
API_TIMEOUT = 10.0
ASSET_TIMEOUT = 30.0

USER_AGENT = "bombercat-cli"


class FirmwareError(Exception):
    """Anything that stops us from getting a usable firmware image.

    The library never calls `exit()` (unlike catnip's Flasher, FLASH_PLAN
    §2.3.2) — the command layer catches this and picks the exit code, the same
    contract `DeviceError` already has in core/bombercat.py.
    """


class ReleaseNotFound(FirmwareError):
    """GitHub answered 404. Told apart because `releases/latest` 404s on a repo
    that simply has not published one yet — the state the firmware repo is in
    today — and that deserves an explanation, not a bare URL."""


@dataclass(frozen=True)
class FirmwareImage:
    """One `.uf2` sitting in the cache, plus what the release says it does."""

    name: str  # "NFCGate.uf2"
    path: Path
    description: str
    size: int

    @property
    def stem(self) -> str:
        """The name a user types: `NFCGate.uf2` -> `NFCGate`."""
        return self.path.stem


def _headers() -> Dict[str, str]:
    # Unauthenticated API calls are capped at 60/h per IP; a token raises that
    # to 5000 and costs us one env lookup (FLASH_PLAN §3.2).
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def http_get(url: str, timeout: float = API_TIMEOUT) -> bytes:
    """GET `url`, translating every network failure into a FirmwareError."""
    request = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ReleaseNotFound(f"not found on GitHub: {url}") from e
        if e.code in (403, 429):
            raise FirmwareError(
                "GitHub rate limit reached (60 requests/hour without a token). "
                "Set GITHUB_TOKEN or retry later."
            ) from e
        raise FirmwareError(f"GitHub answered HTTP {e.code} for {url}") from e
    except urllib.error.URLError as e:
        raise FirmwareError(f"could not reach GitHub: {e.reason}") from e
    except OSError as e:  # socket timeouts, DNS, ...
        raise FirmwareError(f"could not reach GitHub: {e}") from e


def parse_descriptions(payload: bytes) -> Dict[str, str]:
    """Flatten `{board: [{filename, description}]}` to `{filename.lower(): desc}`."""
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return {}
    out: Dict[str, str] = {}
    for entries in (data or {}).values():
        for entry in entries or []:
            filename = (entry or {}).get("filename")
            if filename:
                out[filename.lower()] = entry.get("description", "")
    return out


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ReleaseCache:
    """The cached release on disk, and the one method that refills it.

    Layout (`BOMBERCAT_FIRMWARE_CACHE` redirects the root)::

        ~/.bombercat/firmware/
        ├── index.json          # tag currently cached + last revalidation date
        └── v1.2.0/
            ├── NFCGate.uf2
            ├── descriptions.json
            └── release.json

    The explicit `index.json` is what keeps several cached tags from confusing
    each other, which is where catnip's `os.listdir`-and-take-the-first
    approach breaks (FLASH_PLAN §2.3.4).
    """

    def __init__(
        self,
        root: Optional[Path] = None,
        repo: Optional[str] = None,
        fetch: Optional[Callable[[str, float], bytes]] = None,
    ) -> None:
        self.root = Path(
            root or os.environ.get(CACHE_ENV) or Path.home() / ".bombercat" / "firmware"
        )
        self.repo = repo or os.environ.get(REPO_ENV) or DEFAULT_REPO
        # Injected in the tests; "no network" and "bad checksum" become two
        # ordinary function calls instead of urllib surgery (FLASH_PLAN §5).
        self._fetch = fetch or http_get

    # ── what is on disk right now ────────────────────────────────────────────

    @property
    def index_path(self) -> Path:
        return self.root / INDEX_FILE

    def _index(self) -> Dict[str, str]:
        try:
            return json.loads(self.index_path.read_text())
        except (OSError, ValueError):
            return {}

    @property
    def tag(self) -> Optional[str]:
        """The cached tag, or None when the cache is empty or half-written."""
        tag = self._index().get("tag")
        if tag and (self.root / tag).is_dir():
            return tag
        return None

    @property
    def dir(self) -> Optional[Path]:
        tag = self.tag
        return self.root / tag if tag else None

    def descriptions(self) -> Dict[str, str]:
        directory = self.dir
        if not directory:
            return {}
        try:
            return parse_descriptions((directory / DESCRIPTIONS_FILE).read_bytes())
        except OSError:
            # The release should always ship descriptions.json; if it doesn't,
            # every image just gets an empty one (FLASH_PLAN §2.2).
            return {}

    def images(self) -> List[FirmwareImage]:
        """Every `.uf2` in the cache, sorted by name. Never touches the network."""
        directory = self.dir
        if not directory:
            return []
        descriptions = self.descriptions()
        images = [
            FirmwareImage(
                name=path.name,
                path=path,
                description=descriptions.get(path.name.lower(), ""),
                size=path.stat().st_size,
            )
            for path in sorted(directory.glob("*.uf2"))
        ]
        return images

    def find(self, name: str) -> Optional[FirmwareImage]:
        """Resolve what the user typed to one image (FLASH_PLAN §3.5, 2-4).

        Exact asset name, then case-insensitive stem, then a *unique*
        substring. An ambiguous substring raises instead of guessing; a local
        path is the caller's business, since it never reaches the cache.
        """
        images = self.images()
        wanted = name.strip().lower()

        for image in images:
            if image.name.lower() == wanted:
                return image
        for image in images:
            if image.stem.lower() == wanted:
                return image

        matches = [i for i in images if wanted in i.stem.lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            candidates = ", ".join(i.stem for i in matches)
            raise FirmwareError(f"'{name}' matches several firmwares: {candidates}")
        return None

    # ── revalidation ─────────────────────────────────────────────────────────

    def is_stale(self) -> bool:
        """True when the cached tag has not been checked against GitHub today."""
        if self.tag is None:
            return True
        return self._index().get("checked") != date.today().isoformat()

    def _write_index(self, tag: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(
            json.dumps({"tag": tag, "checked": date.today().isoformat()}, indent=2)
        )

    def _no_release_message(self) -> str:
        return (
            f"{self.repo} has no published release to download from (the "
            "firmware build workflow only attaches .uf2 assets when a release "
            "is published, and the repo may not exist under that name). Build "
            f"from source with flash_bombercat.sh, or point {REPO_ENV} at a "
            "fork that has a release."
        )

    def refresh(self, force: bool = False) -> str:
        """Fetch the latest release into the cache and return its tag.

        A no-op (bar the revalidation stamp) when the newest tag is already on
        disk and `force` is not set.
        """
        url = f"https://api.github.com/repos/{self.repo}/releases/latest"
        try:
            payload = self._fetch(url, API_TIMEOUT)
        except ReleaseNotFound as e:
            raise FirmwareError(self._no_release_message()) from e
        try:
            release = json.loads(payload)
        except (ValueError, TypeError) as e:
            raise FirmwareError(f"GitHub sent a malformed release: {e}") from e

        tag = release.get("tag_name")
        if not tag:
            raise FirmwareError(self._no_release_message())

        if tag == self.tag and not force:
            self._write_index(tag)
            return tag

        assets = [
            a
            for a in release.get("assets") or []
            if a.get("name", "").endswith(".uf2") or a.get("name") == DESCRIPTIONS_FILE
        ]
        if not any(a["name"].endswith(".uf2") for a in assets):
            raise FirmwareError(
                f"release {tag} of {self.repo} carries no .uf2 assets — it was "
                "probably published before the firmware build workflow ran."
            )

        # Download into a sibling directory and swap it in at the end, so an
        # interrupted run can never leave a half-populated tag that looks
        # complete (FLASH_PLAN §3.2).
        staging = self.root / f"{tag}.partial"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)

        try:
            for asset in assets:
                self._download_asset(asset, staging)
            (staging / RELEASE_FILE).write_text(
                json.dumps(
                    {
                        key: release.get(key)
                        for key in ("tag_name", "published_at", "body", "assets")
                    },
                    indent=2,
                )
            )
            destination = self.root / tag
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

        self._write_index(tag)
        return tag

    def _download_asset(self, asset: Dict, staging: Path) -> None:
        name = asset["name"]
        url = asset.get("browser_download_url")
        if not url:
            raise FirmwareError(f"asset {name} has no download URL")

        target = staging / name
        target.write_bytes(self._fetch(url, ASSET_TIMEOUT))

        digest = asset.get("digest") or ""
        if not digest.startswith("sha256:"):
            # Releases published before GitHub added `digest` have no checksum
            # to compare against; catnip carries on too.
            return
        expected = digest.split(":", 1)[1].strip().lower()
        actual = _sha256(target)
        if actual != expected:
            raise FirmwareError(
                f"checksum mismatch for {name}: expected {expected}, got {actual}. "
                "The download is corrupt — retry with --refresh."
            )
