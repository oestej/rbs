"""Build the third-party notices bundled with RBS Desktop.

The desktop build runs this after syncing its locked dependencies. Rather than
maintaining a second dependency inventory, the generator follows the declared
desktop/build dependency closure and preserves every license, notice, copyright,
and authors file shipped by those distributions.
"""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import re
import sys
import tomllib
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PROJECT = PROJECT_ROOT / "pyproject.toml"
DEFAULT_OUTPUT = PROJECT_ROOT / "build" / "licenses" / (
    "THIRD_PARTY_LICENSES.txt"
)
BUNDLED_OPTIONAL_GROUPS = ("desktop", "build")
NOTICE_NAME = re.compile(
    r"^(?:licen[cs]e|copying|copyright|notice|authors)(?:$|[._-])",
    re.IGNORECASE,
)
NON_TEXT_SUFFIXES = {".dll", ".dylib", ".exe", ".pyd", ".py", ".pyc", ".so"}
DIVIDER = "=" * 88
FILE_DIVIDER = "-" * 88


class LicenseBundleError(RuntimeError):
    """The installed desktop environment cannot produce complete notices."""


@dataclass(frozen=True, slots=True)
class NoticeFile:
    """One license-related file and the distribution-relative name it shipped as."""

    name: str
    text: str


@dataclass(frozen=True, slots=True)
class DistributionNotices:
    """License material collected for one installed distribution."""

    name: str
    version: str
    declared_license: str
    files: tuple[NoticeFile, ...]


def project_requirements(
    pyproject_path: Path,
    *,
    optional_groups: Sequence[str] = BUNDLED_OPTIONAL_GROUPS,
) -> tuple[Requirement, ...]:
    """Return base plus selected optional requirements from ``pyproject.toml``."""
    with pyproject_path.open("rb") as pyproject_file:
        project = tomllib.load(pyproject_file)["project"]

    raw_requirements = list(project.get("dependencies", ()))
    optional = project.get("optional-dependencies", {})
    for group in optional_groups:
        try:
            raw_requirements.extend(optional[group])
        except KeyError as exc:
            raise LicenseBundleError(
                f"pyproject.toml has no optional dependency group named {group!r}"
            ) from exc

    parsed: list[Requirement] = []
    for raw_requirement in raw_requirements:
        try:
            requirement = Requirement(raw_requirement)
        except InvalidRequirement as exc:
            raise LicenseBundleError(f"invalid project requirement: {raw_requirement}") from exc
        if _requirement_applies(requirement, (group for group in optional_groups)):
            parsed.append(requirement)
    return tuple(parsed)


def dependency_closure(
    roots: Iterable[Requirement],
    *,
    distribution_for: Callable[[str], metadata.Distribution] = metadata.distribution,
) -> tuple[metadata.Distribution, ...]:
    """Resolve the installed, marker-aware dependency closure of ``roots``.

    Requested extras travel with each dependency edge.  A distribution is
    revisited only if another edge enables a previously unseen extra.
    """
    queue = deque(roots)
    distributions: dict[str, metadata.Distribution] = {}
    requested_extras: defaultdict[str, set[str]] = defaultdict(set)
    processed_extras: dict[str, frozenset[str]] = {}

    while queue:
        requirement = queue.popleft()
        key = canonicalize_name(requirement.name)
        requested_extras[key].update(requirement.extras)
        active_extras = frozenset(requested_extras[key])
        if key in processed_extras and processed_extras[key] == active_extras:
            continue

        try:
            distribution = distributions.get(key) or distribution_for(requirement.name)
        except metadata.PackageNotFoundError as exc:
            raise LicenseBundleError(
                f"desktop dependency {requirement.name!r} is not installed; "
                "sync the desktop and build extras before generating notices"
            ) from exc

        distributions[key] = distribution
        processed_extras[key] = active_extras
        for raw_child in distribution.requires or ():
            try:
                child = Requirement(raw_child)
            except InvalidRequirement as exc:
                raise LicenseBundleError(
                    f"{_distribution_name(distribution)} contains an invalid requirement: "
                    f"{raw_child}"
                ) from exc
            if _requirement_applies(child, ("", *sorted(active_extras))):
                queue.append(child)

    return tuple(
        distributions[key]
        for key in sorted(
            distributions,
            key=lambda item: _distribution_name(distributions[item]).lower(),
        )
    )


def collect_distribution_notices(
    distribution: metadata.Distribution,
) -> DistributionNotices:
    """Collect all declared and conventionally named notice files from a wheel."""
    package_name = _distribution_name(distribution)
    package_files = tuple(distribution.files or ())
    declared_paths = tuple(distribution.metadata.get_all("License-File") or ())

    selected: dict[str, object] = {}
    for package_file in package_files:
        normalized = _normalize_path(package_file)
        if _looks_like_notice_file(normalized):
            selected[normalized] = package_file

    missing_declared: list[str] = []
    for declared_path in declared_paths:
        match = _find_declared_file(declared_path, package_files)
        if match is None:
            missing_declared.append(declared_path)
        else:
            selected[_normalize_path(match)] = match

    if missing_declared:
        missing = ", ".join(sorted(missing_declared))
        raise LicenseBundleError(
            f"{package_name} declares license file(s) that are absent from its "
            f"installation: {missing}"
        )

    notices: list[NoticeFile] = []
    for normalized, package_file in sorted(selected.items()):
        located = Path(distribution.locate_file(package_file))
        try:
            raw_text = located.read_bytes()
        except OSError as exc:
            raise LicenseBundleError(
                f"could not read {package_name} notice file {normalized}: {exc}"
            ) from exc
        text = raw_text.decode("utf-8", errors="replace").strip()
        if text:
            notices.append(NoticeFile(_display_path(normalized), text))

    declared_license = _declared_license(distribution)
    if not notices:
        fallback = _metadata_license_text(distribution)
        if not fallback:
            raise LicenseBundleError(
                f"{package_name} ships no license/notice files and declares no license metadata"
            )
        notices.append(NoticeFile("License metadata", fallback))

    return DistributionNotices(
        name=package_name,
        version=distribution.version,
        declared_license=declared_license,
        files=tuple(notices),
    )


def python_runtime_notices() -> DistributionNotices:
    """Collect the license for the Python runtime embedded by PyInstaller."""
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    python_dirname = f"python{sys.version_info.major}.{sys.version_info.minor}"
    stdlib = Path(sys.base_prefix) / "lib" / python_dirname
    candidates = (
        stdlib / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE",
        Path(sys.base_prefix) / "Lib" / "LICENSE.txt",
    )
    license_path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if license_path is None:
        raise LicenseBundleError(
            f"could not find the Python {version} runtime license beneath {sys.base_prefix}"
        )
    return DistributionNotices(
        name="Python runtime",
        version=version,
        declared_license="Python-2.0",
        files=(
            NoticeFile(
                license_path.name,
                license_path.read_text(encoding="utf-8", errors="replace").strip(),
            ),
        ),
    )


def render_bundle(notices: Iterable[DistributionNotices]) -> str:
    """Render a deterministic, human-readable notices artifact."""
    records = sorted(notices, key=lambda record: record.name.lower())
    lines = [
        "RBS Desktop — Third-Party License Notices",
        DIVIDER,
        "",
        "RBS Desktop incorporates the open-source components listed below. These license",
        "terms and attribution notices apply to their respective components, not to RBS",
        "Desktop as a whole.",
        "",
        f"Components: {len(records)}",
    ]

    for record in records:
        lines.extend(("", "", DIVIDER, f"{record.name} {record.version}"))
        if record.declared_license:
            lines.append(f"Declared license: {record.declared_license}")
        for notice_file in record.files:
            lines.extend(("", FILE_DIVIDER, notice_file.name, FILE_DIVIDER, notice_file.text))

    return "\n".join(lines).rstrip() + "\n"


def build_bundle(pyproject_path: Path) -> str:
    """Resolve the desktop environment and render all bundled notices."""
    roots = project_requirements(pyproject_path)
    distributions = dependency_closure(roots)
    records = [collect_distribution_notices(distribution) for distribution in distributions]
    records.append(python_runtime_notices())
    return render_bundle(records)


def _requirement_applies(requirement: Requirement, extras: Iterable[str]) -> bool:
    if requirement.marker is None:
        return True
    return any(requirement.marker.evaluate({"extra": extra}) for extra in extras)


def _distribution_name(distribution: metadata.Distribution) -> str:
    return distribution.metadata.get("Name") or "Unknown distribution"


def _normalize_path(path: object) -> str:
    return str(path).replace("\\", "/").lstrip("./")


def _looks_like_notice_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return (
        bool(NOTICE_NAME.match(name))
        and PurePosixPath(name).suffix.lower() not in NON_TEXT_SUFFIXES
    )


def _find_declared_file(
    declared_path: str,
    package_files: Sequence[object],
) -> object | None:
    declared = _normalize_path(declared_path)
    normalized_files = [
        (_normalize_path(package_file), package_file) for package_file in package_files
    ]

    exact = [package_file for path, package_file in normalized_files if path == declared]
    if len(exact) == 1:
        return exact[0]

    root_metadata = []
    declared_parts = PurePosixPath(declared).parts
    for path, package_file in normalized_files:
        parts = PurePosixPath(path).parts
        if not parts or not parts[0].endswith(".dist-info"):
            continue
        remainder = parts[1:]
        if remainder and remainder[0] == "licenses":
            remainder = remainder[1:]
        if remainder == declared_parts:
            root_metadata.append(package_file)
    if len(root_metadata) == 1:
        return root_metadata[0]

    preferred_suffixes = (f".dist-info/licenses/{declared}", f".dist-info/{declared}")
    preferred = [
        package_file
        for path, package_file in normalized_files
        if any(path.endswith(suffix) for suffix in preferred_suffixes)
    ]
    if len(preferred) == 1:
        return preferred[0]

    suffix = f"/{declared}"
    matches = [package_file for path, package_file in normalized_files if path.endswith(suffix)]
    return matches[0] if len(matches) == 1 else None


def _display_path(path: str) -> str:
    parts = PurePosixPath(path).parts
    if parts and parts[0].endswith(".dist-info"):
        remainder = parts[1:]
        if remainder and remainder[0] == "licenses":
            remainder = remainder[1:]
        return "/".join(remainder) or PurePosixPath(path).name
    return path


def _declared_license(distribution: metadata.Distribution) -> str:
    expression = (distribution.metadata.get("License-Expression") or "").strip()
    if expression:
        return expression
    legacy = (distribution.metadata.get("License") or "").strip()
    if legacy and "\n" not in legacy and len(legacy) <= 160:
        return legacy
    classifiers = distribution.metadata.get_all("Classifier") or ()
    licenses = [
        classifier.removeprefix("License :: OSI Approved :: ")
        for classifier in classifiers
        if classifier.startswith("License :: OSI Approved :: ")
    ]
    return "; ".join(licenses)


def _metadata_license_text(distribution: metadata.Distribution) -> str:
    legacy = (distribution.metadata.get("License") or "").strip()
    if legacy:
        return legacy
    expression = (distribution.metadata.get("License-Expression") or "").strip()
    if expression:
        return f"The installed distribution declares the license expression: {expression}"
    declared = _declared_license(distribution)
    if declared:
        return f"The installed distribution declares: {declared}"
    return ""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pyproject", type=Path, default=DEFAULT_PROJECT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    try:
        bundle = build_bundle(args.pyproject.resolve())
    except (LicenseBundleError, OSError, KeyError) as exc:
        parser.error(str(exc))

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(bundle, encoding="utf-8")
    component_count = bundle.count(f"\n{DIVIDER}\n") - 1
    print(f"Wrote notices for {component_count} components to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
