from __future__ import annotations

import importlib.metadata as metadata
from email.message import Message
from pathlib import Path, PurePosixPath

import pytest
from packaging.requirements import Requirement

from rbs.desktop.bundle_audit import (
    analysis_sources,
    undeclared_bundled_distributions,
)
from rbs.desktop.license_bundle import (
    DistributionNotices,
    LicenseBundleError,
    NoticeFile,
    collect_distribution_notices,
    dependency_closure,
    project_requirements,
    render_bundle,
)
from rbs.ui.legal_notices import (
    APPLICATION_LICENSE_UNAVAILABLE,
    THIRD_PARTY_LICENSES_UNAVAILABLE,
    application_license_path,
    load_application_license,
    load_third_party_licenses,
)


class FakeDistribution:
    def __init__(
        self,
        root: Path,
        name: str,
        *,
        version: str = "1.0",
        requires: tuple[str, ...] = (),
        license_expression: str | None = "MIT",
        license_text: str | None = None,
        license_files: tuple[str, ...] = (),
    ) -> None:
        self.root = root
        self.version = version
        self.requires = requires
        self.metadata = Message()
        self.metadata["Name"] = name
        if license_expression:
            self.metadata["License-Expression"] = license_expression
        if license_text:
            self.metadata["License"] = license_text
        for license_file in license_files:
            self.metadata["License-File"] = license_file
        self.files: list[PurePosixPath] = []

    def add_file(self, relative_path: str, text: str) -> None:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.files.append(PurePosixPath(relative_path))

    def locate_file(self, path: object) -> Path:
        return self.root / str(path)


def test_project_requirements_include_the_desktop_and_build_extras(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "fixture"
version = "1.0"
dependencies = ["alpha>=1"]

[project.optional-dependencies]
desktop = ["alpha[native]>=1", "desktop-only>=2"]
build = ["build-tool>=3"]
""".strip(),
        encoding="utf-8",
    )

    requirements = project_requirements(pyproject)

    assert [(requirement.name, requirement.extras) for requirement in requirements] == [
        ("alpha", set()),
        ("alpha", {"native"}),
        ("desktop-only", set()),
        ("build-tool", set()),
    ]


def test_dependency_closure_honors_requested_extras_and_markers(tmp_path: Path) -> None:
    distributions = {
        "alpha": FakeDistribution(
            tmp_path / "alpha",
            "Alpha",
            requires=(
                "beta>=1",
                "native-helper>=1; extra == 'native'",
                "wrong-platform>=1; sys_platform == 'never'",
            ),
        ),
        "beta": FakeDistribution(tmp_path / "beta", "Beta"),
        "native-helper": FakeDistribution(tmp_path / "native-helper", "Native-Helper"),
    }

    def distribution_for(name: str) -> FakeDistribution:
        try:
            return distributions[name.lower()]
        except KeyError as exc:
            raise metadata.PackageNotFoundError(name) from exc

    resolved = dependency_closure(
        (Requirement("alpha"), Requirement("alpha[native]")),
        distribution_for=distribution_for,
    )

    assert [distribution.metadata["Name"] for distribution in resolved] == [
        "Alpha",
        "Beta",
        "Native-Helper",
    ]


def test_bundle_audit_finds_an_installed_distribution_outside_the_closure(
    tmp_path: Path,
) -> None:
    allowed = FakeDistribution(tmp_path / "allowed", "Allowed")
    allowed.add_file("allowed/__init__.py", "")
    leaked = FakeDistribution(tmp_path / "leaked", "Leaked-Optional", version="2.0")
    leaked.add_file("leaked/__init__.py", "")
    untouched = FakeDistribution(tmp_path / "untouched", "Untouched")
    untouched.add_file("untouched/__init__.py", "")

    violations = undeclared_bundled_distributions(
        ("Allowed",),
        (allowed, leaked, untouched),
        {
            (tmp_path / "allowed" / "allowed" / "__init__.py"): ("allowed",),
            (tmp_path / "leaked" / "leaked" / "__init__.py"): ("leaked",),
        },
    )

    assert [(leak.name, leak.version, leak.targets) for leak in violations] == [
        ("Leaked-Optional", "2.0", ("leaked",)),
    ]


def test_bundle_audit_reads_pyinstaller_analysis_sources(tmp_path: Path) -> None:
    module = (tmp_path / "demo.py").resolve()
    analysis = tmp_path / "Analysis-00.toc"
    analysis.write_text(
        repr(([('demo', str(module), 'PYMODULE')], [('ignored', 0)])),
        encoding="utf-8",
    )

    assert analysis_sources((analysis,)) == {module: ("demo",)}


def test_notice_collection_preserves_declared_and_discovered_files(tmp_path: Path) -> None:
    distribution = FakeDistribution(
        tmp_path,
        "Demo",
        version="2.3",
        license_expression="MIT",
        license_files=("LICENSE",),
    )
    distribution.add_file("demo-2.3.dist-info/licenses/LICENSE", "MIT license text")
    distribution.add_file("demo-2.3.dist-info/NOTICE", "Required attribution")
    distribution.add_file("demo/_vendor/helper-1.0.dist-info/LICENSE", "Vendored license")
    distribution.add_file("demo/license.py", "not legal text")

    notices = collect_distribution_notices(distribution)

    assert notices == DistributionNotices(
        name="Demo",
        version="2.3",
        declared_license="MIT",
        files=(
            NoticeFile("NOTICE", "Required attribution"),
            NoticeFile("LICENSE", "MIT license text"),
            NoticeFile("demo/_vendor/helper-1.0.dist-info/LICENSE", "Vendored license"),
        ),
    )


def test_notice_collection_uses_license_metadata_only_when_no_file_shipped(
    tmp_path: Path,
) -> None:
    distribution = FakeDistribution(
        tmp_path,
        "MetadataOnly",
        license_expression=None,
        license_text="BSD-3-Clause",
    )

    notices = collect_distribution_notices(distribution)

    assert notices.files == (NoticeFile("License metadata", "BSD-3-Clause"),)


def test_notice_collection_rejects_a_missing_declared_file(tmp_path: Path) -> None:
    distribution = FakeDistribution(
        tmp_path,
        "Broken",
        license_files=("LICENSE",),
    )

    with pytest.raises(LicenseBundleError, match="absent from its installation"):
        collect_distribution_notices(distribution)


def test_rendered_bundle_is_sorted_and_contains_full_text() -> None:
    bundle = render_bundle(
        (
            DistributionNotices("Zulu", "1", "MIT", (NoticeFile("LICENSE", "z text"),)),
            DistributionNotices(
                "Alpha",
                "2",
                "Apache-2.0",
                (NoticeFile("NOTICE", "alpha attribution"),),
            ),
        )
    )

    assert "Components: 2" in bundle
    assert bundle.index("Alpha 2") < bundle.index("Zulu 1")
    assert "alpha attribution" in bundle
    assert "z text" in bundle


def test_packaged_notice_loader_reads_text_and_has_a_source_fallback(tmp_path: Path) -> None:
    notices = tmp_path / "THIRD_PARTY_LICENSES.txt"
    notices.write_text("Complete notices", encoding="utf-8")

    assert load_third_party_licenses(notices) == "Complete notices"
    assert load_third_party_licenses(tmp_path / "missing.txt") == (
        THIRD_PARTY_LICENSES_UNAVAILABLE
    )


def test_application_license_loader_reads_text_and_has_a_fallback(tmp_path: Path) -> None:
    license_path = tmp_path / "LICENSE"
    license_path.write_text("Open Software License text", encoding="utf-8")

    assert load_application_license(license_path) == "Open Software License text"
    assert load_application_license(tmp_path / "missing.txt") == APPLICATION_LICENSE_UNAVAILABLE


def test_application_license_loader_finds_the_source_license(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import rbs.ui.legal_notices as legal_notices

    module_path = tmp_path / "src" / "rbs" / "ui" / "legal_notices.py"
    source_license = tmp_path / "LICENSE"
    source_license.write_text("Source license", encoding="utf-8")
    monkeypatch.setattr(legal_notices, "__file__", str(module_path))

    assert application_license_path() == source_license
    assert load_application_license() == "Source license"


def test_notice_loader_finds_the_source_build_artifact(tmp_path: Path, monkeypatch) -> None:
    import rbs.ui.legal_notices as legal_notices

    module_path = tmp_path / "src" / "rbs" / "ui" / "legal_notices.py"
    generated = tmp_path / "build" / "licenses" / "THIRD_PARTY_LICENSES.txt"
    generated.parent.mkdir(parents=True)
    generated.write_text("Generated notices", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]", encoding="utf-8")
    monkeypatch.setattr(legal_notices, "__file__", str(module_path))

    assert legal_notices.third_party_licenses_path() == generated
    assert legal_notices.load_third_party_licenses() == "Generated notices"
