"""The boundary that keeps the desktop build free of the hosted stack.

``rbs.cloud`` may import the shared workspace code. Nothing shared may import
``rbs.cloud``. Enforcing it statically is what makes a future PyInstaller build
of the desktop entry point possible without dragging in the identity stack, and
it catches the mistake at the moment it is made rather than at packaging time.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tomllib
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
PACKAGE = SRC / "rbs"
CLOUD_ONLY_DEPENDENCIES = {"jwt", "httpx"}
SOLVER_FORBIDDEN_IMPORTS = {"rbs.cloud", "rbs.store", "rbs.ui", "nicegui", "reportlab"}
PRIVATE_SOLVER_PACKAGE = "rbs.solver.core"
UI_COMPOSITION_MODULES = {"__main__.py"}
PYOBJC_MODULES = {"AppKit", "Foundation", "objc", "PyObjCTools"}


def test_project_version_is_single_sourced_for_build_and_runtime() -> None:
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)

    assert "version" not in project["project"]
    assert project["project"]["dynamic"] == ["version"]
    assert project["tool"]["hatch"]["version"]["path"] == "src/rbs/__init__.py"
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"][
        "CHANGELOG.md"
    ] == "rbs/CHANGELOG.md"
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"][
        "LICENSE"
    ] == "rbs/LICENSE"


def _module_name(path: Path) -> str:
    relative = path.relative_to(SRC).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_names(path: Path) -> set[str]:
    """Every module this file imports, whether at module or function scope."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def _shared_modules() -> list[Path]:
    return [
        path
        for path in sorted(PACKAGE.rglob("*.py"))
        if "cloud" not in path.relative_to(PACKAGE).parts
    ]


def test_shared_code_never_imports_the_cloud_package() -> None:
    violations = [
        (_module_name(path), imported)
        for path in _shared_modules()
        for imported in _imported_names(path)
        if imported == "rbs.cloud" or imported.startswith("rbs.cloud.")
    ]
    assert not violations, (
        "shared modules must not import rbs.cloud; the dependency runs the "
        f"other way: {violations}"
    )


def test_shared_code_never_imports_cloud_only_dependencies() -> None:
    violations = [
        (_module_name(path), imported)
        for path in _shared_modules()
        for imported in _imported_names(path)
        if imported.split(".")[0] in CLOUD_ONLY_DEPENDENCIES
    ]
    assert not violations, (
        f"{sorted(CLOUD_ONLY_DEPENDENCIES)} are hosted-build dependencies and "
        f"must stay inside rbs.cloud: {violations}"
    )


def test_ui_leaf_modules_do_not_import_the_application_entrypoint() -> None:
    ui_paths = sorted((PACKAGE / "ui").rglob("*.py"))
    violations = [
        (_module_name(path), imported)
        for path in ui_paths
        if path.name != "__main__.py"
        for imported in _imported_names(path)
        if imported == "rbs.ui.app" or imported.startswith("rbs.ui.app.")
    ]
    assert not violations, (
        "UI components must flow into the application composition root, not "
        f"import it back and create cycles: {violations}"
    )


def test_ui_depends_on_the_repository_seam_not_sqlite() -> None:
    """Only the ``python -m rbs.ui`` composition root may construct the Store."""
    ui_paths = [
        path
        for path in sorted((PACKAGE / "ui").rglob("*.py"))
        if path.name not in UI_COMPOSITION_MODULES
    ]
    violations = [
        (_module_name(path), imported)
        for path in ui_paths
        for imported in _imported_names(path)
        if imported == "rbs.store" or imported.startswith("rbs.store.")
    ]
    assert not violations, (
        "UI modules must use the WorkspaceRepository seam instead of the "
        f"concrete SQLite Store: {violations}"
    )


def test_workspace_contract_and_controller_do_not_import_sqlite_store() -> None:
    paths = [PACKAGE / "repository.py", PACKAGE / "workspaces.py"]
    violations = [
        (_module_name(path), imported)
        for path in paths
        for imported in _imported_names(path)
        if imported == "rbs.store" or imported.startswith("rbs.store.")
    ]
    assert not violations, (
        "workspace application layers must be storage-independent: "
        f"{violations}"
    )


def test_desktop_entry_point_does_not_load_the_cloud_package() -> None:
    """Importing the desktop CLI must not drag the hosted stack in with it."""
    probe = (
        "import sys; import rbs.cli; import rbs.ui.app; "
        "loaded = sorted(m for m in sys.modules "
        "if m == 'rbs.cloud' or m.startswith('rbs.cloud.') "
        f"or m.split('.')[0] in {sorted(CLOUD_ONLY_DEPENDENCIES)!r}); "
        "print(','.join(loaded))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "", (
        f"desktop entry point loaded hosted-only modules: {result.stdout.strip()}"
    )


def test_solver_process_code_is_independent_of_ui_storage_and_cloud() -> None:
    solver_paths = sorted((PACKAGE / "solver").rglob("*.py"))
    violations = [
        (_module_name(path), imported)
        for path in solver_paths
        for imported in _imported_names(path)
        if any(
            imported == forbidden or imported.startswith(f"{forbidden}.")
            for forbidden in SOLVER_FORBIDDEN_IMPORTS
        )
    ]
    assert not violations, (
        "the standalone solver must not reach into UI, storage, or cloud code: "
        f"{violations}"
    )


def test_application_code_never_imports_the_private_solver_core() -> None:
    """Only the solver-side service is allowed to enter the implementation."""
    core = PACKAGE / "solver" / "core"
    service = PACKAGE / "solver" / "service.py"
    application_paths = [
        path
        for path in sorted(PACKAGE.rglob("*.py"))
        if path != service and not path.is_relative_to(core)
    ]
    violations = [
        (_module_name(path), imported)
        for path in application_paths
        for imported in _imported_names(path)
        if imported == PRIVATE_SOLVER_PACKAGE
        or imported.startswith(f"{PRIVATE_SOLVER_PACKAGE}.")
    ]
    assert not violations, (
        "application code must use the public rbs.solver API instead of its "
        f"private core: {violations}"
    )


def test_importing_public_solver_api_does_not_load_ortools_or_private_core() -> None:
    probe = (
        "import sys; import rbs.solver; "
        "loaded = sorted(m for m in sys.modules "
        "if m == 'ortools' or m.startswith('ortools.') "
        "or m == 'rbs.solver.core' or m.startswith('rbs.solver.core.')); "
        "print(','.join(loaded))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "", (
        "the public solver API eagerly loaded its private implementation: "
        f"{result.stdout.strip()}"
    )


def test_objective_c_bridges_are_confined_to_the_macos_desktop_adapter() -> None:
    macos = PACKAGE / "desktop" / "macos"
    violations = [
        (_module_name(path), imported)
        for path in sorted(PACKAGE.rglob("*.py"))
        if not path.is_relative_to(macos)
        for imported in _imported_names(path)
        if imported.split(".")[0] in PYOBJC_MODULES
    ]
    assert not violations, f"PyObjC imports must stay under rbs.desktop.macos: {violations}"


def test_only_the_desktop_composition_root_selects_the_macos_adapter() -> None:
    allowed = PACKAGE / "desktop" / "main.py"
    macos = PACKAGE / "desktop" / "macos"
    violations = [
        (_module_name(path), imported)
        for path in sorted(PACKAGE.rglob("*.py"))
        if path != allowed and not path.is_relative_to(macos)
        for imported in _imported_names(path)
        if imported == "rbs.desktop.macos" or imported.startswith("rbs.desktop.macos.")
    ]
    assert not violations, f"only rbs.desktop.main may select a native adapter: {violations}"


def test_shared_and_solver_imports_do_not_load_appkit() -> None:
    probe = (
        "import json,sys; import rbs.cli; import rbs.ui.app; import rbs.solver; "
        "print(json.dumps(sorted(name for name in sys.modules "
        "if name in {'AppKit','Foundation','objc','PyObjCTools'} "
        "or name.startswith('rbs.desktop.macos'))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == []
