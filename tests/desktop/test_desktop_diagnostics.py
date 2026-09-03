from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import pickle
import stat
import sys
import time
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

from rbs.desktop.diagnostics import (
    DesktopDiagnosticsService,
    DesktopDiagnosticsSettingsFile,
    default_diagnostics_path,
    default_log_directory,
    export_log_bundle,
    prune_logs,
)
from rbs.logging import LoggingConfig, configure_logging, get_logger


def _unpickle_native_configuration(payload: bytes) -> None:
    """Spawn target proving menu unpickling performs the early child bootstrap."""
    pickle.loads(payload)  # noqa: S301 - test payload was created in this process
    from rbs.logging import current_runtime

    runtime = current_runtime()
    assert runtime is not None
    get_logger("desktop.native").info("native.bootstrap_verified")
    runtime.close()


def test_macos_diagnostic_paths_are_separate_from_application_settings(tmp_path: Path) -> None:
    assert default_diagnostics_path(platform_name="darwin", home=tmp_path) == (
        tmp_path / "Library" / "Application Support" / "RBS Desktop" / "diagnostics.json"
    )
    assert default_log_directory(platform_name="darwin", home=tmp_path) == (
        tmp_path / "Library" / "Logs" / "RBS Desktop"
    )


def test_verbose_preference_is_private_atomic_and_recovers_from_invalid_json(
    tmp_path: Path,
) -> None:
    path = tmp_path / "support" / "diagnostics.json"
    path.parent.mkdir()
    path.write_text('{"verbose_logging":"Alice"}', encoding="utf-8")
    settings = DesktopDiagnosticsSettingsFile(path)

    assert not settings.verbose_logging
    assert settings.error_code == "diagnostics_invalid"
    assert "Alice" in path.read_text(encoding="utf-8")

    settings.set_verbose(True)

    assert DesktopDiagnosticsSettingsFile(path).verbose_logging
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "verbose_logging": True,
    }
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_retention_removes_old_inactive_logs_but_not_a_live_run(tmp_path: Path) -> None:
    old_run = str(uuid.uuid4())
    live_run = str(uuid.uuid4())
    old = tmp_path / f"rbs-{old_run}-app-100.jsonl"
    live = tmp_path / f"rbs-{live_run}-app-{os.getpid()}.jsonl"
    old.write_bytes(b"x" * 20)
    live.write_bytes(b"x" * 20)
    marker = tmp_path / f".active-{live_run}-{os.getpid()}"
    marker.touch()
    old_time = time.time() - 20 * 24 * 60 * 60
    os.utime(old, (old_time, old_time))
    os.utime(live, (old_time, old_time))

    result = prune_logs(tmp_path, max_bytes=1)

    assert result.removed_count == 1
    assert not old.exists()
    assert live.exists()


def test_export_bundle_contains_only_complete_valid_jsonl_and_safe_manifest(
    tmp_path: Path,
) -> None:
    logs = tmp_path / "logs"
    runtime = configure_logging(
        LoggingConfig(
            runtime="desktop",
            component="application",
            destination="desktop",
            log_directory=logs,
        )
    )
    destination = tmp_path / "support.zip"
    try:
        get_logger("desktop").info("application.started")
        runtime.flush()
        active = next(logs.glob("*.jsonl"))
        with active.open("ab") as stream:
            stream.write(b'{"schema_version":1,"service":"rbs","partial":')
        result = export_log_bundle(
            logs,
            destination,
            run_id=runtime.run_id,
            runtime=runtime,
        )
    finally:
        runtime.close()

    assert result.file_count == 1
    with zipfile.ZipFile(destination) as archive:
        assert archive.namelist()[0] == "manifest.json"
        manifest = json.loads(archive.read("manifest.json"))
        payload = archive.read(f"logs/{manifest['files'][0]['name']}")
    assert manifest["schema_version"] == 1
    assert manifest["run_id"] == runtime.run_id
    assert "hostname" not in manifest
    assert "username" not in manifest
    assert str(tmp_path) not in json.dumps(manifest)
    assert len(payload.splitlines()) == 1
    assert json.loads(payload)["event"] == "application.started"


def test_service_toggles_and_exports_without_returning_a_path(tmp_path: Path) -> None:
    class Dialogs:
        async def choose_log_export_path(self, suggested_name: str):
            assert suggested_name.startswith("rbs-logs-")
            return tmp_path / "bundle"

    settings = DesktopDiagnosticsSettingsFile(tmp_path / "diagnostics.json")
    runtime = configure_logging(
        LoggingConfig(
            runtime="desktop",
            component="application",
            destination="desktop",
            log_directory=tmp_path / "logs",
        )
    )
    service = DesktopDiagnosticsService(runtime, settings, tmp_path / "logs", Dialogs())
    try:
        assert service.toggle_verbose()
        result = asyncio.run(service.export_logs())
    finally:
        runtime.close()

    assert result is not None
    assert result.path == (tmp_path / "bundle.zip").resolve()
    assert result.path.is_file()


def test_macos_menu_import_is_lazy_and_configuration_uses_native_help_menu() -> None:
    before = set(sys.modules)
    from rbs.desktop.macos.menu import (
        DEBUG_MODE_TITLE,
        EXPORT_LOGS_TITLE,
        HELP_MENU_TITLE,
        MacOSDiagnosticsMenu,
    )

    assert "AppKit" not in set(sys.modules) - before
    assert "objc" not in set(sys.modules) - before
    native = SimpleNamespace(start_args={})

    MacOSDiagnosticsMenu(
        log_directory=Path("/tmp/rbs-logs"),
        run_id=str(uuid.uuid4()),
        verbose_logging=True,
    ).install(native)

    menu = native.start_args["menu"][0]
    assert menu.title == HELP_MENU_TITLE
    assert menu.items[0].title == EXPORT_LOGS_TITLE
    assert menu.items[2].title == DEBUG_MODE_TITLE
    assert callable(native.start_args["func"])


def test_macos_verbose_check_changes_only_after_parent_confirmation(monkeypatch) -> None:
    from rbs.desktop.macos import menu

    states: list[bool] = []
    monkeypatch.setattr(menu, "_set_verbose_check", states.append)
    monkeypatch.setattr(menu, "_show_error", lambda _message: None)
    monkeypatch.setattr(
        menu,
        "_request_parent",
        lambda _route: {"status": "error", "verbose": True},
    )
    menu._toggle_verbose()
    assert states == []

    monkeypatch.setattr(
        menu,
        "_request_parent",
        lambda _route: {"status": "ok", "verbose": True},
    )
    menu._toggle_verbose()
    assert states == [True]


def test_spawned_native_menu_bootstraps_logging_before_window_start(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from rbs.desktop.macos.menu import (
        NATIVE_LOG_DIRECTORY_ENV,
        NATIVE_RUN_ID_ENV,
        NATIVE_VERBOSE_ENV,
        MacOSDiagnosticsMenu,
    )

    run_id = str(uuid.uuid4())
    native = SimpleNamespace(start_args={})
    MacOSDiagnosticsMenu(tmp_path, run_id, False).install(native)
    monkeypatch.setenv(NATIVE_LOG_DIRECTORY_ENV, str(tmp_path))
    monkeypatch.setenv(NATIVE_RUN_ID_ENV, run_id)
    monkeypatch.setenv(NATIVE_VERBOSE_ENV, "0")

    child = multiprocessing.get_context("spawn").Process(
        target=_unpickle_native_configuration,
        args=(pickle.dumps(native.start_args),),
    )
    child.start()
    child.join(20)

    assert child.exitcode == 0
    log_file = next(tmp_path.glob("*.jsonl"))
    record = json.loads(log_file.read_text(encoding="utf-8"))
    assert record["runtime"] == "native"
    assert record["run_id"] == run_id
    assert record["event"] == "native.bootstrap_verified"
