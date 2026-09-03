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
import re
import shutil
import time
import urllib.error
import urllib.parse
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

# A hostile or misbehaving server (C2: the token-stripped host can still be
# anything reachable over https on github.com/objects.githubusercontent.com)
# must not be able to OOM the process with a giant or slow-drip response
# (docs/AUDIT_ERROR_HANDLING.md M1). No real release asset is anywhere close
# to this.
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
DOWNLOAD_CHUNK = 1 << 16
# `timeout` only bounds a single socket read; a slow-drip server that trickles
# one chunk per read just under that limit would otherwise hold the
# connection open indefinitely. Bound the whole transfer too.
DOWNLOAD_DEADLINE_MULTIPLIER = 6

USER_AGENT = "bombercat-cli"

# tag_name / asset name come straight from the GitHub API response; a hostile
# value like "../../.config" must never reach a Path join that builds a cache
# location (docs/AUDIT_ERROR_HANDLING.md C1).
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Only these hosts ever see the GITHUB_TOKEN, and only these hosts are
# trusted to serve an asset download — a hostile release cannot point
# `browser_download_url` at an arbitrary server and collect the token
# (docs/AUDIT_ERROR_HANDLING.md C2).
API_HOST = "api.github.com"
ASSET_HOSTS = {"github.com", "objects.githubusercontent.com"}


def _validate_path_component(value: str, what: str) -> None:
    if not value or value in (".", "..") or not _SAFE_NAME_RE.match(value):
        raise FirmwareError(f"unsafe {what} from GitHub release: {value!r}")


def _validate_asset_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ASSET_HOSTS:
        raise FirmwareError(f"refusing to download asset from untrusted URL: {url}")


# owner/repo, GitHub's own naming rules (alnum + hyphens for the owner,
# alnum/./_/- for the repo). Validated once at construction so a malformed
# BOMBERCAT_FIRMWARE_REPO fails with a clear message instead of a cryptic
# HTTP error surfacing deep inside the first API call (docs/AUDIT_ERROR_HANDLING.md L17).
_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+$")


def _validate_repo(repo: str) -> None:
    if not _REPO_RE.match(repo):
        raise FirmwareError(f"{REPO_ENV!r} must look like 'owner/repo', got {repo!r}")


class _StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Cross-host redirects must not carry the GitHub token along with them.

    The default handler re-sends every header — Authorization included — to
    wherever a 30x points, which would leak the token via an open redirect.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            old_host = urllib.parse.urlsplit(req.full_url).hostname
            new_host = urllib.parse.urlsplit(newurl).hostname
            if new_host != old_host:
                new_req.headers.pop("Authorization", None)
                new_req.unredirected_hdrs.pop("Authorization", None)
        return new_req


_opener = urllib.request.build_opener(_StripAuthOnRedirect)


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


def _headers(url: str) -> Dict[str, str]:
    # Unauthenticated API calls are capped at 60/h per IP; a token raises that
    # to 5000 and costs us one env lookup (FLASH_PLAN §3.2). Only ever sent to
    # the GitHub API itself — never to an asset host (C2).
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if urllib.parse.urlsplit(url).hostname == API_HOST:
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def http_get(url: str, timeout: float = API_TIMEOUT) -> bytes:
    """GET `url`, translating every network failure into a FirmwareError.

    Reads the body in bounded chunks against a hard size cap and an overall
    deadline, instead of one unbounded `response.read()` — the asset URL
    comes straight from release JSON (C2), so a hostile or slow-drip server
    must not be able to grow this without bound or hold the connection open
    forever (docs/AUDIT_ERROR_HANDLING.md M1).
    """
    request = urllib.request.Request(url, headers=_headers(url))
    try:
        with _opener.open(request, timeout=timeout) as response:
            deadline = time.monotonic() + timeout * DOWNLOAD_DEADLINE_MULTIPLIER
            chunks: List[bytes] = []
            total = 0
            while True:
                if time.monotonic() > deadline:
                    raise FirmwareError(
                        f"download from {url} took too long and was aborted"
                    )
                chunk = response.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise FirmwareError(
                        f"response from {url} exceeded the "
                        f"{MAX_DOWNLOAD_BYTES // (1 << 20)} MiB download limit"
                    )
                chunks.append(chunk)
            return b"".join(chunks)
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
    if not isinstance(data, dict):
        # The cache lives under ~/.bombercat/firmware — user-editable,
        # remote-persisted data — so a top-level list/int/etc. is reachable in
        # practice, not just malicious JSON (docs/AUDIT_ERROR_HANDLING.md M2).
        return {}
    out: Dict[str, str] = {}
    for entries in data.values():
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
        _validate_repo(self.repo)
        # Injected in the tests; "no network" and "bad checksum" become two
        # ordinary function calls instead of urllib surgery (FLASH_PLAN §5).
        self._fetch = fetch or http_get
        # Asset names downloaded by the most recent refresh() that GitHub
        # published with no `digest` field, so nothing could be verified
        # (AUDIT_ERROR_HANDLING.md H1). The CLI layer reads this to warn.
        self.unverified_assets: List[str] = []

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
        _validate_path_component(tag, "release tag")

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

        self.unverified_assets = []
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
        _validate_path_component(name, "asset name")
        url = asset.get("browser_download_url")
        if not url:
            raise FirmwareError(f"asset {name} has no download URL")
        _validate_asset_url(url)

        target = staging / name
        target.write_bytes(self._fetch(url, ASSET_TIMEOUT))

        digest = asset.get("digest") or ""
        if not digest.startswith("sha256:"):
            # Releases published before GitHub added `digest` have no checksum
            # to compare against; catnip carries on too, but the caller needs
            # to know so it can warn instead of silently trusting the binary.
            self.unverified_assets.append(name)
            return
        expected = digest.split(":", 1)[1].strip().lower()
        actual = _sha256(target)
        if actual != expected:
            raise FirmwareError(
                f"checksum mismatch for {name}: expected {expected}, got {actual}. "
                "The download is corrupt — retry with --refresh."
            )
