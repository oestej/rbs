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

Review and commit the version, changelog, and refreshed lockfile together. Merge
the release-ready `v0` line into `main`, then create an annotated `vX.Y.Z` tag on
the resulting `main` commit. Use the matching changelog section as the GitHub
Release description and attach the verified artifacts.

The first release is `0.1.0`. Until it is actually prepared, its notes remain in
`Unreleased`; do not add a date or tag in advance.

## Future CI/CD

A future release workflow should invoke the same rollover logic, verify that the
release heading and package version agree, run the lock/test/build gates above,
and publish only from the tagged `main` commit. The current repository does not
automate commits, merges, tags, signing, notarization, or publication.

