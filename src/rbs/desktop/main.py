"""Entry point for the self-contained RBS Desktop application."""

from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from rbs import __version__

DEFAULT_WINDOW_SIZE = (1440, 900)
DESKTOP_PRODUCT_NAME = "RBS Desktop"


@dataclass(frozen=True, slots=True)
class DesktopOptions:
    """Startup options which are independent of NiceGUI and PyInstaller."""

    document: Path | None
    state_db: Path
    recovery_path: Path | None = None
    recovery_sources: tuple[Path, ...] = ()
    settings_path: Path | None = None
    diagnostics_path: Path | None = None
    log_directory: Path | None = None
    remove_state_db_on_exit: bool = False


def build_parser(*, state_db: Path | None = None) -> argparse.ArgumentParser:
    """Build the user-facing parser without importing the UI stack."""
    parser = argparse.ArgumentParser(
        prog=DESKTOP_PRODUCT_NAME,
        description="Open an RBS workspace in the native desktop application.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{DESKTOP_PRODUCT_NAME} {__version__}",
    )
    parser.add_argument(
        "document",
        nargs="?",
        type=_rbsc_path,
        help="optional .rbsc workspace to open",
    )
    # This is intentionally suppressed: it is a diagnostic and test seam, not
    # a second user-facing persistence model. Normal runs allocate an isolated
    # temporary editing database.
    parser.add_argument(
        "--state-db",
        type=Path,
        default=state_db,
        help=argparse.SUPPRESS,
    )
    return parser


def parse_arguments(
    argv: Sequence[str],
    *,
    state_db: Path | None = None,
    recovery_root: Path | None = None,
    settings_path: Path | None = None,
) -> DesktopOptions:
    """Parse a normal desktop launch, including a document-open event."""
    # Older macOS launch services may add a process-serial-number argument.
    # Ignoring it here keeps direct and test launches equally robust.
    cleaned = [value for value in argv if not value.startswith("-psn_")]
    args = build_parser(state_db=state_db).parse_args(cleaned)
    configured_state_db = args.state_db
    resolved_state_db = Path(configured_state_db or desktop_state_db()).expanduser().resolve()
    if settings_path is None and configured_state_db is None:
        from rbs.desktop.settings import default_settings_path

        resolved_settings_path = default_settings_path()
    else:
        resolved_settings_path = (
            None if settings_path is None else Path(settings_path).expanduser().resolve()
        )
    from rbs.desktop.diagnostics import default_diagnostics_path, default_log_directory

    if configured_state_db is None:
        diagnostics_path = default_diagnostics_path()
        log_directory = default_log_directory()
    else:
        diagnostics_path = resolved_state_db.parent / "diagnostics.json"
        log_directory = resolved_state_db.parent / "Logs"
    manages_recovery = configured_state_db is None or recovery_root is not None
    if manages_recovery:
        from rbs.desktop.recovery import (
            allocate_recovery_path,
            default_recovery_directory,
            recoverable_drafts,
        )

        root = (
            default_recovery_directory()
            if recovery_root is None
            else Path(recovery_root).expanduser().resolve()
        )
        recovery_path = allocate_recovery_path(root)
        recovery_sources = recoverable_drafts(root)
    else:
        recovery_sources = ()
        recovery_path = None
    return DesktopOptions(
        document=args.document,
        state_db=resolved_state_db,
        recovery_path=recovery_path,
        recovery_sources=recovery_sources,
        settings_path=resolved_settings_path,
        diagnostics_path=diagnostics_path,
        log_directory=log_directory,
        remove_state_db_on_exit=configured_state_db is None,
    )


def desktop_state_db(
    *,
    temp_parent: Path | None = None,
) -> Path:
    """Allocate an isolated editing database for one desktop process.

    The SQLite store is intentionally ephemeral and unique. This prevents two
    windows from sharing mutable state and keeps the user's ``.rbsc`` file as
    the only durable record.
    """
    parent = None if temp_parent is None else str(temp_parent)
    session_dir = Path(tempfile.mkdtemp(prefix="rbs-desktop-", dir=parent))
    return session_dir / "session.sqlite"


def remove_desktop_state_db(path: str | Path) -> None:
    """Delete one owned desktop editing database and its SQLite sidecars.

    The containing directory is removed only when it is empty. Callers decide
    whether they own the database; this function never recursively removes a
    directory or touches adjacent files.
    """
    database = Path(path).expanduser().resolve()
    for suffix in ("-wal", "-shm", "-journal", ""):
        Path(f"{database}{suffix}").unlink(missing_ok=True)
    try:
        database.parent.rmdir()
    except OSError:
        pass


def run_desktop(options: DesktopOptions) -> int:
    """Build the local host and hand it to the shared native UI server.

    Document ownership is deliberately isolated in :func:`_build_runtime`.
    The browser and hosted packagings continue to use the shared UI without
    importing pywebview or adopting desktop filesystem semantics.
    """
    from rbs.desktop.diagnostics import (
        DesktopDiagnosticsService,
        DesktopDiagnosticsSettingsFile,
        prune_logs,
    )
    from rbs.logging import LoggingConfig, configure_logging, get_logger

    log_directory = (options.log_directory or options.state_db.parent / "Logs").resolve()
    diagnostics_path = (
        options.diagnostics_path or options.state_db.parent / "diagnostics.json"
    ).resolve()
    diagnostics_settings = DesktopDiagnosticsSettingsFile(diagnostics_path)
    runtime = configure_logging(
        LoggingConfig(
            runtime="desktop",
            component="application",
            destination="desktop",
            level="DEBUG" if diagnostics_settings.verbose_logging else "INFO",
            log_directory=log_directory,
        )
    )
    logger = get_logger("desktop")
    native_environment: dict[str, str | None] = {}
    host = None
    clean_exit = False
    try:
        logger.info("application.started")
        if diagnostics_settings.error_code is not None:
            logger.warning(
                "logging.preference_invalid",
                error_code=diagnostics_settings.error_code,
            )
        retention = prune_logs(log_directory)
        if retention.removed_count:
            logger.info(
                "logging.retention_completed",
                removed_count=retention.removed_count,
                size_bytes=retention.removed_bytes,
            )

        host = _build_runtime(options)
        logger.info("database.initialized")

        # NiceGUI is intentionally imported only for a normal desktop process. A
        # solver worker must never load the web/native window stack.
        from nicegui import app as nicegui_app
        from nicegui.native import find_open_port

        from rbs.desktop.capability import DesktopCapability
        from rbs.ui.app import serve

        # NiceGUI does not currently bridge pywebview's synchronous closing event,
        # so the browser beforeunload hook cannot run a Save/Discard/Cancel flow.
        # Keep the toolkit confirmation as the first guard; persistent atomic
        # SQLite checkpoints restore the open workspace after a crash and are
        # removed after an orderly exit.
        nicegui_app.native.window_args.setdefault("confirm_close", True)

        # NiceGUI normally chooses the native port inside ui.run and opens its bare
        # root URL. Select the same kind of ephemeral loopback port up front so we
        # can override that URL with a per-launch capability. The override is a
        # plain string and therefore crosses NiceGUI's spawned pywebview process.
        port = find_open_port()
        capability = DesktopCapability.create(port)
        capability.install(nicegui_app)
        nicegui_app.native.window_args["url"] = capability.bootstrap_url

        dialogs = NiceGuiNativeFileDialogs()
        diagnostics = DesktopDiagnosticsService(
            runtime,
            diagnostics_settings,
            log_directory,
            dialogs,
        )
        diagnostics.install(nicegui_app)
        if sys.platform == "darwin":
            from rbs.desktop.macos.menu import (
                NATIVE_LOG_DIRECTORY_ENV,
                NATIVE_RUN_ID_ENV,
                NATIVE_VERBOSE_ENV,
                MacOSDiagnosticsMenu,
            )

            native_values = {
                NATIVE_LOG_DIRECTORY_ENV: str(log_directory),
                NATIVE_RUN_ID_ENV: runtime.run_id,
                NATIVE_VERBOSE_ENV: "1" if diagnostics.verbose_logging else "0",
            }
            native_environment = {key: os.environ.get(key) for key in native_values}
            os.environ.update(native_values)

            MacOSDiagnosticsMenu(
                log_directory=log_directory,
                run_id=runtime.run_id,
                verbose_logging=diagnostics.verbose_logging,
            ).install(nicegui_app.native)

        serve(
            host,
            title=_window_title(host.document_io.path),
            port=port,
            native=True,
            window_size=DEFAULT_WINDOW_SIZE,
            show=False,
            reload=False,
            exit_abruptly=True,
        )
        clean_exit = True
        return 0
    except Exception:
        logger.exception("application.failed")
        raise
    finally:
        if clean_exit and host is not None:
            documents = host.document_io
            if (
                documents is not None
                and not documents.clear_recovery_checkpoint()
            ):
                logger.warning("recovery.cleanup_failed")
        if options.remove_state_db_on_exit:
            try:
                if host is not None:
                    principal = host.principal(None)
                    if principal is not None:
                        host.store_for(principal).invalidate(
                            "desktop application has closed"
                        )
                remove_desktop_state_db(options.state_db)
            except OSError:
                logger.warning("database.cleanup_failed", exc_info=True)
        logger.info("application.stopped")
        runtime.close()
        for key, previous in native_environment.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def _window_title(document: Path | None) -> str:
    return (
        DESKTOP_PRODUCT_NAME
        if document is None
        else f"{document.name} — {DESKTOP_PRODUCT_NAME}"
    )


def _build_runtime(options: DesktopOptions):
    """Construct today's local runtime behind one replaceable integration seam.

    The SQLite file is ephemeral application state, never the portable record.
    A supplied ``.rbsc`` replaces that editing state atomically after complete
    validation.  Native open/save dialogs and path ownership plug in here via
    ``DesktopDocuments`` without changing startup or frozen-worker dispatch.
    """
    from rbs.desktop.documents import DesktopDocumentController
    from rbs.desktop.settings import DesktopSettingsFile, detected_solver_workers
    from rbs.store import Store
    from rbs.ui.host import LocalHost

    options.state_db.parent.mkdir(parents=True, exist_ok=True)
    store = Store(options.state_db)
    store.init()
    application_settings = DesktopSettingsFile(
        options.settings_path,
        automatic_num_workers=detected_solver_workers(),
    )
    documents = DesktopDocumentController(
        store,
        NiceGuiNativeFileDialogs(),
        recovery_path=options.recovery_path,
        application_settings=application_settings,
        lock_directory=(
            None
            if options.recovery_path is None
            else options.recovery_path.parent.parent / "Locks"
        ),
    )
    host = LocalHost(store, document_io=documents)
    if options.document is not None:
        documents.load(options.document)
    else:
        recovered = False
        for recovery_source in options.recovery_sources:
            try:
                documents.restore_recovery(recovery_source)
            except Exception:
                continue
            recovered = True
            break
        if not recovered and documents.workspace is not None:
            documents.close()
    return host


class NiceGuiNativeFileDialogs:
    """Bridge the document controller to pywebview's native file pickers.

    NiceGUI and pywebview remain lazy imports so solver children, hosted
    deployments, and packaging probes do not load a window toolkit.
    """

    def __init__(self, *, workspace_directory: str | Path | None = None) -> None:
        self.workspace_directory = (
            Path.home() / "Documents"
            if workspace_directory is None
            else Path(workspace_directory).expanduser().resolve()
        )

    async def choose_open_path(self) -> Path | None:
        selected = await _native_window().create_file_dialog(
            dialog_type=_file_dialog_type("OPEN"),
            directory=str(self.workspace_directory),
            allow_multiple=False,
            file_types=("RBS Workspace (*.rbsc)",),
        )
        return _selected_path(selected)

    async def choose_save_path(self, suggested_name: str) -> Path | None:
        selected = await _native_window().create_file_dialog(
            dialog_type=_file_dialog_type("SAVE"),
            directory=str(self.workspace_directory),
            save_filename=suggested_name,
            file_types=("RBS Workspace (*.rbsc)",),
        )
        return _selected_path(selected)

    async def choose_settings_open_path(self) -> Path | None:
        selected = await _native_window().create_file_dialog(
            dialog_type=_file_dialog_type("OPEN"),
            allow_multiple=False,
            file_types=("RBS Settings (*.json)",),
        )
        return _selected_path(selected)

    async def choose_settings_save_path(self, suggested_name: str) -> Path | None:
        selected = await _native_window().create_file_dialog(
            dialog_type=_file_dialog_type("SAVE"),
            save_filename=suggested_name,
            file_types=("RBS Settings (*.json)",),
        )
        return _selected_path(selected)

    async def choose_log_export_path(self, suggested_name: str) -> Path | None:
        selected = await _native_window().create_file_dialog(
            dialog_type=_file_dialog_type("SAVE"),
            save_filename=suggested_name,
            file_types=("ZIP Archive (*.zip)",),
        )
        return _selected_path(selected)


def _native_window():
    from nicegui import app

    window = app.native.main_window
    if window is None:
        raise RuntimeError("native file dialogs require the RBS desktop window")
    return window


def _file_dialog_type(name: str):
    """Return the pywebview 5 or 6 spelling of a dialog type."""
    import webview

    file_dialog = getattr(webview, "FileDialog", None)
    if file_dialog is not None:
        return getattr(file_dialog, name)
    return getattr(webview, f"{name}_DIALOG")


def _selected_path(selected: object) -> Path | None:
    """Normalize the return shapes used across pywebview 5 and 6."""
    if selected is None:
        return None
    if isinstance(selected, (str, Path)):
        value = str(selected).strip()
    else:
        values = list(selected)  # type: ignore[arg-type]
        value = str(values[0]).strip() if values else ""
    return Path(value) if value else None


def main(argv: Sequence[str] | None = None) -> int:
    """Launch the native application after PyInstaller child dispatch."""
    # PyInstaller multiprocessing children must be diverted before importing
    # NiceGUI, pywebview, OR-Tools, or application modules with side effects.
    multiprocessing.freeze_support()

    values = list(sys.argv[1:] if argv is None else argv)
    return run_desktop(parse_arguments(values))


def _rbsc_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.suffix.lower() != ".rbsc":
        raise argparse.ArgumentTypeError("workspace files must use the .rbsc extension")
    return path.resolve()


if __name__ == "__main__":
    raise SystemExit(main())
