# Cutting a release

Every push to `main` already builds and uploads all four packages as CI
artifacts (30-day retention) — see
[`docs/packaging.md`](packaging.md#building-the-packages-locally). A
**release** just also attaches them to a GitHub Release, which happens
automatically once the tag is pushed and the release is created.

## Procedure

1. **Green baseline.**

   ```sh
   pre-commit run --all-files
   pytest
   ```

2. **Bump `VERSION`.** It's the single source of truth — `DEBIAN/control`'s
   `Version:`, the Arch `PKGBUILD`'s `pkgver`, and the Inno Setup `.iss`'s
   `AppVersion` are all filled in from it at build time (`@VERSION@`
   substitution), nothing to edit by hand.

   ```sh
   echo -n "1.2.0.1" > VERSION
   ```

3. **Commit and tag.**

   ```sh
   git add VERSION
   git commit -m "chore(release): v1.2.0.1"
   git tag v1.2.0.1
   git push origin main --tags
   ```

   `VERSION`'s 4-component form (`1.2.0.1`) and the git tag's semver-ish form
   (`v1.2.0.1`) are two different strings by convention — don't try to make
   them byte-identical, just keep the numeric parts in sync.

4. **Create the GitHub Release.** This is what triggers the four
   `build-*.yml` workflows (`on: release: types: [created]`) to build and
   attach their artifacts:

   ```sh
   gh release create v1.2.0.1 --generate-notes
   ```

5. **Verify the 5 assets land on the release**, once the workflows finish
   (`gh run list` / the Actions tab):

   - `bombercat-1.2.0.1.deb`
   - `bombercat-1.2.0.1.pkg.tar.zst`
   - `bombercat-1.2.0.1-x86_64.pkg`
   - `bombercat-1.2.0.1-arm64.pkg`
   - `bombercat-1.2.0.1.exe`

   Each `build-deb.yml`/`build-arch.yml` run also gates on its own
   `verify-install` job (installs the artifact in a clean `debian:12` /
   `archlinux:latest` container and runs `bombercat --version`/`--help`/
   `flash --help`) — a red run there means the asset didn't even install
   cleanly, not just that a lint check complained.

6. **Re-run a single failed build**, if one of the five didn't make it,
   without re-tagging or re-releasing:

   ```sh
   gh workflow run build-mac.yml -f release_tag=v1.2.0.1
   ```

   `workflow_dispatch` re-runs that one workflow against the existing
   release tag and re-uploads just its asset(s).

## Maintenance checklist (do this at every release, not just when something breaks)

- **Pin versions, don't float `@latest`.** `actions/checkout`, `actions/upload-artifact`,
  `actions/download-artifact`, `actions/setup-python` and
  `softprops/action-gh-release` are all pinned to a major version in the
  workflows (`@v4`/`@v5`/`@v2`) — bump them deliberately, and specifically
  keep `softprops/action-gh-release` on **v2** (catnip's `v1` runs on an
  end-of-life Node runtime — see
  [`PACKAGING_PLAN.md` §1.8](PACKAGING_PLAN.md#18-deuda-técnica-observada-no-replicar)).
- **`macos-15-intel`** is a GitHub-announced-for-retirement runner. If it
  disappears, `build-mac.yml`'s matrix loses its `x86_64` leg — the fallback
  is a `universal2` PyInstaller build on `arm64` instead of two separate
  `.pkg`s.
- **Never regenerate the Inno Setup `AppId`**
  (`{B2123304-531C-477C-B378-46EE726970B3}` in
  [`packaging/windows/bombercat_installer.iss`](../packaging/windows/bombercat_installer.iss)).
  A new GUID makes Windows treat the next release as an unrelated product —
  existing installs won't upgrade, they'll sit alongside the new one.
- **`.pkg`/`.exe` stay unsigned** (no Apple Developer ID, no Authenticode
  cert) — Gatekeeper/SmartScreen warnings on first run are expected, not a
  regression. Documented in [`docs/packaging.md`](packaging.md).
