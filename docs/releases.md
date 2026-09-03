# Releasing RBS

`CHANGELOG.md` is the release-note source of truth. During development, add one
user-focused bullet to `Unreleased` in the same change that introduces notable
behavior. Do not add entries for tests, refactors, or dependency updates unless
they affect users or operators.

Use the standard headings `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`,
and `Security`, omitting headings that have no entries. A surface-specific entry
starts with **Desktop**, **Cloud**, **CLI**, or **Data compatibility**. Always call
out changes to `.rbsc` files, settings, database migrations, the solver protocol,
CLI behavior, or hosted environment variables, including whether existing data
is migrated and whether older builds can still read it. Publish security details
only when the corresponding fix is available.

## Prepare a release

Choose an increasing numeric `X.Y.Z` version. RBS does not currently attach
Semantic Versioning compatibility promises to those numbers.

From the release-ready `v0` branch, run:

```bash
uv run python tools/release_changelog.py X.Y.Z --date YYYY-MM-DD
uv lock
uv run ruff check .
uv run pytest
uv build
tools/build_desktop.sh --skip-sync
```

The release helper performs only two edits: it updates the single application
version and moves the current `Unreleased` notes beneath a dated release heading.
It also recreates an empty `Unreleased` section and maintains GitHub comparison
links. It validates every input before writing and refuses empty, duplicate, or
out-of-order releases.

Continuous integration repeats the lock, lint, and test gates, so running them
locally is about finding problems before the tag rather than about proving the
release. The `uv build` and `tools/build_desktop.sh` steps are the local smoke
check that the artifacts still assemble.

Review and commit the version, changelog, and refreshed lockfile together. Merge
the release-ready `v0` line into `main`, then create an annotated `vX.Y.Z` tag on
the resulting `main` commit and push it:

```bash
git tag -a vX.Y.Z -m "RBS X.Y.Z"
git push origin main --follow-tags
```

The first release is `0.1.0`. Until it is actually prepared, its notes remain in
`Unreleased`; do not add a date or tag in advance.

## What CI does

`.github/workflows/ci.yml` runs `uv lock --check`, Ruff, and the test suite on
macOS and Linux for every pull request and every push to `main`. macOS is the
platform RBS Desktop ships on; the Linux leg covers the CLI and the hosted
build.

Pushing a `vX.Y.Z` tag starts `.github/workflows/release.yml` on a macOS runner,
which:

1. Refuses any tag whose commit is not contained in `main`.
2. Runs `tools/release_notes.py`, which refuses to continue unless the tag, the
   `__version__` in `src/rbs/__init__.py`, and one dated `## [X.Y.Z]` changelog
   heading all agree. This gate runs before any dependency is installed, so an
   unprepared tag fails in seconds.
3. Repeats the lock, lint, and test gates on the tagged commit.
4. Builds the application with `tools/build_desktop.sh` and wraps it with
   `tools/package_dmg.sh`, producing `RBS-Desktop-X.Y.Z-macos-arm64.dmg`.
   RBS Desktop is a macOS application for Apple Silicon, which is what a
   GitHub-hosted macOS runner is. No other artifact is built.
5. Publishes a GitHub Release for the tag, using that release's changelog
   section as the description and attaching the disk image. The image is also
   kept as a workflow artifact for two weeks, so a failed publish can be
   retried without rebuilding.

The workflow never edits the changelog, creates a commit, or moves a tag: every
release edit happens locally, before the tag exists. Redoing a release means
deleting the tag and its GitHub Release, then pushing the tag again.

## Not automated yet

RBS Desktop is not signed with an Apple Developer ID. PyInstaller applies an
ad-hoc signature, so the bundle runs where it was built, but macOS quarantines it
after download and a user has to right-click the application and choose **Open**
once. The published release description says so. `release.yml` carries a
commented outline of the signing and notarization step; enabling it needs a
Developer ID certificate and an App Store Connect API key in repository secrets,
after which that installation note should come out of the release description.

