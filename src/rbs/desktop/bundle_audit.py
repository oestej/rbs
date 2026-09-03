"""Reject Python distributions that leak across the desktop build boundary."""

from __future__ import annotations

import argparse
import ast
import importlib.metadata as metadata
import tomllib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from packaging.utils import canonicalize_name

from rbs.desktop.license_bundle import (
    DEFAULT_PROJECT,
    PROJECT_ROOT,
    LicenseBundleError,
    dependency_closure,
    project_requirements,
)

DEFAULT_ANALYSES = (
    PROJECT_ROOT / "build" / "rbs-desktop" / "Analysis-00.toc",
    PROJECT_ROOT / "build" / "rbs-desktop" / "Analysis-01.toc",
)


@dataclass(frozen=True, slots=True)
class BundleLeak:
    """One undeclared distribution whose files appear in the frozen bundle."""

    name: str
    version: str
    targets: tuple[str, ...]


def analysis_sources(analysis_paths: Iterable[Path]) -> dict[Path, tuple[str, ...]]:
    """Map source files in PyInstaller analysis tables to bundle targets."""
    sources: defaultdict[Path, set[str]] = defaultdict(set)
    for analysis_path in analysis_paths:
        try:
            analysis = ast.literal_eval(analysis_path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError) as exc:
            raise LicenseBundleError(
                f"could not read PyInstaller analysis table {analysis_path}: {exc}"
            ) from exc
        _collect_analysis_sources(analysis, sources)
    return {
        source: tuple(sorted(targets))
        for source, targets in sorted(sources.items(), key=lambda item: str(item[0]))
    }


def undeclared_bundled_distributions(
    allowed_names: Iterable[str],
    installed: Iterable[metadata.Distribution],
    sources: Mapping[Path, Sequence[str]],
) -> tuple[BundleLeak, ...]:
    """Return installed distributions outside ``allowed_names`` found in analyses."""
    allowed = {canonicalize_name(name) for name in allowed_names}
    source_targets = {source.resolve(): tuple(targets) for source, targets in sources.items()}
    source_directories = tuple(source for source in source_targets if source.is_dir())
    leaks: list[BundleLeak] = []
    seen: set[str] = set()

    for distribution in installed:
        name = distribution.metadata.get("Name") or "Unknown distribution"
        key = canonicalize_name(name)
        if key in allowed or key in seen:
            continue
        seen.add(key)

        targets: set[str] = set()
        for package_file in distribution.files or ():
            located = Path(distribution.locate_file(package_file)).resolve()
            targets.update(source_targets.get(located, ()))
            for source_directory in source_directories:
                if located.is_relative_to(source_directory):
                    targets.update(source_targets[source_directory])

        if targets:
            leaks.append(
                BundleLeak(
                    name=name,
                    version=distribution.version,
                    targets=tuple(sorted(targets)),
                )
            )

    return tuple(sorted(leaks, key=lambda leak: leak.name.lower()))


def audit_bundle(
    pyproject_path: Path,
    analysis_paths: Iterable[Path],
) -> tuple[BundleLeak, ...]:
    """Audit a frozen bundle against the declared desktop/build closure."""
    roots = project_requirements(pyproject_path)
    allowed_names = {
        distribution.metadata.get("Name") or "Unknown distribution"
        for distribution in dependency_closure(roots)
    }
    with pyproject_path.open("rb") as pyproject_file:
        allowed_names.add(tomllib.load(pyproject_file)["project"]["name"])
    return undeclared_bundled_distributions(
        allowed_names,
        metadata.distributions(),
        analysis_sources(analysis_paths),
    )


def _collect_analysis_sources(
    value: object,
    sources: defaultdict[Path, set[str]],
) -> None:
    if isinstance(value, (list, tuple)):
        if (
            len(value) == 3
            and isinstance(value[0], str)
            and isinstance(value[1], str)
            and isinstance(value[2], str)
        ):
            source = Path(value[1])
            if source.is_absolute():
                sources[source.resolve()].add(value[0])
            return
        for item in value:
            _collect_analysis_sources(item, sources)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pyproject", type=Path, default=DEFAULT_PROJECT)
    parser.add_argument("--analysis", action="append", type=Path)
    args = parser.parse_args(argv)

    analyses = tuple(args.analysis) if args.analysis else DEFAULT_ANALYSES
    try:
        leaks = audit_bundle(args.pyproject.resolve(), analyses)
    except (LicenseBundleError, OSError, KeyError) as exc:
        parser.error(str(exc))

    if leaks:
        lines = [
            "the frozen desktop bundle contains distributions outside the declared "
            "desktop/build dependency closure:",
        ]
        for leak in leaks:
            sample = ", ".join(leak.targets[:5])
            if len(leak.targets) > 5:
                sample += ", ..."
            lines.append(f"  - {leak.name} {leak.version}: {sample}")
        lines.append(
            "exclude the optional import from rbs-desktop.spec or declare it as a "
            "desktop dependency"
        )
        parser.error("\n".join(lines))

    print("Verified frozen bundle against the declared desktop/build dependency closure")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
