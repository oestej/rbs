#!/usr/bin/env python3
"""Resolve the changelog section that describes one tagged RBS release.

Release automation runs this before it builds anything. It answers the only
question a publish step needs answered — does this tag actually describe a
prepared release? — by requiring the Git tag, the single application version,
and the dated changelog heading to agree, and it hands back the notes that
belong on the published release.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from release_changelog import (
    CATEGORY_PATTERN,
    CHANGELOG_PATH,
    ENTRY_PATTERN,
    RELEASE_HEADING_PATTERN,
    VERSION_ASSIGNMENT_PATTERN,
    VERSION_LINK_PATTERN,
    VERSION_PATH,
)

TAG_PATTERN = re.compile(r"^v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$")
SECTION_BOUNDARY_PATTERN = re.compile(r"^## ", re.MULTILINE)


class ReleaseResolutionError(ValueError):
    """Raised when a tag, the application version, and the changelog disagree."""


@dataclass(frozen=True, slots=True)
class Release:
    """One prepared release as the repository itself describes it."""

    version: str
    date: str
    notes: str

    @property
    def tag(self) -> str:
        return f"v{self.version}"


def version_from_tag(tag: str) -> str:
    """Read the numeric version out of a ``vX.Y.Z`` release tag."""
    match = TAG_PATTERN.fullmatch(tag.strip())
    if match is None:
        raise ReleaseResolutionError(f"release tag must use the vX.Y.Z form: {tag!r}")
    return match.group("version")


def application_version(version_source: str) -> str:
    """Read the single application version out of the version module source."""
    matches = list(VERSION_ASSIGNMENT_PATTERN.finditer(version_source))
    if len(matches) != 1:
        raise ReleaseResolutionError(
            "version source must contain exactly one numeric __version__ assignment"
        )
    return matches[0].group("version")


def resolve_release(*, tag: str, changelog: str, version_source: str) -> Release:
    """Verify that one tag names a prepared release and return its notes."""
    version = version_from_tag(tag)

    current_version = application_version(version_source)
    if current_version != version:
        raise ReleaseResolutionError(
            f"tag {tag} does not match the application version {current_version}; "
            "prepare the release with tools/release_changelog.py before tagging"
        )

    headings = [
        match
        for match in RELEASE_HEADING_PATTERN.finditer(changelog)
        if match.group("version") == version
    ]
    if len(headings) != 1:
        raise ReleaseResolutionError(
            f"changelog must contain exactly one dated ## [{version}] release heading"
        )
    heading = headings[0]

    boundaries = (
        SECTION_BOUNDARY_PATTERN.search(changelog, heading.end()),
        VERSION_LINK_PATTERN.search(changelog, heading.end()),
    )
    content_end = min(
        (match.start() for match in boundaries if match is not None),
        default=len(changelog),
    )
    notes = changelog[heading.end() : content_end].strip()
    if not CATEGORY_PATTERN.search(notes) or not ENTRY_PATTERN.search(notes):
        raise ReleaseResolutionError(
            f"release {version} must document at least one standard category and "
            "bullet entry"
        )

    return Release(version=version, date=heading.group("date"), notes=f"{notes}\n")


def load_release(
    tag: str,
    *,
    changelog_path: Path = CHANGELOG_PATH,
    version_path: Path = VERSION_PATH,
) -> Release:
    """Resolve one release from the files checked out in the repository."""
    return resolve_release(
        tag=tag,
        changelog=changelog_path.read_text(encoding="utf-8"),
        version_source=version_path.read_text(encoding="utf-8"),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that a release tag, the application version, and the changelog "
            "agree, then emit that release's notes."
        )
    )
    parser.add_argument("tag", help="release tag, for example v0.1.0")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write the notes to this file instead of standard output",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(argv)
    try:
        release = load_release(options.tag)
        if options.output is None:
            print(release.notes, end="")
        else:
            options.output.write_text(release.notes, encoding="utf-8")
    except (OSError, ReleaseResolutionError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
