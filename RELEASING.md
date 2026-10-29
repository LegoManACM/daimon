# Releasing

Daimon is installed from GitHub releases only (not PyPI). The four packages share one version.

1. Bump `version` in all four `pyproject.toml` files (root, `canvas/`, `messaging/`, `calendars/`) to the same `X.Y.Z`, and commit.
2. Tag and push the tag:

       git tag vX.Y.Z
       git push origin vX.Y.Z

3. `.github/workflows/release.yml` takes it from there. It fails, without publishing anything, if any of the three
   versions isn't `X.Y.Z` or a test fails. Otherwise it creates the `vX.Y.Z` release with generated notes and:

   - `daimon-X.Y.Z-py3-none-any.whl`, `daimon_canvas-X.Y.Z-py3-none-any.whl`, `daimon_messages-X.Y.Z-py3-none-any.whl`, `daimon_calendar-X.Y.Z-py3-none-any.whl`
   - `daimon-messages-sidecar-{windows,darwin,linux}-{amd64,arm64}` (`.exe` on Windows): the Google Messages helper,
     which `daimon-messages` downloads on first use for its own version
   - `install.ps1` and `install.sh`
   - `SHA256SUMS` for all of the above

   A tag with a hyphen (`v1.0.0-rc1`) is published as a pre-release, so `releases/latest` and the installers skip it.

Wheels and the helper are pinned to the release they came from: daimon-messages X.Y.Z downloads the helper from
`vX.Y.Z`, so never delete or re-tag a published release. If one is broken, release a new patch version.

To try a release's installer before it's the latest, set `DAIMON_VERSION=X.Y.Z` (`$env:DAIMON_VERSION` on Windows).
`DAIMON_DRY_RUN=1` prints the installer's commands without running them, and `DAIMON_NO_SETUP=1` skips `daimon setup`.
