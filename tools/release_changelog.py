#!/usr/bin/env python3
"""Promote RBS's Unreleased notes into a dated release."""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG_PATH = PROJECT_ROOT / "CHANGELOG.md"
VERSION_PATH = PROJECT_ROOT / "src" / "rbs" / "__init__.py"
REPOSITORY_URL = "https://github.com/oestej/rbs"

VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
VERSION_ASSIGNMENT_PATTERN = re.compile(
    r"^(?P<prefix>__version__\s*=\s*)(?P<quote>['\"])(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"
    r"(?P=quote)[ \t]*$",
    re.MULTILINE,
)
UNRELEASED_HEADING_PATTERN = re.compile(r"^## \[Unreleased\][ \t]*$", re.MULTILINE)
RELEASE_HEADING_PATTERN = re.compile(
    r"^## \[(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\] - "
    r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})[ \t]*$",
    re.MULTILINE,
)
CATEGORY_PATTERN = re.compile(
    r"^### (?:Added|Changed|Deprecated|Removed|Fixed|Security)[ \t]*$",
    re.MULTILINE,
)
ENTRY_PATTERN = re.compile(r"^- .+", re.MULTILINE)
VERSION_LINK_PATTERN = re.compile(
    r"^\[(?:Unreleased|[0-9]+\.[0-9]+\.[0-9]+)\]:[^\n]*(?:\n|$)",
    re.MULTILINE,
)


class ReleasePreparationError(ValueError):
    """Raised when release inputs or repository state are inconsistent."""


@dataclass(frozen=True, slots=True)
class ReleaseUpdates:
    """Fully validated file contents for one release preparation."""

    changelog: str
    version_source: str
    previous_version: str | None


def _version_tuple(value: str) -> tuple[int, int, int]:
    if not VERSION_PATTERN.fullmatch(value):
        raise ReleasePreparationError(
            f"version must contain three numeric components (X.Y.Z): {value!r}"
        )
    major, minor, patch = value.split(".")
    if any(component.startswith("0") and component != "0" for component in (major, minor, patch)):
        raise ReleasePreparationError(
            f"version must contain three numeric components without leading zeros: {value!r}"
        )
    return int(major), int(minor), int(patch)


def _release_date(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ReleasePreparationError(f"date must use YYYY-MM-DD: {value!r}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ReleasePreparationError(f"date is not valid: {value!r}") from exc
    if parsed.isoformat() != value:
        raise ReleasePreparationError(f"date must use YYYY-MM-DD: {value!r}")
    return value


def _current_version(version_source: str) -> tuple[str, re.Match[str]]:
    matches = list(VERSION_ASSIGNMENT_PATTERN.finditer(version_source))
    if len(matches) != 1:
        raise ReleasePreparationError(
            "version source must contain exactly one numeric __version__ assignment"
        )
    return matches[0].group("version"), matches[0]


def _released_versions(changelog: str) -> list[str]:
    return [match.group("version") for match in RELEASE_HEADING_PATTERN.finditer(changelog)]


def _validate_release_order(versions: list[str]) -> None:
    for newer, older in zip(versions, versions[1:], strict=False):
        if _version_tuple(newer) <= _version_tuple(older):
            raise ReleasePreparationError(
                "released changelog sections must be in descending version order"
            )


def _unreleased_content(changelog: str, heading: re.Match[str]) -> tuple[str, int]:
    candidates = [
        match.start()
        for pattern in (RELEASE_HEADING_PATTERN, VERSION_LINK_PATTERN)
        if (match := pattern.search(changelog, heading.end())) is not None
    ]
    content_end = min(candidates, default=len(changelog))
    content = changelog[heading.end() : content_end]
    if not CATEGORY_PATTERN.search(content) or not ENTRY_PATTERN.search(content):
        raise ReleasePreparationError(
            "Unreleased must contain at least one standard category and bullet entry"
        )
    return content, content_end


def _link_definitions(versions: list[str]) -> str:
    newest = versions[0]
    links = [f"[Unreleased]: {REPOSITORY_URL}/compare/v{newest}...HEAD"]
    for index, version in enumerate(versions):
        if index + 1 < len(versions):
            older = versions[index + 1]
            url = f"{REPOSITORY_URL}/compare/v{older}...v{version}"
        else:
            url = f"{REPOSITORY_URL}/releases/tag/v{version}"
        links.append(f"[{version}]: {url}")
    return "\n".join(links)


def build_release_updates(
    *,
    changelog: str,
    version_source: str,
    target_version: str,
    release_date: str,
) -> ReleaseUpdates:
    """Validate and construct both release file updates without writing them."""
    target_key = _version_tuple(target_version)
    release_date = _release_date(release_date)
    current_version, version_match = _current_version(version_source)
    current_key = _version_tuple(current_version)

    unreleased_matches = list(UNRELEASED_HEADING_PATTERN.finditer(changelog))
    if len(unreleased_matches) != 1:
        raise ReleasePreparationError(
            "changelog must contain exactly one ## [Unreleased] heading"
        )
    heading = unreleased_matches[0]

    released = _released_versions(changelog)
    _validate_release_order(released)
    if target_version in released:
        raise ReleasePreparationError(f"release {target_version} already exists")

    previous_version = released[0] if released else None
    if previous_version is None:
        if target_key < current_key:
            raise ReleasePreparationError(
                f"first release {target_version} cannot precede current version {current_version}"
            )
    else:
        if current_version != previous_version:
            raise ReleasePreparationError(
                "current application version must match the newest changelog release "
                f"({previous_version}) before preparing the next release"
            )
        if target_key <= _version_tuple(previous_version):
            raise ReleasePreparationError(
                f"release {target_version} must be newer than {previous_version}"
            )

    content, content_end = _unreleased_content(changelog, heading)
    tail = changelog[content_end:].lstrip("\n")
    release_block = (
        "## [Unreleased]\n\n"
        f"## [{target_version}] - {release_date}"
        f"{content.rstrip()}\n\n"
    )
    updated_changelog = changelog[: heading.start()] + release_block + tail
    updated_changelog = VERSION_LINK_PATTERN.sub("", updated_changelog).rstrip()
    versions = _released_versions(updated_changelog)
    updated_changelog = f"{updated_changelog}\n\n{_link_definitions(versions)}\n"

    updated_version = (
        version_source[: version_match.start("version")]
        + target_version
        + version_source[version_match.end("version") :]
    )
    return ReleaseUpdates(
        changelog=updated_changelog,
        version_source=updated_version,
        previous_version=previous_version,
    )


def _atomic_write(path: Path, content: str) -> None:
    """Replace one text file while preserving its existing permissions."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, path.stat().st_mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_release(
    target_version: str,
    release_date: str,
    *,
    changelog_path: Path = CHANGELOG_PATH,
    version_path: Path = VERSION_PATH,
) -> ReleaseUpdates:
    """Prepare and persist a release after every validation has succeeded."""
    changelog = changelog_path.read_text(encoding="utf-8")
    version_source = version_path.read_text(encoding="utf-8")
    updates = build_release_updates(
        changelog=changelog,
        version_source=version_source,
        target_version=target_version,
        release_date=release_date,
    )
    _atomic_write(version_path, updates.version_source)
    _atomic_write(changelog_path, updates.changelog)
    return updates


def prepare_release_if_needed(
    target_version: str,
    release_date: str,
    *,
    changelog_path: Path = CHANGELOG_PATH,
    version_path: Path = VERSION_PATH,
) -> ReleaseUpdates | None:
    """Prepare a release, or do nothing when that version is already dated.

    Returns the written updates when files change, or ``None`` when the
    changelog already has this version and the application version agrees.
    """
    changelog = changelog_path.read_text(encoding="utf-8")
    version_source = version_path.read_text(encoding="utf-8")
    released = _released_versions(changelog)
    current_version, _ = _current_version(version_source)
    if target_version in released:
        if current_version != target_version:
            raise ReleasePreparationError(
                f"changelog already has {target_version} but the application "
                f"version is {current_version}"
            )
        return None
    return prepare_release(
        target_version,
        release_date,
        changelog_path=changelog_path,
        version_path=version_path,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Move RBS Unreleased notes into one dated release section."
    )
    parser.add_argument("version", help="numeric release version, for example 0.1.0")
    parser.add_argument(
        "--date",
        dest="release_date",
        required=True,
        help="release date in YYYY-MM-DD form",
    )
    parser.add_argument(
        "--if-needed",
        action="store_true",
        help=(
            "do nothing when this version already has a dated changelog heading "
            "and the application version agrees"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(argv)
    try:
        if options.if_needed:
            updates = prepare_release_if_needed(options.version, options.release_date)
            if updates is None:
                print(f"RBS {options.version} is already prepared.")
                return 0
        else:
            prepare_release(options.version, options.release_date)
    except (OSError, ReleasePreparationError) as exc:
        parser.error(str(exc))

    print(f"Prepared RBS {options.version} for {options.release_date}.")
    print("Next: run uv lock, validation, builds, and the documented Git release steps.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
