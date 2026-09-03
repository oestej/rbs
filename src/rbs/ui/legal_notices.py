"""Read legal notices packaged alongside the shared desktop UI."""

from __future__ import annotations

from pathlib import Path

THIRD_PARTY_LICENSES_FILENAME = "THIRD_PARTY_LICENSES.txt"
APPLICATION_LICENSE_FILENAME = "LICENSE"
APPLICATION_LICENSE_UNAVAILABLE = "The RBS license text is unavailable in this build."
THIRD_PARTY_LICENSES_UNAVAILABLE = (
    "Third-party license notices are generated and included with packaged RBS Desktop builds."
)


def application_license_path() -> Path:
    """Return the source, wheel, or frozen-app path for the RBS license."""
    module_path = Path(__file__).resolve()
    candidates = (
        module_path.parents[1] / APPLICATION_LICENSE_FILENAME,
        module_path.parents[2] / "licenses" / APPLICATION_LICENSE_FILENAME,
        module_path.parents[3] / APPLICATION_LICENSE_FILENAME,
    )
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[0])


def load_application_license(path: Path | None = None) -> str:
    """Load the full license governing RBS."""
    license_path = path or application_license_path()
    try:
        return license_path.read_text(encoding="utf-8")
    except OSError:
        return APPLICATION_LICENSE_UNAVAILABLE


def third_party_licenses_path() -> Path:
    """Return the source or PyInstaller path reserved for generated notices."""
    module_path = Path(__file__).resolve()
    packaged = module_path.parents[1] / "legal" / THIRD_PARTY_LICENSES_FILENAME
    if packaged.is_file():
        return packaged

    project_root = module_path.parents[3]
    generated = project_root / "build" / "licenses" / THIRD_PARTY_LICENSES_FILENAME
    return generated if (project_root / "pyproject.toml").is_file() else packaged


def load_third_party_licenses(path: Path | None = None) -> str:
    """Load the packaged notices, with a useful message for source/cloud runs."""
    notice_path = path or third_party_licenses_path()
    try:
        return notice_path.read_text(encoding="utf-8")
    except OSError:
        return THIRD_PARTY_LICENSES_UNAVAILABLE
