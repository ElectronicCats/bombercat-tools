#!/usr/bin/env python3

# Electronic Cats
# test_cli_flash.py — the release cache behind `bombercat flash`
# (modules/firmware/releases.py) and the `--list` surface on top of it
# (modules/firmware/cli.py), per docs/FLASH_PLAN.md Fase A.
#
# Nothing here touches the network: `ReleaseCache` takes its fetch function as
# a constructor argument, so "GitHub is down" and "the checksum is wrong" are
# ordinary Python calls, and the cache root is always a tmp_path.

import hashlib
import json
import struct
from pathlib import Path

import click
import pytest

from conftest import flat, make_device, make_port
from modules.core.firmwares import all_firmwares
from modules.firmware import cli as fw
from modules.firmware import flasher, releases as releases_mod, uf2
from modules.firmware.cli import complete_firmware, flash, human_size
from modules.firmware.releases import (
    FirmwareError,
    ReleaseCache,
    ReleaseNotFound,
    _headers,
    parse_descriptions,
)
from modules.firmware.uf2 import BootloaderTimeout

# ── Fakes ────────────────────────────────────────────────────────────────────

DEFAULT_IMAGES = {
    "NFCGate.uf2": b"\x00" * 4096,
    "DetectTags.uf2": b"\x01" * 2048,
    "MagspoofCVSAttack.uf2": b"\x02" * 1024,
}


class FakeGitHub:
    """A stand-in for `http_get`: serves one release and its assets.

    `corrupt` advertises a digest that will not match what it hands out, and
    `offline` makes every call fail the way the real one does when there is no
    route to github.com.
    """

    def __init__(
        self,
        tag="v1.2.0",
        images=None,
        descriptions=True,
        digest=True,
        corrupt=False,
        offline=False,
    ):
        self.tag = tag
        self.images = DEFAULT_IMAGES if images is None else images
        self.offline = offline
        self.calls = []

        self.blobs = {}
        assets = []
        for name, blob in self.images.items():
            url = f"https://objects.githubusercontent.com/{tag}/{name}"
            self.blobs[url] = blob
            asset = {"name": name, "browser_download_url": url, "size": len(blob)}
            if digest:
                body = b"tampered" if corrupt else blob
                asset["digest"] = "sha256:" + hashlib.sha256(body).hexdigest()
            assets.append(asset)

        if descriptions:
            payload = json.dumps(
                {
                    "bombercat": [
                        {"filename": name, "description": f"{name} does things."}
                        for name in self.images
                    ]
                }
            ).encode()
            url = f"https://objects.githubusercontent.com/{tag}/descriptions.json"
            self.blobs[url] = payload
            assets.append(
                {
                    "name": "descriptions.json",
                    "browser_download_url": url,
                    "size": len(payload),
                    "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                }
            )

        self.release = {
            "tag_name": tag,
            "published_at": "2026-08-20T10:00:00Z",
            "body": "Release notes.",
            "assets": assets,
        }

    def __call__(self, url, timeout):
        self.calls.append(url)
        if self.offline:
            raise FirmwareError("could not reach GitHub: [Errno -3] Temporary failure")
        if url.endswith("/releases/latest"):
            return json.dumps(self.release).encode()
        return self.blobs[url]

    @property
    def asset_calls(self):
        return [u for u in self.calls if not u.endswith("/releases/latest")]


@pytest.fixture
def cache(tmp_path):
    """A cache rooted in tmp_path; `github` is swapped per test."""

    def _make(**kw):
        github = FakeGitHub(**kw)
        return ReleaseCache(root=tmp_path, repo="fake/firmware", fetch=github), github

    return _make


@pytest.fixture
def use_cache(monkeypatch):
    """Make the `flash` command use the cache the test built."""

    def _use(instance):
        monkeypatch.setattr(fw, "ReleaseCache", lambda *a, **k: instance)
        return instance

    return _use


def _describe(github, descriptions):
    """Rewrite the descriptions.json a FakeGitHub serves (and its digest)."""
    url = f"https://objects.githubusercontent.com/{github.tag}/descriptions.json"
    payload = json.dumps(
        {
            "bombercat": [
                {"filename": name, "description": text}
                for name, text in descriptions.items()
            ]
        }
    ).encode()
    github.blobs[url] = payload
    for asset in github.release["assets"]:
        if asset["name"] == "descriptions.json":
            asset["digest"] = "sha256:" + hashlib.sha256(payload).hexdigest()


def _stale(cache_obj):
    """Backdate the revalidation stamp so the next command re-checks GitHub."""
    index = json.loads(cache_obj.index_path.read_text())
    index["checked"] = "2020-01-01"
    cache_obj.index_path.write_text(json.dumps(index))


# ── The cache: filling it ────────────────────────────────────────────────────


def test_an_empty_cache_has_no_tag_and_no_images(cache):
    c, github = cache()

    assert c.tag is None
    assert c.images() == []
    assert c.is_stale() is True
    assert github.calls == [], "the constructor must never touch the network"


def test_refresh_downloads_the_release_and_its_descriptions(cache):
    c, github = cache()

    assert c.refresh() == "v1.2.0"
    assert c.tag == "v1.2.0"

    names = [i.name for i in c.images()]
    assert names == ["DetectTags.uf2", "MagspoofCVSAttack.uf2", "NFCGate.uf2"]
    assert c.find("NFCGate").description == "NFCGate.uf2 does things."
    assert c.find("NFCGate").size == 4096
    assert (c.dir / "release.json").exists()
    assert c.is_stale() is False


def test_refresh_on_an_up_to_date_cache_downloads_nothing(cache):
    c, github = cache()
    c.refresh()
    before = len(github.asset_calls)

    assert c.refresh() == "v1.2.0"
    assert github.asset_calls[before:] == [], "assets were re-downloaded needlessly"


def test_refresh_force_downloads_the_same_tag_again(cache):
    c, github = cache()
    c.refresh()
    before = len(github.asset_calls)

    c.refresh(force=True)

    assert len(github.asset_calls) > before


def test_a_newer_tag_replaces_the_cached_one(cache, tmp_path):
    c, _ = cache()
    c.refresh()

    newer = FakeGitHub(tag="v1.3.0", images={"NFCGate.uf2": b"\x09" * 512})
    c._fetch = newer

    assert c.refresh() == "v1.3.0"
    assert c.tag == "v1.3.0"
    assert [i.name for i in c.images()] == ["NFCGate.uf2"]
    assert (tmp_path / "v1.2.0").is_dir(), "the old tag stays on disk, unreferenced"


def test_a_stale_stamp_is_what_triggers_the_re_check(cache):
    c, _ = cache()
    c.refresh()
    assert c.is_stale() is False

    _stale(c)

    assert c.is_stale() is True


# ── The cache: when things go wrong ──────────────────────────────────────────


def test_a_bad_checksum_aborts_and_leaves_no_half_cache(cache, tmp_path):
    c, _ = cache(corrupt=True)

    with pytest.raises(FirmwareError) as excinfo:
        c.refresh()

    assert "checksum mismatch" in str(excinfo.value)
    assert c.tag is None
    assert list(tmp_path.glob("*.partial")) == [], "staging directory was left behind"


def test_no_network_raises_instead_of_exiting(cache):
    c, _ = cache(offline=True)

    with pytest.raises(FirmwareError) as excinfo:
        c.refresh()

    assert "could not reach GitHub" in str(excinfo.value)


def test_a_repo_without_releases_says_what_to_do(cache):
    c, github = cache()
    github.release = {}  # a release payload with no tag_name

    with pytest.raises(FirmwareError) as excinfo:
        c.refresh()

    assert "no published release" in str(excinfo.value)
    assert "BOMBERCAT_FIRMWARE_REPO" in str(excinfo.value)


def test_a_404_reads_as_no_release_rather_than_a_bare_url(cache):
    # What `releases/latest` actually answers for a repo that never published
    # one — the state ElectronicCats/bombercat-firmware is in today.
    c, _ = cache()
    c._fetch = lambda url, timeout: (_ for _ in ()).throw(
        ReleaseNotFound(f"not found on GitHub: {url}")
    )

    with pytest.raises(FirmwareError) as excinfo:
        c.refresh()

    assert "no published release" in str(excinfo.value)
    assert "flash_bombercat.sh" in str(excinfo.value)


def test_a_release_with_no_uf2_assets_is_rejected(cache):
    c, github = cache()
    github.release["assets"] = [
        a for a in github.release["assets"] if "uf2" not in a["name"]
    ]

    with pytest.raises(FirmwareError) as excinfo:
        c.refresh()

    assert "no .uf2 assets" in str(excinfo.value)


def test_missing_descriptions_leave_the_images_usable(cache):
    c, _ = cache(descriptions=False)
    c.refresh()

    assert [i.name for i in c.images()] == [
        "DetectTags.uf2",
        "MagspoofCVSAttack.uf2",
        "NFCGate.uf2",
    ]
    assert c.find("NFCGate").description == ""


def test_an_asset_without_a_digest_is_accepted(cache):
    c, _ = cache(digest=False)

    assert c.refresh() == "v1.2.0"
    assert len(c.images()) == 3
    assert set(c.unverified_assets) == set(DEFAULT_IMAGES)


def test_an_asset_with_a_digest_is_not_flagged_unverified(cache):
    c, _ = cache(digest=True)

    c.refresh()

    assert c.unverified_assets == []


# ── AUDIT_ERROR_HANDLING.md H1: no-digest assets warn instead of silently
# skipping integrity verification ──────────────────────────────────────────


def test_list_warns_when_assets_download_without_a_digest(runner, cache, use_cache):
    c, _ = cache(digest=False)
    use_cache(c)

    result = runner.invoke(flash, ["--list"])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "WITHOUT checksum verification" in out
    assert "NFCGate.uf2" in out


def test_list_does_not_warn_when_assets_have_a_digest(runner, cache, use_cache):
    c, _ = cache(digest=True)
    use_cache(c)

    result = runner.invoke(flash, ["--list"])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "checksum verification" not in out


# ── Security: hostile release payloads (AUDIT_ERROR_HANDLING.md C1/C2) ──────


def test_a_path_traversal_tag_name_is_rejected(cache, tmp_path):
    c, github = cache()
    github.release["tag_name"] = "../../evil"

    with pytest.raises(FirmwareError, match="unsafe release tag"):
        c.refresh()

    assert not (tmp_path.parent / "evil").exists()
    assert list(tmp_path.glob("*.partial")) == []


def test_a_path_traversal_asset_name_is_rejected(cache, tmp_path):
    c, github = cache()
    github.release["assets"][0]["name"] = "../../evil.uf2"

    with pytest.raises(FirmwareError, match="unsafe asset name"):
        c.refresh()

    assert not (tmp_path.parent / "evil.uf2").exists()
    assert list(tmp_path.glob("*.partial")) == []


def test_an_asset_from_an_untrusted_host_is_rejected(cache):
    c, github = cache()
    github.release["assets"][0]["browser_download_url"] = "https://evil.example/x.uf2"

    with pytest.raises(FirmwareError, match="untrusted URL"):
        c.refresh()


def test_the_github_token_is_only_sent_to_the_github_api(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "s3cr3t")

    assert "Authorization" in _headers("https://api.github.com/repos/x/releases/latest")
    assert "Authorization" not in _headers(
        "https://objects.githubusercontent.com/x/x.uf2"
    )
    assert "Authorization" not in _headers("https://evil.example/x.uf2")


# ── http_get: bounded reads (M1) ─────────────────────────────────────────────


class _FakeHTTPResponse:
    """Just enough of urllib's response object for http_get to read from."""

    def __init__(self, body: bytes):
        self._body = body
        self._pos = 0

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._body) - self._pos
        chunk = self._body[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_http_get_reads_the_whole_body_in_bounded_chunks(monkeypatch):
    body = b"x" * 200_000
    monkeypatch.setattr(
        releases_mod._opener, "open", lambda request, timeout: _FakeHTTPResponse(body)
    )
    assert releases_mod.http_get("https://api.github.com/x") == body


def test_http_get_rejects_a_response_over_the_size_cap(monkeypatch):
    monkeypatch.setattr(releases_mod, "MAX_DOWNLOAD_BYTES", 10)
    monkeypatch.setattr(
        releases_mod._opener,
        "open",
        lambda request, timeout: _FakeHTTPResponse(b"x" * 1000),
    )

    with pytest.raises(FirmwareError, match="download limit"):
        releases_mod.http_get("https://api.github.com/x")


def test_http_get_aborts_a_stalled_download(monkeypatch):
    # A server that keeps trickling bytes without ever finishing must not hold
    # the process open forever just because each individual read is fast.
    clock = {"t": 0.0}
    monkeypatch.setattr(releases_mod.time, "monotonic", lambda: clock["t"])

    class _StalledResponse(_FakeHTTPResponse):
        def read(self, n=-1):
            clock["t"] += 1000.0
            return b"x"

    monkeypatch.setattr(
        releases_mod._opener,
        "open",
        lambda request, timeout: _StalledResponse(b""),
    )

    with pytest.raises(FirmwareError, match="took too long"):
        releases_mod.http_get("https://api.github.com/x", timeout=1.0)


# ── parse_descriptions: malformed cache data (M2) ────────────────────────────


def test_a_non_object_descriptions_payload_is_ignored_instead_of_crashing():
    assert parse_descriptions(b"[1, 2, 3]") == {}
    assert parse_descriptions(b"42") == {}
    assert parse_descriptions(b'"just a string"') == {}


# ── Name resolution (FLASH_PLAN §3.5) ────────────────────────────────────────


@pytest.mark.parametrize(
    "typed, expected",
    [
        ("NFCGate.uf2", "NFCGate.uf2"),
        ("NFCGate", "NFCGate.uf2"),
        ("nfcgate", "NFCGate.uf2"),
        ("magspoofc", "MagspoofCVSAttack.uf2"),
        ("detect", "DetectTags.uf2"),
    ],
)
def test_find_resolves_what_a_user_would_type(cache, typed, expected):
    c, _ = cache()
    c.refresh()

    assert c.find(typed).name == expected


def test_find_refuses_to_guess_between_several_matches(cache):
    c, _ = cache(
        images={"MagspoofMqtt.uf2": b"a" * 512, "MagspoofCVSAttack.uf2": b"b" * 512}
    )
    c.refresh()

    with pytest.raises(FirmwareError) as excinfo:
        c.find("magspoof")

    assert "matches several" in str(excinfo.value)
    assert "MagspoofMqtt" in str(excinfo.value)
    assert "MagspoofCVSAttack" in str(excinfo.value)


def test_find_prefers_an_exact_stem_over_a_substring(cache):
    c, _ = cache(images={"magspoof.uf2": b"a" * 512, "MagspoofMqtt.uf2": b"b" * 512})
    c.refresh()

    assert c.find("magspoof").name == "magspoof.uf2"


def test_find_returns_nothing_for_an_unknown_name(cache):
    c, _ = cache()
    c.refresh()

    assert c.find("nosuchfirmware") is None


# ── `bombercat flash --list` ─────────────────────────────────────────────────


def test_list_fills_an_empty_cache_and_tabulates_it(runner, cache, use_cache):
    c, github = cache()
    use_cache(c)

    result = runner.invoke(flash, ["--list"])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "Firmware images — v1.2.0" in out
    assert "NFCGate" in out and "DetectTags" in out
    assert "4 KB" in out
    assert "bombercat flash DetectTags" in out
    assert github.asset_calls, "an empty cache should have been filled on demand"


def test_list_does_not_call_github_when_the_cache_is_fresh(runner, cache, use_cache):
    c, github = cache()
    c.refresh()
    use_cache(c)
    before = len(github.calls)

    result = runner.invoke(flash, ["--list"])

    assert result.exit_code == 0
    assert github.calls[before:] == [], "a same-day cache must not hit the network"


def test_list_falls_back_to_the_cache_when_github_is_unreachable(
    runner, cache, use_cache
):
    c, _ = cache()
    c.refresh()
    _stale(c)
    c._fetch = FakeGitHub(offline=True)
    use_cache(c)

    result = runner.invoke(flash, ["--list"])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "could not check GitHub" in out
    assert "NFCGate" in out, "the cached images are still perfectly flashable"


def test_list_with_an_empty_cache_and_no_network_fails_clearly(
    runner, cache, use_cache
):
    c, _ = cache(offline=True)
    use_cache(c)

    result = runner.invoke(flash, ["--list"])

    assert result.exit_code == 1
    assert "could not reach GitHub" in flat(result.output)


def test_list_marks_a_firmware_with_no_description(runner, cache, use_cache):
    c, _ = cache(descriptions=False, images={"NFCGate.uf2": b"x" * 1024})
    use_cache(c)

    result = runner.invoke(flash, ["--list"])

    assert result.exit_code == 0
    assert "—" in result.output


def test_full_shows_the_whole_description(runner, cache, use_cache):
    long = "A very long description. " * 8
    c, github = cache(images={"NFCGate.uf2": b"x" * 1024})
    _describe(github, {"NFCGate.uf2": long})
    use_cache(c)

    truncated = runner.invoke(flash, ["--list"])
    full = runner.invoke(flash, ["--list", "--full"])

    assert "…" in truncated.output, "a long description should be cut to the column"
    assert "…" not in full.output
    assert full.output.count("\n") > truncated.output.count("\n")


def test_refresh_forces_a_re_download(runner, cache, use_cache):
    c, github = cache()
    c.refresh()
    use_cache(c)
    before = len(github.asset_calls)

    result = runner.invoke(flash, ["--refresh", "--list"])

    assert result.exit_code == 0
    assert len(github.asset_calls) > before


# ── Helpers ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "size, expected",
    [(512, "512 B"), (1024, "1 KB"), (421888, "412 KB"), (2 * 1024 * 1024, "2.0 MB")],
)
def test_human_size(size, expected):
    assert human_size(size) == expected


# ═════════════════════════════════════════════════════════════════════════════
# Fase B — the UF2 format, the bootloader drive and the flash sequence
# ═════════════════════════════════════════════════════════════════════════════


def make_uf2(path, blocks=2, family=uf2.RP2040_FAMILY_ID, flags=None):
    """Write a syntactically valid UF2 image at `path` and return it."""
    if flags is None:
        flags = uf2.FLAG_FAMILY_ID_PRESENT
    raw = bytearray()
    for index in range(blocks):
        block = bytearray(uf2.BLOCK_SIZE)
        struct.pack_into(
            "<8I",
            block,
            0,
            uf2.MAGIC_START0,
            uf2.MAGIC_START1,
            flags,
            0x10000000 + index * 256,
            256,
            index,
            blocks,
            family,
        )
        struct.pack_into("<I", block, uf2.BLOCK_SIZE - 4, uf2.MAGIC_END)
        raw += block
    path.write_bytes(bytes(raw))
    return path


def make_drive(tmp_path, name=uf2.DRIVE_LABEL):
    """A directory that looks like a mounted UF2 bootloader."""
    drive = tmp_path / name
    drive.mkdir()
    (drive / uf2.INFO_FILE).write_text(
        "UF2 Bootloader v3.0\nModel: Raspberry Pi RP2\nBoard-ID: RPI-RP2\n"
    )
    return drive


class FakeTouchSerial:
    """Records what the 1200-bps touch did to the port."""

    opened = []

    def __init__(self):
        self.port = None
        self.baudrate = None
        self.dtr = True
        self.closed = False

    def open(self):
        FakeTouchSerial.opened.append((self.port, self.baudrate))

    def close(self):
        self.closed = True


@pytest.fixture
def touch(monkeypatch):
    """Replace `serial.Serial` so the 1200-bps touch hits nothing physical."""
    FakeTouchSerial.opened = []
    monkeypatch.setattr(flasher.serial, "Serial", FakeTouchSerial)
    return FakeTouchSerial


@pytest.fixture
def ports(monkeypatch):
    """Script what the USB layer reports to the flasher, call after call."""

    def _set(*snapshots):
        queue = [list(s) for s in snapshots]

        def _list(include_all=False):
            current = queue[0] if len(queue) == 1 else queue.pop(0)
            return [make_port(device=name) for name in current]

        monkeypatch.setattr(flasher, "list_ports_info", _list)

    return _set


# ── validate_uf2 (§3.3) ──────────────────────────────────────────────────────


def test_a_well_formed_rp2040_image_validates(tmp_path):
    uf2.validate_uf2(make_uf2(tmp_path / "NFCGate.uf2"))


def test_an_image_whose_size_is_not_a_multiple_of_512_is_rejected(tmp_path):
    image = tmp_path / "truncated.uf2"
    image.write_bytes(b"\x00" * 600)

    with pytest.raises(FirmwareError) as excinfo:
        uf2.validate_uf2(image)

    assert "multiple of the 512-byte UF2 block" in str(excinfo.value)


def test_a_file_without_uf2_magic_is_rejected(tmp_path):
    image = tmp_path / "notreally.uf2"
    image.write_bytes(b"\x00" * 512)

    with pytest.raises(FirmwareError) as excinfo:
        uf2.validate_uf2(image)

    assert "no UF2 magic" in str(excinfo.value)


def test_an_image_for_another_chip_family_is_rejected(tmp_path):
    # 0x1C5F21B0 is the ESP32-S2 family: right container, wrong silicon.
    image = make_uf2(tmp_path / "esp32.uf2", family=0x1C5F21B0)

    with pytest.raises(FirmwareError) as excinfo:
        uf2.validate_uf2(image)

    assert "not the RP2040" in str(excinfo.value)


def test_an_image_that_declares_no_family_is_accepted(tmp_path):
    # Without the flag the family word is meaningless, so it must not be read.
    uf2.validate_uf2(make_uf2(tmp_path / "old.uf2", family=0xDEADBEEF, flags=0))


# ── validate_uf2 walks every block, not just the first (M6) ─────────────────


def test_a_corrupted_middle_block_is_rejected(tmp_path):
    image = make_uf2(tmp_path / "NFCGate.uf2", blocks=4)
    raw = bytearray(image.read_bytes())
    # Stomp block 2's magic while leaving block 0 (and the file size) intact.
    raw[2 * uf2.BLOCK_SIZE : 2 * uf2.BLOCK_SIZE + 4] = b"\x00\x00\x00\x00"
    image.write_bytes(bytes(raw))

    with pytest.raises(FirmwareError, match="block 2 has no UF2 magic"):
        uf2.validate_uf2(image)


def test_a_truncated_but_block_aligned_image_is_rejected(tmp_path):
    # Chop the last block off a well-formed 4-block image: still a multiple of
    # 512 bytes, but block 0 still claims 4 total blocks.
    image = make_uf2(tmp_path / "NFCGate.uf2", blocks=4)
    image.write_bytes(image.read_bytes()[: 3 * uf2.BLOCK_SIZE])

    with pytest.raises(FirmwareError, match="claims 4 total blocks"):
        uf2.validate_uf2(image)


def test_out_of_order_blocks_are_rejected(tmp_path):
    image = make_uf2(tmp_path / "NFCGate.uf2", blocks=2)
    raw = bytearray(image.read_bytes())
    # Swap the two blocks wholesale, so block 0 on disk claims blockNo=1.
    raw[: uf2.BLOCK_SIZE], raw[uf2.BLOCK_SIZE :] = (
        bytes(raw[uf2.BLOCK_SIZE :]),
        bytes(raw[: uf2.BLOCK_SIZE]),
    )
    image.write_bytes(bytes(raw))

    with pytest.raises(FirmwareError, match="out of order"):
        uf2.validate_uf2(image)


# ── Finding the bootloader drive (§3.3) ──────────────────────────────────────


@pytest.fixture
def mounts(monkeypatch):
    """Pretend the OS has exactly these mount points."""

    def _set(*paths):
        monkeypatch.setattr(uf2, "_candidates", lambda: list(paths))

    return _set


def test_the_drive_is_the_mount_that_carries_info_uf2(tmp_path, mounts):
    drive = make_drive(tmp_path)
    boring = tmp_path / "boot-efi"
    boring.mkdir()
    mounts(boring, drive)

    assert uf2.find_uf2_drive() == drive


def test_no_drive_when_nothing_is_in_bootloader(tmp_path, mounts):
    boring = tmp_path / "boot-efi"
    boring.mkdir()
    mounts(boring)

    assert uf2.find_uf2_drive() is None


def test_an_unlabelled_uf2_drive_is_not_picked_as_a_fallback(tmp_path, mounts):
    # No RPI-RP2 anywhere, only a differently-named UF2 board (M5): must not
    # silently become the flash target — same as no board being in BOOTSEL.
    other = make_drive(tmp_path, name="CATSNIFFER")
    mounts(other)

    assert uf2.find_uf2_drive() is None


def test_a_second_uf2_board_does_not_steal_the_flash(tmp_path, mounts):
    other = make_drive(tmp_path, name="CATSNIFFER")
    ours = make_drive(tmp_path)
    mounts(other, ours)

    assert uf2.find_uf2_drive() == ours


def test_waiting_for_a_drive_that_never_comes_raises(monkeypatch, mounts):
    mounts()
    monkeypatch.setattr(uf2.time, "sleep", lambda seconds: None)

    with pytest.raises(BootloaderTimeout) as excinfo:
        uf2.wait_for_uf2_drive(timeout=0.01)

    assert "RPI-RP2" in str(excinfo.value)


# ── Copying (§3.4) ───────────────────────────────────────────────────────────


def test_copy_puts_the_image_on_the_drive(tmp_path):
    image = make_uf2(tmp_path / "NFCGate.uf2", blocks=4)
    drive = make_drive(tmp_path)

    flasher.copy_uf2(image, drive)

    assert (drive / "NFCGate.uf2").read_bytes() == image.read_bytes()


def test_the_board_rebooting_mid_copy_is_success_not_failure(tmp_path, monkeypatch):
    # The classic trap: the bootloader restarts the board as soon as it has the
    # last block, so the write that hands over the final bytes — or the flush,
    # or the close — can fail with the drive already gone.
    image = make_uf2(tmp_path / "NFCGate.uf2", blocks=4)
    drive = make_drive(tmp_path)
    real_open = Path.open

    class VanishingFile:
        def __init__(self, fh):
            self.fh = fh

        def write(self, data):
            return self.fh.write(data)

        def flush(self):
            raise OSError(5, "Input/output error")

        def fileno(self):
            return self.fh.fileno()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()
            return False

    def _open(self, *a, **kw):
        handle = real_open(self, *a, **kw)
        return VanishingFile(handle) if self.parent == drive else handle

    monkeypatch.setattr(Path, "open", _open)

    flasher.copy_uf2(image, drive)  # must not raise


def test_a_copy_that_stops_short_is_a_real_failure(tmp_path, monkeypatch):
    image = make_uf2(tmp_path / "NFCGate.uf2", blocks=4)
    drive = make_drive(tmp_path)
    monkeypatch.setattr(flasher, "COPY_CHUNK", 512)
    real_open = Path.open

    class DyingFile:
        def __init__(self, fh):
            self.fh = fh
            self.writes = 0

        def write(self, data):
            self.writes += 1
            if self.writes > 1:
                raise OSError(5, "Input/output error")
            return self.fh.write(data)

        def flush(self):
            self.fh.flush()

        def fileno(self):
            return self.fh.fileno()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()
            return False

    def _open(self, *a, **kw):
        handle = real_open(self, *a, **kw)
        return DyingFile(handle) if self.parent == drive else handle

    monkeypatch.setattr(Path, "open", _open)

    with pytest.raises(FirmwareError) as excinfo:
        flasher.copy_uf2(image, drive)

    assert "failed after" in str(excinfo.value)


def test_a_silent_short_write_is_a_real_failure(tmp_path, monkeypatch):
    # M4: a write() that returns fewer bytes than asked *without raising* must
    # not be counted as if the whole chunk went through.
    image = make_uf2(tmp_path / "NFCGate.uf2", blocks=4)
    drive = make_drive(tmp_path)
    monkeypatch.setattr(flasher, "COPY_CHUNK", 512)
    real_open = Path.open

    class ShortWriteFile:
        def __init__(self, fh):
            self.fh = fh

        def write(self, data):
            self.fh.write(data[:1])  # silently swallow the rest
            return 1

        def flush(self):
            self.fh.flush()

        def fileno(self):
            return self.fh.fileno()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()
            return False

    def _open(self, *a, **kw):
        handle = real_open(self, *a, **kw)
        return ShortWriteFile(handle) if self.parent == drive else handle

    monkeypatch.setattr(Path, "open", _open)

    with pytest.raises(FirmwareError, match="short write"):
        flasher.copy_uf2(image, drive)


# ── The board coming back: prefer a BomberCat USB id (M3) ───────────────────


def test_a_bystander_usb_device_is_not_mistaken_for_the_board(monkeypatch):
    # An unrelated USB-CDC device (a phone, a second board) can appear during
    # the flash window; a real BomberCat port among several new ones must win.
    monkeypatch.setattr(flasher.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        flasher,
        "list_ports_info",
        lambda include_all=False: [
            make_port(device="/dev/ttyACM0"),  # was already known
            make_port(device="/dev/ttyUSB9", vid=0x05AC, pid=0x12A8),  # bystander
            make_port(device="/dev/ttyACM1"),  # the board, back from the flash
        ],
    )

    back = flasher.wait_for_new_port({"/dev/ttyACM0"}, timeout=1.0)

    assert back == "/dev/ttyACM1"


def test_falls_back_to_an_unmatched_port_with_a_warning(monkeypatch):
    # Nothing new matches a known BomberCat id: still report it (better than
    # timing out) but say so.
    monkeypatch.setattr(flasher.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        flasher,
        "list_ports_info",
        lambda include_all=False: [
            make_port(device="/dev/ttyUSB9", vid=0x05AC, pid=0x12A8),
        ],
    )
    warnings = []

    back = flasher.wait_for_new_port(set(), timeout=1.0, say=warnings.append)

    assert back == "/dev/ttyUSB9"
    assert any("does not match" in w for w in warnings)


# ── The sequence (§3.4) ──────────────────────────────────────────────────────


def test_an_already_mounted_drive_skips_the_reset(tmp_path, monkeypatch, touch, ports):
    image = make_uf2(tmp_path / "NFCGate.uf2")
    drive = make_drive(tmp_path)
    monkeypatch.setattr(flasher, "find_uf2_drive", lambda: drive)
    ports(["/dev/ttyACM0"])

    outcome = flasher.flash(image, port=None, port_timeout=0.01)

    assert outcome.touched is False
    assert touch.opened == [], "the board was already in BOOTSEL; do not touch it"
    assert (drive / "NFCGate.uf2").exists()


def test_a_running_board_is_rebooted_at_1200_bps(tmp_path, monkeypatch, touch, ports):
    image = make_uf2(tmp_path / "NFCGate.uf2")
    drive = make_drive(tmp_path)
    monkeypatch.setattr(flasher, "find_uf2_drive", lambda: None)
    monkeypatch.setattr(flasher, "wait_for_uf2_drive", lambda timeout: drive)
    monkeypatch.setattr(flasher.time, "sleep", lambda seconds: None)
    # (1) gone after the touch, (2) still gone while in BOOTSEL, (3) back
    ports([], [], ["/dev/ttyACM1"])

    outcome = flasher.flash(image, port="/dev/ttyACM0", port_timeout=2.0)

    assert touch.opened == [("/dev/ttyACM0", 1200)]
    assert outcome.touched is True
    assert outcome.port == "/dev/ttyACM1"


def test_a_bad_image_is_rejected_before_anything_is_rebooted(tmp_path, touch):
    image = tmp_path / "junk.uf2"
    image.write_bytes(b"\x00" * 512)

    with pytest.raises(FirmwareError):
        flasher.flash(image, port="/dev/ttyACM0")

    assert touch.opened == [], "a board must not be left in BOOTSEL over a bad image"


def test_a_board_that_never_comes_back_is_reported(tmp_path, monkeypatch, touch, ports):
    image = make_uf2(tmp_path / "NFCGate.uf2")
    drive = make_drive(tmp_path)
    monkeypatch.setattr(flasher, "find_uf2_drive", lambda: drive)
    monkeypatch.setattr(flasher.time, "sleep", lambda seconds: None)
    ports([])

    outcome = flasher.flash(image, port=None, port_timeout=0.01)

    assert outcome.port is None


# ── `bombercat flash <FIRMWARE>` ─────────────────────────────────────────────


@pytest.fixture
def bench(monkeypatch, tmp_path):
    """Stub the CLI's view of the bench: boards attached, drive mounted, write.

    Every one of these is a real-hardware call, so a test that forgets one
    would reach for whatever is plugged into the machine running the suite.
    """

    calls = []

    def _set(devices=(), drive=None, outcome=None, error=None):
        monkeypatch.setattr(fw, "find_devices", lambda *a, **k: list(devices))
        monkeypatch.setattr(
            fw,
            "find_device",
            lambda device_id=None: next(
                (d for d in devices if d.device_id == device_id), None
            ),
        )
        monkeypatch.setattr(fw, "describe_devices", lambda *a, **k: "#1 /dev/ttyACM0")
        monkeypatch.setattr(fw, "find_uf2_drive", lambda: drive)

        def _write(image, port=None, progress=None):
            calls.append((image, port))
            if error is not None:
                raise error
            if progress:
                progress("copying")
            return outcome or flasher.FlashOutcome(
                image=image, drive=drive or tmp_path, touched=True, port="/dev/ttyACM0"
            )

        monkeypatch.setattr(fw, "write_image", _write)
        return calls

    return _set


def test_flashing_asks_before_writing_anything(runner, cache, use_cache, bench):
    c, _ = cache()
    use_cache(c)
    calls = bench(devices=[make_device(1, "/dev/ttyACM0")])

    result = runner.invoke(flash, ["NFCGate"], input="n\n")
    out = flat(result.output)

    assert result.exit_code == 0
    assert "About to flash NFCGate.uf2" in out
    assert "Write it to /dev/ttyACM0?" in out
    assert "Nothing was written" in out
    assert calls == [], "answering no must not write"


def test_yes_skips_the_prompt_and_flashes(runner, cache, use_cache, bench):
    c, _ = cache()
    use_cache(c)
    calls = bench(devices=[make_device(1, "/dev/ttyACM0")])

    result = runner.invoke(flash, ["NFCGate", "-y"])
    out = flat(result.output)

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0][0].name == "NFCGate.uf2"
    assert calls[0][1] == "/dev/ttyACM0"
    assert "NFCGate.uf2 written to" in out
    assert "came back on /dev/ttyACM0" in out


def test_a_local_uf2_path_never_touches_the_network(
    runner, cache, use_cache, bench, tmp_path
):
    c, github = cache(offline=True)
    use_cache(c)
    bench(devices=[make_device(1, "/dev/ttyACM0")])
    image = make_uf2(tmp_path / "mine.uf2")

    result = runner.invoke(flash, [str(image), "-y"])

    assert result.exit_code == 0, result.output
    assert github.calls == [], "a local path must not consult the release cache"


def test_an_unknown_name_lists_what_there_is(runner, cache, use_cache, bench):
    c, _ = cache()
    use_cache(c)
    bench(devices=[make_device(1, "/dev/ttyACM0")])

    result = runner.invoke(flash, ["nfcgat3", "-y"])
    out = flat(result.output)

    assert result.exit_code == 1
    assert "no firmware named 'nfcgat3'" in out
    assert "NFCGate" in out


def test_port_and_device_are_mutually_exclusive(runner, cache, use_cache, bench):
    c, _ = cache()
    use_cache(c)
    bench(devices=[make_device(1, "/dev/ttyACM0")])

    result = runner.invoke(flash, ["NFCGate", "-p", "/dev/ttyACM0", "-d", "1", "-y"])

    assert result.exit_code == 1
    assert "mutually exclusive" in flat(result.output)


def test_several_boards_ask_for_a_device_id(runner, cache, use_cache, bench):
    c, _ = cache()
    use_cache(c)
    bench(devices=[make_device(1, "/dev/ttyACM0"), make_device(2, "/dev/ttyACM1")])

    result = runner.invoke(flash, ["NFCGate", "-y"])
    out = flat(result.output)

    assert result.exit_code == 1
    assert "multiple BomberCats found" in out
    assert "-d/--device" in out


def test_no_board_and_no_drive_says_what_to_do(runner, cache, use_cache, bench):
    c, _ = cache()
    use_cache(c)
    bench(devices=[], drive=None)

    result = runner.invoke(flash, ["NFCGate", "-y"])
    out = flat(result.output)

    assert result.exit_code == 1
    assert "no BomberCat found" in out
    assert "double-tap RESET" in out


def test_a_board_in_bootsel_needs_no_serial_port(
    runner, cache, use_cache, bench, tmp_path
):
    # In BOOTSEL there IS no serial port — flashing must still work, and this
    # is the escape hatch for a board whose firmware ignores the 1200-bps touch.
    c, _ = cache()
    use_cache(c)
    drive = make_drive(tmp_path)
    calls = bench(devices=[], drive=drive)

    result = runner.invoke(flash, ["NFCGate"], input="y\n")
    out = flat(result.output)

    assert result.exit_code == 0, result.output
    assert "Write it to the RPI-RP2 drive?" in out
    assert calls[0][1] is None


def test_a_board_that_will_not_enter_bootloader_gets_instructions(
    runner, cache, use_cache, bench
):
    c, _ = cache()
    use_cache(c)
    bench(
        devices=[make_device(1, "/dev/ttyACM0")],
        error=BootloaderTimeout("no RPI-RP2 drive appeared within 15 s."),
    )

    result = runner.invoke(flash, ["NFCGate", "-y"])
    out = flat(result.output)

    assert result.exit_code == 1
    assert "did not enter bootloader mode" in out
    assert "Double-tap the RESET button" in out
    assert "bombercat flash NFCGate" in out


def test_an_unanticipated_write_error_is_one_line_and_hides_the_traceback(
    runner, cache, use_cache, bench
):
    c, _ = cache()
    use_cache(c)
    bench(devices=[make_device(1, "/dev/ttyACM0")], error=OSError("disk full"))

    result = runner.invoke(flash, ["NFCGate", "-y"])
    out = flat(result.output)

    assert result.exit_code == 1
    assert "OSError: disk full" in out
    assert "BOMBERCAT_DEBUG=1" in out


def test_an_unanticipated_write_error_re_raises_under_bombercat_debug(
    runner, cache, use_cache, bench, monkeypatch
):
    c, _ = cache()
    use_cache(c)
    bench(devices=[make_device(1, "/dev/ttyACM0")], error=OSError("disk full"))
    monkeypatch.setenv("BOMBERCAT_DEBUG", "1")

    result = runner.invoke(flash, ["NFCGate", "-y"])

    assert isinstance(result.exception, OSError)


def test_a_board_that_does_not_re_enumerate_is_flagged(
    runner, cache, use_cache, bench, tmp_path
):
    c, _ = cache()
    use_cache(c)
    bench(
        devices=[make_device(1, "/dev/ttyACM0")],
        outcome=flasher.FlashOutcome(
            image=tmp_path / "NFCGate.uf2", drive=tmp_path, touched=True, port=None
        ),
    )

    result = runner.invoke(flash, ["NFCGate", "-y"])
    out = flat(result.output)

    assert result.exit_code == 0
    assert "did not re-enumerate" in out
    assert "bombercat device list" in out


# ── Tab completion (FLASH_PLAN Fase D) ───────────────────────────────────────
# `complete_firmware` runs on every <TAB>, so the contract these tests pin down
# is as much about what it must NOT do — no network, no exception — as about
# the names it offers.


def _values(items):
    return [item.value for item in items]


def _typed(items):
    return [(item.value, item.type) for item in items]


def test_completion_offers_the_names_in_the_cached_release(cache, use_cache):
    c, github = cache()
    c.refresh()
    use_cache(c)
    before = len(github.calls)

    items = complete_firmware(None, None, "")

    assert _values(items) == ["DetectTags", "MagspoofCVSAttack", "NFCGate"]
    assert items[0].help == "DetectTags.uf2 does things."
    assert github.calls[before:] == [], "completion must never touch the network"


def test_completion_matches_a_substring_case_insensitively(cache, use_cache):
    c, _ = cache()
    c.refresh()
    use_cache(c)

    # The same resolution `find()` does (§3.5), so what completes is flashable.
    assert _values(complete_firmware(None, None, "magspoofc")) == ["MagspoofCVSAttack"]
    assert _values(complete_firmware(None, None, "NFC")) == ["NFCGate"]


def test_completion_falls_back_to_the_registry_when_the_cache_is_empty(
    cache, use_cache
):
    c, github = cache()
    use_cache(c)

    values = _values(complete_firmware(None, None, ""))

    # Nothing downloaded yet is the normal state of a fresh install; the nine
    # names are still known statically.
    assert values == [f.display for f in all_firmwares()]
    assert "NFCGate" in values
    assert github.calls == [], "completion must never touch the network"


def test_completion_hands_paths_back_to_the_shell(cache, use_cache):
    c, _ = cache()
    c.refresh()
    use_cache(c)

    for typed in ("./build/", "/tmp/nfc", "~/images/"):
        assert _typed(complete_firmware(None, None, typed)) == [(typed, "file")]


def test_completion_offers_files_when_nothing_matches(cache, use_cache):
    c, _ = cache()
    c.refresh()
    use_cache(c)

    # `bombercat flash mine.uf2` with the image in the current directory.
    assert _typed(complete_firmware(None, None, "mine.uf")) == [("mine.uf", "file")]


def test_completion_help_is_a_single_clipped_line(cache, use_cache):
    c, github = cache()
    _describe(github, {"NFCGate.uf2": "Relay endpoint.\n\n" + "verbose " * 40})
    c.refresh()
    use_cache(c)

    (item,) = complete_firmware(None, None, "nfcgate")

    # click joins the completion fields with newlines; a paragraph would
    # corrupt the response the shell parses.
    assert "\n" not in item.help
    assert len(item.help) <= fw.COMPLETION_HELP_WIDTH
    assert item.help.startswith("Relay endpoint.") and item.help.endswith("\u2026")


def test_completion_of_an_image_without_a_description_has_no_help(cache, use_cache):
    c, github = cache()
    _describe(github, {})
    c.refresh()
    use_cache(c)

    assert complete_firmware(None, None, "nfcgate")[0].help is None


def test_a_broken_cache_does_not_break_completion(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no home directory")

    monkeypatch.setattr(fw, "ReleaseCache", boom)

    assert complete_firmware(None, None, "nfc") == []


def test_the_firmware_argument_is_wired_to_the_completer(cache, use_cache):
    c, _ = cache()
    c.refresh()
    use_cache(c)
    argument = next(p for p in flash.params if p.name == "firmware")

    items = argument.shell_complete(click.Context(flash), "nfc")

    assert _values(items) == ["NFCGate"]
