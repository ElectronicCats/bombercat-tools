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

import pytest

from conftest import flat
from modules.firmware import cli as fw
from modules.firmware.cli import flash, human_size
from modules.firmware.releases import FirmwareError, ReleaseCache, ReleaseNotFound

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
            url = f"https://example.invalid/{tag}/{name}"
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
            url = f"https://example.invalid/{tag}/descriptions.json"
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
    url = f"https://example.invalid/{github.tag}/descriptions.json"
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


# ── Fase B is not here yet ───────────────────────────────────────────────────


def test_flashing_an_image_says_it_is_not_implemented(runner, cache, use_cache):
    c, _ = cache()
    use_cache(c)

    result = runner.invoke(flash, ["NFCGate"])
    out = flat(result.output)

    assert result.exit_code == 1
    assert "not implemented yet" in out
    assert "flash_bombercat.sh" in out


# ── Helpers ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "size, expected",
    [(512, "512 B"), (1024, "1 KB"), (421888, "412 KB"), (2 * 1024 * 1024, "2.0 MB")],
)
def test_human_size(size, expected):
    assert human_size(size) == expected
