"""Read release notes packaged alongside the shared application UI."""

from __future__ import annotations

from pathlib import Path

CHANGELOG_FILENAME = "CHANGELOG.md"
RELEASE_NOTES_UNAVAILABLE = "Release notes are unavailable in this build."


def release_notes_path() -> Path:
    """Return the source-tree or packaged changelog path."""
    module_path = Path(__file__).resolve()
    packaged = module_path.parents[1] / CHANGELOG_FILENAME
    if packaged.is_file():
        return packaged

    project_root = module_path.parents[3]
    source = project_root / CHANGELOG_FILENAME
    return source if (project_root / "pyproject.toml").is_file() else packaged


def load_release_notes(path: Path | None = None) -> str:
    """Load release-note Markdown, with a useful packaged-build fallback."""
    notes_path = path or release_notes_path()
    try:
        return notes_path.read_text(encoding="utf-8")
    except OSError:
        return RELEASE_NOTES_UNAVAILABLE
