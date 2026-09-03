"""NiceGUI workspace: edit instance, run solver, persist workspaces."""

from __future__ import annotations

import os
import signal

from fastapi import Request, Response

from rbs.logging import (
    LoggingConfig,
    configure_logging,
    current_runtime,
    get_logger,
    install_asgi_logging,
)
from rbs.models.workspace import WorkspaceConflictError
from rbs.repository import WorkspaceRepository
from rbs.ui.app_branding import (
    BLOCK_LABEL_FIT_SCRIPT,
    DISMISS_LOADING_SCREEN_SCRIPT,
    FAVICON_URL,
    LOADING_SCREEN_HTML,
    RECONNECT_BRANDING_HTML,
    SPINNER_ELAPSED_SCRIPT,
    STATIC_DIR,
    STATIC_URL,
    STYLESHEET_URLS,
)
from rbs.ui.app_documents import (
    _document_io,
    _notify_recovery_error,
)
from rbs.ui.app_shell import (
    _mount_shell,
    _render_tab,
)
from rbs.ui.app_status import (
    _refresh_status_chips,
)
from rbs.ui.asgi import guard_nicegui_socket_mount
from rbs.ui.diagnostics import CLIENT_ERROR_SCRIPT, install_client_error_endpoint
from rbs.ui.host import WorkspaceHost
from rbs.ui.legal_notices import load_application_license, load_third_party_licenses
from rbs.ui.preview import build_host
from rbs.ui.release_notes import load_release_notes
from rbs.ui.session import WorkspaceSession
from rbs.ui.workspaces import file_handle
from rbs.ui.workspaces.file_handle import (
    PAYLOAD_HEADER,
    PAYLOAD_MARKER,
    WORKSPACE_DOWNLOAD_ROUTE,
    workspace_filename,
)

# Public composition surface. Import any other helper from its defining
# ``app_*`` module instead of from here — except the three bundled-document
# loaders below, which are deliberately re-exported: ``app_documents``
# resolves them through this module so tests and alternate packagings can
# replace the loader at one stable seam.
__all__ = [
    "DEFAULT_DB",
    "export_workspace_response",
    "load_application_license",
    "load_release_notes",
    "load_third_party_licenses",
    "run_app",
    "serve",
]

DEFAULT_DB = "rbs.sqlite"


def run_app(
    store: WorkspaceRepository,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    reload: bool = False,
    show: bool = True,
    mode: str = "local",
) -> None:
    """Run the single-user UI against one workspace repository.

    ``mode`` previews another packaging's chrome on the same local stack:
    ``"desktop"`` renders the native-document UI with inert file actions and
    ``"cloud"`` renders the hosted product chrome without its infrastructure.
    """
    owned_runtime = current_runtime() is None
    runtime = (
        configure_logging(LoggingConfig(runtime="local", component="ui", destination="stdout"))
        if owned_runtime
        else current_runtime()
    )
    logger = get_logger("ui")
    try:
        logger.info("application.started", mode=mode)
        local = build_host(store, mode)
        local.bootstrap()
        logger.info("database.initialized")
        serve(
            local,
            host=host,
            port=port,
            reload=reload,
            show=show,
            exit_abruptly=True,
        )
    except Exception:
        logger.exception("application.failed")
        raise
    finally:
        logger.info("application.stopped")
        if owned_runtime and runtime is not None:
            runtime.close()


def serve(
    workspace_host: WorkspaceHost,
    *,
    title: str = "RBS",
    host: str = "127.0.0.1",
    port: int | None = 8080,
    reload: bool = False,
    show: bool = True,
    native: bool = False,
    window_size: tuple[int, int] | None = None,
    storage_secret: str | None = None,
    reconnect_timeout: float = 10.0,
    exit_abruptly: bool = False,
) -> None:
    """Serve the workspace UI for one packaging's host.

    ``exit_abruptly`` belongs to the desktop build only. A hosted deployment
    needs its signals to drain in-flight solves and close databases, so it
    leaves the handler alone and installs its own.
    """
    from nicegui import ui

    _register(workspace_host)

    if exit_abruptly:

        def _exit_quietly(_signum, _frame) -> None:
            # Skip Python/C++ unwinding. NiceGUI and native extensions (even with no
            # solve running) abort on SIGINT with std::bad_function_call and leak
            # multiprocessing semaphores.
            os._exit(0)

        signal.signal(signal.SIGINT, _exit_quietly)
        signal.signal(signal.SIGTERM, _exit_quietly)

    run_options = {
        "title": title,
        "host": host,
        "port": port,
        "reload": reload,
        "show": show,
        "dark": False,
        "reconnect_timeout": reconnect_timeout,
        "native": native,
        # RBS owns the root handler and intentionally emits selected
        # operational events instead of privacy-unsafe access lines.
        "show_welcome_message": False,
        "log_config": None,
        "access_log": False,
    }
    if window_size is not None:
        run_options["window_size"] = window_size
    if storage_secret is not None:
        run_options["storage_secret"] = storage_secret
    ui.run(**run_options)


def _register(workspace_host: WorkspaceHost) -> None:
    from nicegui import app, ui

    install_asgi_logging(app)
    install_client_error_endpoint(app)
    guard_nicegui_socket_mount(app)
    app.add_static_files(STATIC_URL, STATIC_DIR)

    @app.get(WORKSPACE_DOWNLOAD_ROUTE)
    async def download_workspace(
        workspace_id: int,
        request: Request,
        revision: int | None = None,
        save_as: bool = False,
    ) -> Response:
        return export_workspace_response(
            workspace_host,
            workspace_id,
            request,
            expected_workspace_revision=revision,
            save_as=save_as,
        )

    @ui.page("/")
    async def index(resident: str | None = None) -> None:
        ui.add_head_html(f"<script>{CLIENT_ERROR_SCRIPT}</script>")
        ui.add_head_html(f'<link rel="icon" type="image/svg+xml" href="{FAVICON_URL}">')
        ui.add_head_html(RECONNECT_BRANDING_HTML)
        for stylesheet_url in STYLESHEET_URLS:
            ui.add_head_html(f'<link rel="stylesheet" href="{stylesheet_url}">')
        ui.add_head_html(f"<script>{BLOCK_LABEL_FIT_SCRIPT}</script>")
        ui.add_head_html(f"<script>{SPINNER_ELAPSED_SCRIPT}</script>")
        file_handle.install(ui)
        # Deliver a small, browser-native loading screen before building the
        # NiceGUI workspace. This covers the interval in which the page is
        # visible but its websocket-backed controls are not interactive yet.
        ui.add_body_html(LOADING_SCREEN_HTML)
        principal = workspace_host.principal(_page_request())
        if principal is None:
            await _render_unauthorized()
            return
        await ui.context.client.connected()
        store = workspace_host.store_for(principal)
        selected_resident = resident.strip() if resident and resident.strip() else None
        session = WorkspaceSession(
            store=store,
            workspace_host=workspace_host,
            principal=principal,
            workspace_id=store.current_id(),
            resident_id=selected_resident,
            active_tab="residents" if selected_resident else "block_schedule",
        )
        session.header = ui.header().classes("rbs-app-header px-4")
        session.body = ui.column().classes("rbs-page w-full p-4 gap-4")
        session._render_tab = _render_tab
        session._mount = _mount_shell
        session._refresh_status = _refresh_status_chips
        session.rebuild()
        documents = _document_io(session)
        if documents is not None and documents.recovered_from is not None:
            ui.notify(
                "Recovered unsaved work from a previous RBS Desktop session. "
                "Choose Save to keep it as an .rbsc file.",
                type="warning",
                multi_line=True,
            )
            documents.recovered_from = None
        if documents is not None:
            _notify_recovery_error(session, documents)
        # Awaiting the script guarantees all preceding element updates have
        # reached the browser before the overlay stops intercepting input.
        await ui.run_javascript(DISMISS_LOADING_SCREEN_SCRIPT, timeout=10.0)


def export_workspace_response(
    workspace_host: WorkspaceHost,
    workspace_id: int,
    request,
    *,
    expected_workspace_revision: int | None = None,
    save_as: bool = False,
) -> Response:
    """Serve one workspace as an ``.rbsc`` document.

    Identity is resolved again here rather than trusted from the open websocket.
    A proxy session can lapse while the socket stays up, and this response is the
    one the browser writes to the user's own disk - so a silent redirect to a
    login page would be indistinguishable from a successful save. The marker
    header is what lets the browser tell the difference.
    """
    principal = workspace_host.principal(request)
    if principal is None:
        get_logger("documents").info("workspace.export_denied", status_code=403)
        return Response(status_code=403)
    store = workspace_host.store_for(principal)
    try:
        workspace = store.get(workspace_id)
        if workspace.is_sample and not save_as:
            get_logger("documents").info(
                "workspace.export_failed",
                status_code=409,
                reason="sample_requires_save_as",
            )
            return Response(status_code=409)
        payload = store.export_workspace_rbsc(
            workspace_id,
            expected_workspace_revision=expected_workspace_revision,
            clear_sample=save_as,
        )
    except KeyError:
        get_logger("documents").info("workspace.export_failed", status_code=404)
        return Response(status_code=404)
    except WorkspaceConflictError:
        get_logger("documents").info("workspace.export_failed", status_code=409)
        return Response(status_code=409)
    filename = workspace_filename(workspace.name, workspace.academic_year)
    get_logger("documents").info("workspace.exported")
    return Response(
        content=payload,
        media_type="application/json",
        headers={
            PAYLOAD_HEADER: PAYLOAD_MARKER,
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def _page_request():
    """The Starlette request backing this page, when there is one.

    NiceGUI raises rather than returning ``None`` for pages built outside a
    request (tests, sub-page rendering), and an absent request must not read as
    an absent principal.
    """
    from nicegui import ui

    try:
        return ui.context.client.request
    except RuntimeError:
        return None


async def _render_unauthorized() -> None:
    """Refuse a caller the host declined, without offering a way to sign in.

    RBS does not authenticate anyone, so there is nothing here to log into. The
    deployment's proxy owns that, and saying so is more useful than a login form
    that cannot exist.
    """
    from nicegui import ui

    await ui.context.client.connected()
    with ui.column().classes("rbs-page w-full items-center justify-center p-8 gap-2"):
        ui.icon("lock").classes("rbs-icon-xl rbs-text-subtle")
        ui.label("Not authorized").classes("rbs-type-page-title")
        ui.label("Access to RBS is granted by your organization, not by this application.").classes(
            "rbs-text-muted"
        )
    await ui.run_javascript(DISMISS_LOADING_SCREEN_SCRIPT, timeout=10.0)
