from pathlib import Path

import pytest
from release_changelog import (
    ReleasePreparationError,
    build_release_updates,
    prepare_release,
    prepare_release_if_needed,
)

INITIAL_CHANGELOG = """# Changelog

All notable changes are documented here.

## [Unreleased]

### Added

- First user-facing capability.
"""
INITIAL_VERSION_SOURCE = '"""Residency block scheduler."""\n\n__version__ = "0.1.0"\n'


def _first_release():
    return build_release_updates(
        changelog=INITIAL_CHANGELOG,
        version_source=INITIAL_VERSION_SOURCE,
        target_version="0.1.0",
        release_date="2026-09-02",
    )


def _with_unreleased_fix(changelog: str) -> str:
    return changelog.replace(
        "## [Unreleased]\n\n",
        "## [Unreleased]\n\n### Fixed\n\n- Corrected a user-visible defect.\n\n",
        1,
    )


def test_first_release_rolls_notes_down_and_keeps_unreleased_open() -> None:
    updates = _first_release()

    assert (
        "## [Unreleased]\n\n## [0.1.0] - 2026-09-02\n\n### Added"
        in updates.changelog
    )
    assert "- First user-facing capability." in updates.changelog
    assert updates.changelog.endswith(
        "[Unreleased]: https://github.com/oestej/rbs/compare/v0.1.0...HEAD\n"
        "[0.1.0]: https://github.com/oestej/rbs/releases/tag/v0.1.0\n"
    )
    assert updates.version_source == INITIAL_VERSION_SOURCE
    assert updates.previous_version is None


def test_subsequent_release_updates_version_and_comparison_links() -> None:
    first = _first_release()
    updates = build_release_updates(
        changelog=_with_unreleased_fix(first.changelog),
        version_source=first.version_source,
        target_version="0.2.0",
        release_date="2026-10-15",
    )

    assert "## [Unreleased]\n\n## [0.2.0] - 2026-10-15" in updates.changelog
    assert updates.changelog.index("## [0.2.0]") < updates.changelog.index("## [0.1.0]")
    assert "__version__ = \"0.2.0\"" in updates.version_source
    assert updates.previous_version == "0.1.0"
    assert (
        "[Unreleased]: https://github.com/oestej/rbs/compare/v0.2.0...HEAD"
        in updates.changelog
    )
    assert (
        "[0.2.0]: https://github.com/oestej/rbs/compare/v0.1.0...v0.2.0"
        in updates.changelog
    )


@pytest.mark.parametrize("value", ["1.0", "v1.0.0", "1.0.0rc1", "01.0.0", "next"])
def test_release_rejects_non_numeric_three_part_versions(value: str) -> None:
    with pytest.raises(ReleasePreparationError, match="three numeric components"):
        build_release_updates(
            changelog=INITIAL_CHANGELOG,
            version_source=INITIAL_VERSION_SOURCE,
            target_version=value,
            release_date="2026-09-02",
        )


@pytest.mark.parametrize("value", ["2026-9-2", "2026-02-30", "September 2, 2026"])
def test_release_rejects_invalid_dates(value: str) -> None:
    with pytest.raises(ReleasePreparationError, match="date"):
        build_release_updates(
            changelog=INITIAL_CHANGELOG,
            version_source=INITIAL_VERSION_SOURCE,
            target_version="0.1.0",
            release_date=value,
        )


def test_release_rejects_empty_unreleased_notes() -> None:
    empty = "# Changelog\n\n## [Unreleased]\n"

    with pytest.raises(ReleasePreparationError, match="at least one standard category"):
        build_release_updates(
            changelog=empty,
            version_source=INITIAL_VERSION_SOURCE,
            target_version="0.1.0",
            release_date="2026-09-02",
        )


def test_release_rejects_duplicate_or_non_increasing_versions() -> None:
    first = _first_release()
    changelog = _with_unreleased_fix(first.changelog)

    with pytest.raises(ReleasePreparationError, match="already exists"):
        build_release_updates(
            changelog=changelog,
            version_source=first.version_source,
            target_version="0.1.0",
            release_date="2026-09-03",
        )

    with pytest.raises(ReleasePreparationError, match="must be newer"):
        build_release_updates(
            changelog=changelog,
            version_source=first.version_source,
            target_version="0.0.9",
            release_date="2026-09-03",
        )


def test_failed_file_preparation_does_not_modify_either_file(tmp_path: Path) -> None:
    changelog_path = tmp_path / "CHANGELOG.md"
    version_path = tmp_path / "__init__.py"
    changelog_path.write_text("# Changelog\n\n## [Unreleased]\n", encoding="utf-8")
    version_path.write_text(INITIAL_VERSION_SOURCE, encoding="utf-8")

    with pytest.raises(ReleasePreparationError):
        prepare_release(
            "0.1.0",
            "2026-09-02",
            changelog_path=changelog_path,
            version_path=version_path,
        )

    assert changelog_path.read_text(encoding="utf-8") == "# Changelog\n\n## [Unreleased]\n"
    assert version_path.read_text(encoding="utf-8") == INITIAL_VERSION_SOURCE


def test_if_needed_is_a_noop_when_the_release_already_exists(tmp_path: Path) -> None:
    changelog_path = tmp_path / "CHANGELOG.md"
    version_path = tmp_path / "__init__.py"
    changelog_path.write_text(INITIAL_CHANGELOG, encoding="utf-8")
    version_path.write_text(INITIAL_VERSION_SOURCE, encoding="utf-8")
    prepared = prepare_release(
        "0.1.0",
        "2026-09-02",
        changelog_path=changelog_path,
        version_path=version_path,
    )

    result = prepare_release_if_needed(
        "0.1.0",
        "2026-09-04",
        changelog_path=changelog_path,
        version_path=version_path,
    )

    assert result is None
    assert changelog_path.read_text(encoding="utf-8") == prepared.changelog
    assert version_path.read_text(encoding="utf-8") == prepared.version_source


def test_if_needed_prepares_when_the_heading_is_missing(tmp_path: Path) -> None:
    changelog_path = tmp_path / "CHANGELOG.md"
    version_path = tmp_path / "__init__.py"
    changelog_path.write_text(INITIAL_CHANGELOG, encoding="utf-8")
    version_path.write_text(INITIAL_VERSION_SOURCE, encoding="utf-8")

    result = prepare_release_if_needed(
        "0.1.0",
        "2026-09-02",
        changelog_path=changelog_path,
        version_path=version_path,
    )

    assert result is not None
    assert "## [0.1.0] - 2026-09-02" in changelog_path.read_text(encoding="utf-8")
    assert changelog_path.read_text(encoding="utf-8") == result.changelog


def test_if_needed_rejects_a_changelog_heading_that_disagrees_with_the_version(
    tmp_path: Path,
) -> None:
    first = _first_release()
    changelog_path = tmp_path / "CHANGELOG.md"
    version_path = tmp_path / "__init__.py"
    changelog_path.write_text(first.changelog, encoding="utf-8")
    version_path.write_text('__version__ = "0.2.0"\n', encoding="utf-8")

    with pytest.raises(ReleasePreparationError, match="application version is 0.2.0"):
        prepare_release_if_needed(
            "0.1.0",
            "2026-09-02",
            changelog_path=changelog_path,
            version_path=version_path,
        )


def test_file_preparation_writes_both_validated_updates(tmp_path: Path) -> None:
    changelog_path = tmp_path / "CHANGELOG.md"
    version_path = tmp_path / "__init__.py"
    changelog_path.write_text(INITIAL_CHANGELOG, encoding="utf-8")
    version_path.write_text(INITIAL_VERSION_SOURCE, encoding="utf-8")

    updates = prepare_release(
        "0.2.0",
        "2026-09-02",
        changelog_path=changelog_path,
        version_path=version_path,
    )

    assert changelog_path.read_text(encoding="utf-8") == updates.changelog
    assert version_path.read_text(encoding="utf-8") == updates.version_source
    assert "## [0.2.0] - 2026-09-02" in updates.changelog
    assert "__version__ = \"0.2.0\"" in updates.version_source
