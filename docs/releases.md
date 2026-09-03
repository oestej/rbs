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

Keep user-facing notes in `Unreleased` while you work. When `main` is ready,
tag it and push:

```bash
git tag -a vX.Y.Z -m "RBS X.Y.Z"
git push origin main --follow-tags
```

The release workflow runs `tools/release_changelog.py --if-needed`, which
updates the application version, moves the current `Unreleased` notes beneath
a dated heading, recreates an empty `Unreleased` section, and maintains GitHub
comparison links. If that write changed files, the workflow commits them onto
`main` and moves the tag onto the new commit before building. If you already
prepared the notes locally, it leaves the files alone.

The helper still works locally if you want to preview or commit the rollover
yourself:

```bash
uv run python tools/release_changelog.py X.Y.Z --date YYYY-MM-DD
uv lock
uv run ruff check .
uv run pytest
uv build
tools/build_desktop.sh --skip-sync
```

It validates every input before writing and refuses empty, duplicate, or
out-of-order releases. Continuous integration repeats the lock, lint, and
non-solver test gates, so running them locally is about finding problems
before the tag rather than about proving the release. `solve` tests stay a
local gate on real hardware. The `uv build` and `tools/build_desktop.sh`
steps are the local smoke check that the artifacts still assemble.

The first release is `0.1.0`. Keep its notes in `Unreleased` until you tag.

## What CI does

`.github/workflows/ci.yml` runs `uv lock --check`, Ruff, and the test suite on
macOS and Linux for every pull request and every push to `main`. macOS is the
platform RBS Desktop ships on; the Linux leg covers the CLI and the hosted
build.

Pushing a `vX.Y.Z` tag starts `.github/workflows/release.yml` on a macOS runner,
which:

1. Refuses any tag whose commit is not contained in `main`.
2. Runs `tools/release_changelog.py --if-needed`. Empty Unreleased notes fail
   here, in seconds, before any dependency is installed. A write is committed
   onto `main` and the tag is moved onto that commit.
3. Runs `tools/release_notes.py`, which refuses to continue unless the tag, the
   `__version__` in `src/rbs/__init__.py`, and one dated `## [X.Y.Z]` changelog
   heading all agree.
4. Repeats the lock, lint, and test gates on the prepared commit, with
   `-m "not solve"` so hosted runners do not run CP-SAT search tests.
5. Builds the application with `tools/build_desktop.sh`.
6. Signs and notarizes the application, packages it with
   `tools/package_dmg.sh`, then signs and notarizes the disk image, producing
   `RBS-Desktop-X.Y.Z-macos-arm64.dmg`. RBS Desktop is a macOS application for
   Apple Silicon, which is what a GitHub-hosted macOS runner is. No other
   artifact is built.
7. Publishes a GitHub Release for the tag, using that release's changelog
   section as the description and attaching the disk image. The image is also
   kept as a workflow artifact for two weeks, so a failed publish can be
   retried without rebuilding.

Redoing a release means deleting the tag and its GitHub Release, then pushing
the tag again. If the dated heading already exists, the workflow leaves it
alone and rebuilds.

## Signing and notarization

macOS quarantines a downloaded application that no Developer ID vouches for, and
refuses to open a disk image on the same grounds. A release therefore signs and
notarizes both, in that order, because an image can only be signed once it
exists and notarization can only follow a signature:

1. `tools/sign_desktop.sh` signs the application. codesign seals a bundle by
   hashing its contents, so it works inside out: the collected libraries first,
   then `rbs-solver` — a second executable in `Contents/MacOS`, which a bundle
   signature does not cover — then the bundle. Every signature adopts the
   hardened runtime, with `packaging/entitlements.plist` relaxing the three
   restrictions a frozen Python application cannot live under.
2. `tools/notarize_desktop.sh` uploads the application to Apple, waits for a
   verdict, and staples the ticket into the bundle. Stapling is what lets a
   first launch succeed on a machine that is offline.
3. `tools/package_dmg.sh` signs the disk image it builds around the stapled
   application, and `tools/notarize_desktop.sh` notarizes and staples that too.

Each script runs on its own, so a signing problem can be reproduced locally
without pushing a tag, and each takes its credentials from the environment or
from explicit options. `tools/sign_desktop.sh` and `tools/notarize_desktop.sh`
stop when a credential is missing, rather than quietly doing nothing;
`tools/package_dmg.sh` instead reports that it left the image unsigned and
carries on, so an unsigned build is still packageable.

### Repository secrets

Hold these in a repository environment named `release` rather than as
repository secrets. Repository secrets are readable by any workflow on any
branch; an environment restricts them to the job that declares it, and a
deployment rule limiting the environment to `v*` tags, plus a required
reviewer, means a branch cannot reach the certificate. `release.yml` already
declares `environment: release`.

Signing is skipped, not failed, when these are absent — a release still
publishes, with the workflow logging a warning and the release description
keeping its right-click-to-open note. Setting all of them turns signing on with
no further change.

| Secret | Holds |
| --- | --- |
| `APPLE_CERTIFICATE_P12` | Developer ID Application certificate and private key, exported as `.p12` and base64 encoded: `base64 -i certificate.p12 \| pbcopy` |
| `APPLE_CERTIFICATE_PASSWORD` | Password set when exporting that `.p12` |
| `APPLE_DEVELOPER_ID` | Full identity name, for example `Developer ID Application: Example Inc. (ABCDE12345)` |
| `APPLE_API_KEY_P8` | App Store Connect **team** key (`.p8`) with the Developer role, base64 encoded the same way. An individual key cannot notarize |
| `APPLE_API_KEY_ID` | That key's identifier |
| `APPLE_API_ISSUER_ID` | Issuer identifier from App Store Connect |

The certificate expires; a release that starts failing at the signing step with
no other change is the first place to look.

Export the `.p12` with only the Developer ID Application certificate and its
private key. Generate the App Store Connect key under **Team Keys** with the
Developer role: that is the least access notarization accepts, and a key created
under **Individual Keys** cannot notarize at all.

Both workflows pin every action to a commit rather than a tag, because a tag can
be moved and these actions run in the same job as the certificate. Bumping one
means replacing the commit and the version comment beside it together; a test
fails if a reference is ever left on a tag.

