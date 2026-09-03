import pytest
from release_changelog import build_release_updates
from release_notes import (
    Release,
    ReleaseResolutionError,
    load_release,
    resolve_release,
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


def _second_release():
    first = _first_release()
    changelog = first.changelog.replace(
        "## [Unreleased]\n\n",
        "## [Unreleased]\n\n### Fixed\n\n- Corrected a user-visible defect.\n\n",
        1,
    )
    return build_release_updates(
        changelog=changelog,
        version_source=first.version_source,
        target_version="0.2.0",
        release_date="2026-09-10",
    )


def test_resolves_the_notes_of_the_only_release() -> None:
    prepared = _first_release()

    release = resolve_release(
        tag="v0.1.0",
        changelog=prepared.changelog,
        version_source=prepared.version_source,
    )

    assert release == Release(
        version="0.1.0",
        date="2026-09-02",
        notes="### Added\n\n- First user-facing capability.\n",
    )
    assert release.tag == "v0.1.0"


def test_notes_stop_before_the_previous_release_section() -> None:
    prepared = _second_release()

    release = resolve_release(
        tag="v0.2.0",
        changelog=prepared.changelog,
        version_source=prepared.version_source,
    )

    assert release.date == "2026-09-10"
    assert release.notes == "### Fixed\n\n- Corrected a user-visible defect.\n"
    assert "First user-facing capability" not in release.notes


def test_notes_exclude_the_comparison_link_definitions() -> None:
    prepared = _first_release()

    release = resolve_release(
        tag="v0.1.0",
        changelog=prepared.changelog,
        version_source=prepared.version_source,
    )

    assert "[Unreleased]:" not in release.notes
    assert "[0.1.0]:" not in release.notes


@pytest.mark.parametrize("tag", ["0.1.0", "v0.1", "release-0.1.0", "v0.1.0-rc1", ""])
def test_rejects_tags_that_are_not_release_tags(tag: str) -> None:
    prepared = _first_release()

    with pytest.raises(ReleaseResolutionError, match="vX.Y.Z"):
        resolve_release(
            tag=tag,
            changelog=prepared.changelog,
            version_source=prepared.version_source,
        )


def test_rejects_a_tag_that_outruns_the_application_version() -> None:
    prepared = _first_release()

    with pytest.raises(ReleaseResolutionError, match="does not match the application version"):
        resolve_release(
            tag="v0.2.0",
            changelog=prepared.changelog,
            version_source=prepared.version_source,
        )


def test_rejects_a_version_the_changelog_never_released() -> None:
    prepared = _first_release()
    changelog = prepared.changelog.replace("## [0.1.0] - 2026-09-02\n\n", "", 1)

    with pytest.raises(ReleaseResolutionError, match="exactly one dated"):
        resolve_release(
            tag="v0.1.0",
            changelog=changelog,
            version_source=prepared.version_source,
        )


def test_rejects_a_duplicated_release_heading() -> None:
    prepared = _first_release()
    changelog = prepared.changelog.replace(
        "## [0.1.0] - 2026-09-02",
        "## [0.1.0] - 2026-09-02\n\n### Added\n\n- Duplicate.\n\n## [0.1.0] - 2026-09-02",
        1,
    )

    with pytest.raises(ReleaseResolutionError, match="exactly one dated"):
        resolve_release(
            tag="v0.1.0",
            changelog=changelog,
            version_source=prepared.version_source,
        )


def test_rejects_a_release_section_without_entries() -> None:
    prepared = _first_release()
    changelog = prepared.changelog.replace("- First user-facing capability.\n", "", 1)

    with pytest.raises(ReleaseResolutionError, match="standard category and bullet entry"):
        resolve_release(
            tag="v0.1.0",
            changelog=changelog,
            version_source=prepared.version_source,
        )


def test_rejects_an_ambiguous_application_version() -> None:
    prepared = _first_release()

    with pytest.raises(ReleaseResolutionError, match="exactly one numeric"):
        resolve_release(
            tag="v0.1.0",
            changelog=prepared.changelog,
            version_source='__version__ = "0.1.0"\n__version__ = "0.1.0"\n',
        )


def test_load_release_reads_the_repository_files(tmp_path) -> None:
    prepared = _first_release()
    changelog_path = tmp_path / "CHANGELOG.md"
    version_path = tmp_path / "__init__.py"
    changelog_path.write_text(prepared.changelog, encoding="utf-8")
    version_path.write_text(prepared.version_source, encoding="utf-8")

    release = load_release(
        "v0.1.0",
        changelog_path=changelog_path,
        version_path=version_path,
    )

    assert release.version == "0.1.0"
    assert release.notes == "### Added\n\n- First user-facing capability.\n"
