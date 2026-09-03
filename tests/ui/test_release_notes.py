from pathlib import Path

from rbs.ui.release_notes import (
    RELEASE_NOTES_UNAVAILABLE,
    load_release_notes,
    release_notes_path,
)


def test_release_note_loader_reads_explicit_markdown_and_handles_missing_file(
    tmp_path: Path,
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## Unreleased\n", encoding="utf-8")

    assert load_release_notes(changelog) == "# Changelog\n\n## Unreleased\n"
    assert load_release_notes(tmp_path / "missing.md") == RELEASE_NOTES_UNAVAILABLE


def test_release_note_loader_finds_the_source_changelog(tmp_path: Path, monkeypatch) -> None:
    import rbs.ui.release_notes as release_notes

    module_path = tmp_path / "src" / "rbs" / "ui" / "release_notes.py"
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("Source release notes", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]", encoding="utf-8")
    monkeypatch.setattr(release_notes, "__file__", str(module_path))

    assert release_notes.release_notes_path() == changelog
    assert release_notes.load_release_notes() == "Source release notes"


def test_repository_release_note_path_resolves_to_the_canonical_file() -> None:
    path = release_notes_path()

    assert path.name == "CHANGELOG.md"
    assert path.is_file()
    assert "## [Unreleased]" in load_release_notes(path)

