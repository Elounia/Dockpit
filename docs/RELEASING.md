# Releasing Dockpit

The repository prepares release artifacts; adding these files does not publish a release.

1. Run unit, wheel installation, browser and Docker smoke checks described in CONTRIBUTING.md. Review CI on the exact release commit.
2. Review the full Git history with a secret scanner. Inspect screenshots and example logs for private infrastructure details. Remove/rotate any real credentials before publishing repository history.
3. Enable private vulnerability reporting in GitHub settings, verify repository/license/maintainer details, and require CI checks on pull requests to main. Repository settings are not configured by files in this checkout.
4. Keep `pyproject.toml`, `dockpit/__init__.py` and the UI version consistent. Move Unreleased entries into the chosen version's changelog section. Use a prerelease version/tag if shipping a beta.
5. Push a matching version tag, e.g. `v1.1.0` only for package version `1.1.0`. The release workflow validates it and publishes amd64/arm64 images to GHCR using the repository's Actions token.
6. Verify package visibility and pulls on both architectures, and create GitHub release notes describing the actual tested platforms and limitations. Check that source-build quickstart works before advertising published images.

No PyPI upload is configured. Python wheels are built and installation-tested in CI. CI builds and tests Docker on native amd64 and arm64 runners. After publication, the release workflow pulls and smoke-tests the published image on both architectures.
