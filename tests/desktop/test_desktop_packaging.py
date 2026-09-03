"""Desktop startup and frozen-executable packaging boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from rbs.catalog import sample_instance
from rbs.desktop import main as desktop
from rbs.desktop.documents import DesktopDocumentController
from rbs.desktop.recovery import allocate_recovery_path
from rbs.desktop.settings import DesktopSettingsFile
from rbs.models.enums import SolverEngineName
from rbs.models.instance import SchedulerInput, SolverProblem
from rbs.solver.client import (
    SOLVER_COMMAND_ENV,
    default_solver_command,
)
from rbs.solver.contract import SolveRequest, SolveSuccess, parse_response_json
from rbs.store import Store


def test_desktop_parser_accepts_an_optional_rbsc_document(tmp_path: Path) -> None:
    document = tmp_path / "clinic year.RBSC"
    state_db = tmp_path / "state.sqlite"

    options = desktop.parse_arguments([str(document)], state_db=state_db)

    assert options.document == document.resolve()
    assert options.state_db == state_db.resolve()
    assert options.recovery_path is None
    assert options.recovery_sources == ()
    assert options.settings_path is None
    assert not options.remove_state_db_on_exit


def test_normal_desktop_parser_owns_its_temporary_database(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_db = tmp_path / "rbs-desktop-owned" / "session.sqlite"
    monkeypatch.setattr(desktop, "desktop_state_db", lambda: state_db)

    options = desktop.parse_arguments([], recovery_root=tmp_path / "recovery")

    assert options.state_db == state_db.resolve()
    assert options.remove_state_db_on_exit


def test_desktop_parser_allocates_a_private_recovery_slot_when_requested(tmp_path) -> None:
    recovery_root = tmp_path / "recovery"

    options = desktop.parse_arguments(
        [],
        state_db=tmp_path / "state.sqlite",
        recovery_root=recovery_root,
    )

    assert options.recovery_path is not None
    assert options.recovery_path.parent == recovery_root
    assert options.recovery_path.name.startswith(f"checkpoint-{os.getpid()}-")
    assert options.recovery_path.suffix == ".sqlite"
    assert options.recovery_sources == ()


def test_desktop_parser_accepts_an_application_settings_test_seam(tmp_path) -> None:
    settings = tmp_path / "application" / "settings.json"

    options = desktop.parse_arguments(
        [],
        state_db=tmp_path / "state.sqlite",
        settings_path=settings,
    )

    assert options.settings_path == settings.resolve()


def test_desktop_parser_rejects_non_workspace_documents(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        desktop.parse_arguments([str(tmp_path / "schedule.json")], state_db=tmp_path / "db")


def test_desktop_parser_ignores_legacy_macos_process_serial_number(tmp_path: Path) -> None:
    options = desktop.parse_arguments(["-psn_0_12345"], state_db=tmp_path / "db")

    assert options.document is None


def test_each_desktop_process_gets_an_isolated_editing_database(tmp_path: Path) -> None:
    first = desktop.desktop_state_db(temp_parent=tmp_path)
    second = desktop.desktop_state_db(temp_parent=tmp_path)

    assert first.name == second.name == "session.sqlite"
    assert first.parent != second.parent
    assert first.parent.parent == second.parent.parent == tmp_path
    assert first.parent.name.startswith("rbs-desktop-")


def test_desktop_state_cleanup_removes_only_sqlite_files(tmp_path: Path) -> None:
    directory = tmp_path / "rbs-desktop-owned"
    directory.mkdir()
    database = directory / "session.sqlite"
    sqlite_files = [
        database,
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
        Path(f"{database}-journal"),
    ]
    for path in sqlite_files:
        path.write_bytes(b"sqlite")
    unrelated = directory / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")

    desktop.remove_desktop_state_db(database)

    assert not any(path.exists() for path in sqlite_files)
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert directory.is_dir()


@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        (None, None),
        ((), None),
        (("/tmp/year.rbsc",), Path("/tmp/year.rbsc")),
        ("/tmp/year.rbsc", Path("/tmp/year.rbsc")),
    ],
)
def test_native_dialog_result_shapes_are_normalized(selected, expected) -> None:
    assert desktop._selected_path(selected) == expected


def test_native_dialog_types_support_current_and_legacy_pywebview(monkeypatch) -> None:
    modern = SimpleNamespace(FileDialog=SimpleNamespace(OPEN=10, SAVE=20))
    monkeypatch.setitem(sys.modules, "webview", modern)
    assert desktop._file_dialog_type("OPEN") == 10

    legacy = SimpleNamespace(OPEN_DIALOG=1, SAVE_DIALOG=2)
    monkeypatch.setitem(sys.modules, "webview", legacy)
    assert desktop._file_dialog_type("SAVE") == 2


def test_native_workspace_dialogs_start_in_documents(monkeypatch, tmp_path) -> None:
    class Window:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def create_file_dialog(self, **options):
            self.calls.append(options)
            return (tmp_path / ("opened.rbsc" if len(self.calls) == 1 else "saved.rbsc"),)

    window = Window()
    documents = tmp_path / "Documents"
    documents.mkdir()
    monkeypatch.setattr(desktop, "_native_window", lambda: window)
    monkeypatch.setitem(
        sys.modules,
        "webview",
        SimpleNamespace(FileDialog=SimpleNamespace(OPEN=10, SAVE=20)),
    )
    dialogs = desktop.NiceGuiNativeFileDialogs(workspace_directory=documents)

    assert asyncio.run(dialogs.choose_open_path()) == tmp_path / "opened.rbsc"
    assert asyncio.run(dialogs.choose_save_path("workspace.rbsc")) == (
        tmp_path / "saved.rbsc"
    )
    assert window.calls == [
        {
            "dialog_type": 10,
            "directory": str(documents),
            "allow_multiple": False,
            "file_types": ("RBS Workspace (*.rbsc)",),
        },
        {
            "dialog_type": 20,
            "directory": str(documents),
            "save_filename": "workspace.rbsc",
            "file_types": ("RBS Workspace (*.rbsc)",),
        },
    ]


def test_native_settings_dialogs_use_json_filters(monkeypatch, tmp_path) -> None:
    class Window:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def create_file_dialog(self, **options):
            self.calls.append(options)
            return (tmp_path / ("loaded.json" if len(self.calls) == 1 else "saved.json"),)

    window = Window()
    monkeypatch.setattr(desktop, "_native_window", lambda: window)
    monkeypatch.setitem(
        sys.modules,
        "webview",
        SimpleNamespace(FileDialog=SimpleNamespace(OPEN=10, SAVE=20)),
    )
    dialogs = desktop.NiceGuiNativeFileDialogs()

    assert asyncio.run(dialogs.choose_settings_open_path()) == tmp_path / "loaded.json"
    assert asyncio.run(dialogs.choose_settings_save_path("settings.json")) == (
        tmp_path / "saved.json"
    )
    assert window.calls == [
        {
            "dialog_type": 10,
            "allow_multiple": False,
            "file_types": ("RBS Settings (*.json)",),
        },
        {
            "dialog_type": 20,
            "save_filename": "settings.json",
            "file_types": ("RBS Settings (*.json)",),
        },
    ]


def test_main_calls_freeze_support_before_native_runtime(monkeypatch, tmp_path) -> None:
    calls: list[str] = []
    monkeypatch.setattr(desktop.multiprocessing, "freeze_support", lambda: calls.append("freeze"))
    monkeypatch.setattr(desktop, "run_desktop", lambda _options: calls.append("desktop") or 7)

    assert desktop.main(["--state-db", str(tmp_path / "state.sqlite")]) == 7
    assert calls == ["freeze", "desktop"]


def test_normal_main_launches_the_native_runtime(monkeypatch, tmp_path: Path) -> None:
    captured: list[desktop.DesktopOptions] = []
    monkeypatch.setattr(desktop.multiprocessing, "freeze_support", lambda: None)
    monkeypatch.setattr(desktop, "run_desktop", lambda options: captured.append(options) or 0)

    assert desktop.main(["--state-db", str(tmp_path / "db")]) == 0
    assert captured[0].document is None


def test_desktop_runtime_starts_without_an_open_workspace(tmp_path: Path) -> None:
    options = desktop.DesktopOptions(document=None, state_db=tmp_path / "state.sqlite")

    host = desktop._build_runtime(options)

    assert host.document_io is not None
    assert host.document_io.workspace is None
    assert host.document_io.path is None
    assert not host.document_io.dirty
    assert host.store_for(host.principal(None)).current() is None
    assert not host.allows_database_restore


def test_desktop_runtime_applies_persisted_application_settings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import rbs.desktop.settings as desktop_settings

    monkeypatch.setattr(desktop_settings, "detected_solver_workers", lambda: 12)
    settings_path = tmp_path / "application" / "settings.json"
    settings = DesktopSettingsFile(settings_path)
    raw = sample_instance().model_dump(mode="json")
    raw["solver"]["num_workers"] = 3
    assert settings.capture(SchedulerInput.model_validate(raw))

    host = desktop._build_runtime(
        desktop.DesktopOptions(
            document=None,
            state_db=tmp_path / "state.sqlite",
            settings_path=settings_path,
        )
    )

    assert host.document_io.workspace is None
    workspace = host.document_io.new()
    assert workspace.instance.solver.num_workers == 12
    assert host.document_io.application_settings.path == settings_path.resolve()


def test_desktop_runtime_loads_the_requested_rbsc_document(tmp_path: Path) -> None:
    source_store = Store(tmp_path / "source.sqlite")
    source_store.init()
    source = source_store.create("Opened from Finder", sample_instance())
    document = tmp_path / "opened.rbsc"
    document.write_text(source_store.export_workspace_rbsc(source.id), encoding="utf-8")

    host = desktop._build_runtime(
        desktop.DesktopOptions(document=document, state_db=tmp_path / "state.sqlite")
    )

    assert host.document_io.path == document.resolve()
    assert host.store_for(host.principal(None)).current().name == "Opened from Finder"
    assert not host.document_io.dirty


def test_desktop_runtime_recovers_a_stale_checkpoint_as_untitled(tmp_path: Path) -> None:
    source_store = Store(tmp_path / "source.sqlite")
    source_store.init()
    source_store.create("Recovered draft", sample_instance())
    stale = allocate_recovery_path(
        tmp_path / "stale-recovery",
        pid=123,
        token="a" * 32,
    )
    DesktopDocumentController(
        source_store,
        SimpleNamespace(),
        recovery_path=stale,
    ).mark_new()
    recovery = tmp_path / "recovery" / "current.sqlite"
    recovery.parent.mkdir()

    host = desktop._build_runtime(
        desktop.DesktopOptions(
            document=None,
            state_db=tmp_path / "state.sqlite",
            recovery_path=recovery,
            recovery_sources=(stale,),
        )
    )

    assert host.document_io.workspace.name == "Recovered draft"
    assert host.document_io.path is None
    assert host.document_io.dirty
    assert host.document_io.recovered_from == stale
    assert recovery.is_file()
    assert not stale.exists()


def test_desktop_launch_uses_native_window_options(monkeypatch, tmp_path: Path) -> None:
    from nicegui import native as nicegui_native

    from rbs.desktop.capability import CAPABILITY_QUERY, DesktopCapability
    from rbs.ui import app as ui_app

    captured = {}
    installed = []
    invalidated = []
    removed = []
    checkpoint_cleared = []
    fake_store = SimpleNamespace(
        invalidate=lambda reason: invalidated.append(reason),
    )
    fake_host = SimpleNamespace(
        document_io=SimpleNamespace(
            path=None,
            clear_recovery_checkpoint=lambda: checkpoint_cleared.append(True) or True,
        ),
        principal=lambda _request: "local",
        store_for=lambda _principal: fake_store,
    )
    monkeypatch.setattr(desktop, "_build_runtime", lambda _options: fake_host)
    monkeypatch.setattr(
        desktop,
        "remove_desktop_state_db",
        lambda path: removed.append(path),
    )
    monkeypatch.setattr(nicegui_native, "find_open_port", lambda: 8123)
    monkeypatch.setattr(
        DesktopCapability,
        "install",
        lambda self, app: installed.append((self, app)),
    )
    monkeypatch.setattr(
        ui_app,
        "serve",
        lambda host, **options: captured.update(host=host, **options),
    )

    result = desktop.run_desktop(
        desktop.DesktopOptions(
            document=None,
            state_db=tmp_path / "state.sqlite",
            remove_state_db_on_exit=True,
        )
    )

    assert result == 0
    assert captured["host"] is fake_host
    assert captured["native"] is True
    assert captured["port"] == 8123
    assert captured["window_size"] == desktop.DEFAULT_WINDOW_SIZE
    assert captured["title"] == "RBS Desktop"
    assert captured["show"] is False
    assert captured["reload"] is False
    from nicegui import app as nicegui_app

    assert nicegui_app.native.window_args["confirm_close"] is True
    assert nicegui_app.native.window_args["url"].startswith(
        f"http://127.0.0.1:8123/?{CAPABILITY_QUERY}="
    )
    assert invalidated == ["desktop application has closed"]
    assert removed == [tmp_path / "state.sqlite"]
    assert checkpoint_cleared == [True]
    assert installed[0][1] is nicegui_app
    if sys.platform == "darwin":
        menu = nicegui_app.native.start_args["menu"][0]
        assert menu.title == "Help"
        assert [getattr(item, "title", None) for item in menu.items] == [
            "Export Logs…",
            None,
            "Debug Mode",
        ]


def test_desktop_crash_preserves_recovery_checkpoint(monkeypatch, tmp_path: Path) -> None:
    from nicegui import native as nicegui_native

    from rbs.desktop.capability import DesktopCapability
    from rbs.ui import app as ui_app

    checkpoint_cleared = []
    fake_store = SimpleNamespace(invalidate=lambda _reason: None)
    fake_host = SimpleNamespace(
        document_io=SimpleNamespace(
            path=None,
            clear_recovery_checkpoint=lambda: checkpoint_cleared.append(True) or True,
        ),
        principal=lambda _request: "local",
        store_for=lambda _principal: fake_store,
    )
    monkeypatch.setattr(desktop, "_build_runtime", lambda _options: fake_host)
    monkeypatch.setattr(desktop, "remove_desktop_state_db", lambda _path: None)
    monkeypatch.setattr(nicegui_native, "find_open_port", lambda: 8124)
    monkeypatch.setattr(DesktopCapability, "install", lambda _self, _app: None)

    def crash(_host, **_options) -> None:
        raise RuntimeError("crashed")

    monkeypatch.setattr(ui_app, "serve", crash)

    with pytest.raises(RuntimeError, match="crashed"):
        desktop.run_desktop(
            desktop.DesktopOptions(
                document=None,
                state_db=tmp_path / "state.sqlite",
                remove_state_db_on_exit=True,
            )
        )

    assert checkpoint_cleared == []


def test_frozen_solver_command_uses_the_bundled_console_helper(monkeypatch) -> None:
    monkeypatch.delenv(SOLVER_COMMAND_ENV, raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        sys,
        "executable",
        "/Applications/RBS Desktop.app/Contents/MacOS/RBS Desktop",
    )

    assert default_solver_command() == (
        "/Applications/RBS Desktop.app/Contents/MacOS/rbs-solver",
    )


def test_explicit_solver_command_still_wins_in_a_frozen_build(monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv(SOLVER_COMMAND_ENV, "custom-solver --mode local")

    assert default_solver_command() == ("custom-solver", "--mode", "local")


def test_desktop_solver_helper_speaks_the_solver_protocol() -> None:
    instance = sample_instance()
    instance.solver.engine = SolverEngineName.STUB
    request = SolveRequest.from_problem(
        SolverProblem.from_instance(instance),
        options=instance.solver,
        request_id="desktop-worker",
    )

    completed = subprocess.run(
        [sys.executable, "packaging/rbs_solver.py"],
        input=request.model_dump_json(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0
    diagnostics = [json.loads(line) for line in completed.stderr.splitlines()]
    assert [record["event"] for record in diagnostics] == [
        "solver.started",
        "solver.completed",
    ]
    assert all(record["runtime"] == "solver" for record in diagnostics)
    response = parse_response_json(completed.stdout)
    assert isinstance(response, SolveSuccess)
    assert response.request_id == "desktop-worker"


def test_spec_collects_runtime_resources_and_avoids_macos_argv_emulation() -> None:
    root = Path(__file__).resolve().parents[2]
    spec = (root / "rbs-desktop.spec").read_text(encoding="utf-8")
    entry = (root / "packaging" / "rbs_desktop.py").read_text(encoding="utf-8")
    solver_entry = (root / "packaging" / "rbs_solver.py").read_text(encoding="utf-8")

    assert "collect_all" not in spec
    for excluded in (
        '"rbs.cloud"',
        '"cryptography"',
        '"jwt"',
        '"pypdf"',
        '"pytest"',
        '"ruff"',
    ):
        assert excluded in spec
    assert 'name="rbs-solver"' in spec
    assert "MERGE(" not in spec
    assert '"rbs/ui/static"' in spec
    assert '"rbs/data"' in spec
    assert 'project_root / "CHANGELOG.md"' in spec
    assert 'str(project_root / "src" / "rbs" / "__init__.py")' in spec
    assert 'project_root / "LICENSE"' in spec
    assert '"licenses"' in spec
    assert '"THIRD_PARTY_LICENSES.txt"' in spec
    assert '"rbs/legal"' in spec
    assert '"public.filename-extension": ["rbsc"]' in spec
    assert "argv_emulation=False" in spec
    assert '"CFBundleDocumentTypes"' not in spec
    assert '"assets" / "rbs.icns"' in spec
    assert '"LSBackgroundOnly": False' in spec
    assert '"NSHighResolutionCapable": True' in spec
    assert (root / "packaging" / "assets" / "rbs.icns").is_file()
    assert "multiprocessing.freeze_support()" in entry
    assert entry.index("multiprocessing.freeze_support()") < entry.index(
        "from rbs.desktop.main import main"
    )
    assert "from rbs.solver.process import main" in solver_entry


def test_desktop_builder_is_executable_and_uses_the_packaging_spec() -> None:
    root = Path(__file__).resolve().parents[2]
    builder = root / "tools" / "build_desktop.sh"
    source = builder.read_text(encoding="utf-8")

    assert builder.stat().st_mode & stat.S_IXUSR
    assert "uv sync --extra desktop --extra build" in source
    assert "--frozen" in source
    assert "uv run --no-sync pyinstaller" in source
    assert "python tools/build_third_party_licenses.py" in source
    assert "python tools/audit_desktop_bundle.py" in source
    assert "rbs-desktop.spec" in source
    assert '"${desktop_executable}" --version' in source
    assert '"${solver_executable}" --version' in source


def test_signing_entitlements_only_relax_the_hardened_runtime() -> None:
    root = Path(__file__).resolve().parents[2]
    entitlements = root / "packaging" / "entitlements.plist"

    declared = plistlib.loads(entitlements.read_bytes())

    # A frozen Python application cannot adopt the hardened runtime without
    # these. Anything beyond them would be requesting access RBS does not need.
    assert declared == {
        "com.apple.security.cs.allow-jit": True,
        "com.apple.security.cs.allow-unsigned-executable-memory": True,
        "com.apple.security.cs.disable-library-validation": True,
    }


def test_desktop_signer_signs_inside_out_with_the_hardened_runtime() -> None:
    root = Path(__file__).resolve().parents[2]
    signer = root / "tools" / "sign_desktop.sh"
    source = signer.read_text(encoding="utf-8")

    assert signer.stat().st_mode & stat.S_IXUSR
    # The notary service rejects a build missing either of these.
    assert "--options runtime" in source
    assert "--timestamp" in source
    assert "packaging/entitlements.plist" in source
    # A bundle signature seals its contents, so the nested binaries and the
    # second executable in Contents/MacOS have to be signed before the bundle.
    nested = source.index('find "${application}/Contents"')
    solver = source.index('"${application}/Contents/MacOS/rbs-solver"')
    bundle = source.index('sign --entitlements "${entitlements}" "${application}"')
    assert nested < solver < bundle
    assert "codesign --verify --deep --strict" in source
    # A few hundred consecutive requests to Apple's timestamp service should not
    # cost a release when one of them is refused.
    assert "for attempt in 1 2 3; do" in source


def test_notarizer_waits_for_a_verdict_and_staples_the_ticket() -> None:
    root = Path(__file__).resolve().parents[2]
    notarizer = root / "tools" / "notarize_desktop.sh"
    source = notarizer.read_text(encoding="utf-8")

    assert notarizer.stat().st_mode & stat.S_IXUSR
    assert "xcrun notarytool submit" in source
    assert "--wait" in source
    # `zip` drops the symlinks and signature that PyInstaller's bundle relies on.
    assert "ditto -c -k --keepParent" in source
    # notarytool can report a rejection without failing, so the verdict is read
    # back rather than inferred from the exit status.
    assert 'plutil -extract status raw' in source
    assert '"${status}" != "Accepted"' in source
    assert "xcrun notarytool log" in source
    assert "xcrun stapler staple" in source
    assert "xcrun stapler validate" in source


def test_disk_image_is_signed_only_when_an_identity_is_available() -> None:
    root = Path(__file__).resolve().parents[2]
    packager = root / "tools" / "package_dmg.sh"
    source = packager.read_text(encoding="utf-8")

    assert 'identity="${APPLE_DEVELOPER_ID:-}"' in source
    assert 'if [[ -n "${identity}" ]]; then' in source
    assert "the disk image is unsigned" in source
    # A disk image is not code: the hardened runtime does not apply to it.
    assert "--options runtime" not in source


def test_workflow_actions_are_pinned_to_commit_shas() -> None:
    root = Path(__file__).resolve().parents[2]
    workflows = sorted((root / ".github" / "workflows").glob("*.yml"))

    assert workflows
    used = [
        line.split("uses:", 1)[1].strip()
        for workflow in workflows
        for line in workflow.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("uses:")
    ]

    assert used
    for reference in used:
        # A third-party action runs in the same job as the Developer ID
        # certificate, so a moved tag would be enough to leak it. Only a commit
        # can be pinned; the trailing comment records which version it was.
        action, _, pinned = reference.partition("@")
        revision = pinned.split("#", 1)[0].strip()
        assert len(revision) == 40, f"{action} is not pinned to a commit: {reference}"
        assert all(character in "0123456789abcdef" for character in revision), (
            f"{action} is not pinned to a commit: {reference}"
        )


def test_release_workflow_signs_and_notarizes_both_artifacts() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert 'pytest -m "not solve"' in workflow
    assert "--extra desktop" in workflow
    assert "release_changelog.py" in workflow
    assert "--if-needed" in workflow
    assert workflow.index("release_changelog.py") < workflow.index("release_notes.py")

    # Every signing step is gated on the credentials existing, so a release
    # stays publishable until the secrets are configured.
    # The certificate, the API key, the signature, and both notarizations.
    assert workflow.count("steps.signing.outputs.enabled == 'true'") == 5
    assert "tools/sign_desktop.sh" in workflow
    assert workflow.count("tools/notarize_desktop.sh --path") == 2
    # The image is packaged from the notarized application, then notarized
    # itself, before anything uploads or publishes it.
    signed = workflow.index("tools/sign_desktop.sh")
    application = workflow.index('tools/notarize_desktop.sh --path "dist/RBS Desktop.app"')
    packaged = workflow.index("tools/package_dmg.sh")
    upload = workflow.index("upload-artifact")
    publish = workflow.index("gh release create")
    assert signed < application < packaged < upload < publish
    assert "security create-keychain" in workflow
    assert "security set-key-partition-list" in workflow


def test_desktop_build_outputs_are_ignored() -> None:
    root = Path(__file__).resolve().parents[2]
    ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert {"build/", "dist/", "*.dmg", ".DS_Store"} <= set(ignored)


def test_importing_desktop_startup_does_not_load_native_or_cloud_stacks() -> None:
    probe = (
        "import json,sys; import rbs.desktop.main; "
        "print(json.dumps(sorted(name for name in sys.modules "
        "if name == 'nicegui' or name.startswith('nicegui.') "
        "or name == 'webview' or name.startswith('webview.') "
        "or name == 'rbs.cloud' or name.startswith('rbs.cloud.'))))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(completed.stdout) == []
