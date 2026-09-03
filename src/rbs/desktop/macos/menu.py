"""Native macOS Help menu for desktop diagnostics.

This is the only RBS module allowed to cross into AppKit/PyObjC. The parent
NiceGUI server owns the actual commands; menu callbacks run in pywebview's
spawned process and invoke capability-protected same-origin routes in the page.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rbs.desktop.diagnostics import EXPORT_ROUTE, VERBOSE_ROUTE
from rbs.logging import (
    LoggingConfig,
    configure_logging,
    current_runtime,
    get_logger,
)

HELP_MENU_TITLE = "Help"
EXPORT_LOGS_TITLE = "Export Logs…"
DEBUG_MODE_TITLE = "Debug Mode"
NATIVE_LOG_DIRECTORY_ENV = "RBS_DESKTOP_NATIVE_LOG_DIRECTORY"
NATIVE_RUN_ID_ENV = "RBS_DESKTOP_NATIVE_RUN_ID"
NATIVE_VERBOSE_ENV = "RBS_DESKTOP_NATIVE_VERBOSE"


@dataclass(frozen=True, slots=True)
class MacOSDiagnosticsMenu:
    """Picklable pywebview configuration for one macOS native child."""

    log_directory: Path
    run_id: str
    verbose_logging: bool

    def install(self, native_config: Any) -> None:
        # pywebview's generic MenuAction has no checked-state API. It is safe to
        # use it for wiring here; the checkmark itself is applied through the
        # isolated AppKit helper after the Cocoa menu exists.
        from webview.menu import Menu, MenuAction, MenuSeparator

        native_config.start_args["menu"] = [
            Menu(
                HELP_MENU_TITLE,
                [
                    MenuAction(EXPORT_LOGS_TITLE, _export_logs),
                    MenuSeparator(),
                    MenuAction(DEBUG_MODE_TITLE, _toggle_verbose),
                ],
            )
        ]
        native_config.start_args["func"] = _native_started
        native_config.start_args["args"] = (
            str(self.log_directory),
            self.run_id,
            self.verbose_logging,
        )


def _native_started(log_directory: str, run_id: str, verbose_logging: bool) -> None:
    """Initialize child-process logging and the initial native checkmark."""
    runtime = current_runtime()
    if runtime is None:
        runtime = configure_logging(
            LoggingConfig(
                runtime="native",
                component="webview",
                destination="desktop",
                level="DEBUG" if verbose_logging else "INFO",
                run_id=run_id,
                log_directory=Path(log_directory),
            )
        )
    else:
        runtime.set_level("DEBUG" if verbose_logging else "INFO")
    logger = get_logger("desktop.native")
    logger.info("native.started")
    window = _active_window()
    if window is None or not window.events.loaded.wait(30):
        logger.error("native.page_unavailable", error_code="startup_timeout")
        runtime.close()
        return
    _set_verbose_check(verbose_logging)

    def close_logging() -> None:
        logger.info("native.stopped")
        runtime.close()

    window.events.closed += close_logging


def _export_logs() -> None:
    result = _request_parent(EXPORT_ROUTE)
    if result.get("status") in {"ok", "cancelled"}:
        return
    _show_error("RBS could not export the diagnostic logs.")


def _toggle_verbose() -> None:
    result = _request_parent(VERBOSE_ROUTE)
    if result.get("status") == "ok" and isinstance(result.get("verbose"), bool):
        enabled = result["verbose"]
        runtime = current_runtime()
        if runtime is not None:
            runtime.set_level("DEBUG" if enabled else "INFO")
        _set_verbose_check(enabled)
        return
    _show_error("RBS could not change the debug mode setting.")


def _request_parent(route: str) -> dict[str, Any]:
    """Run a same-origin fetch inside the authenticated desktop page."""
    window = _active_window()
    if window is None:
        return {"status": "error"}
    script = f"""
      (async () => {{
        try {{
          const response = await fetch({json.dumps(route)}, {{
            method: 'POST',
            credentials: 'same-origin',
            headers: {{'Content-Type': 'application/json'}},
            body: '{{}}',
          }});
          if (!response.ok) return {{status: 'error'}};
          return await response.json();
        }} catch (_) {{
          return {{status: 'error'}};
        }}
      }})()
    """
    completed = threading.Event()
    response: dict[str, Any] = {"status": "error"}

    def receive(value: Any) -> None:
        nonlocal response
        if isinstance(value, dict):
            response = value
        completed.set()

    try:
        window.evaluate_js(script, callback=receive)
    except Exception:
        get_logger("desktop.native").exception("native.command_failed")
        return response
    if not completed.wait(10 * 60):
        get_logger("desktop.native").error(
            "native.command_failed",
            error_code="parent_timeout",
        )
    return response


def _active_window():
    import webview

    return webview.windows[0] if webview.windows else None


def _set_verbose_check(enabled: bool) -> None:
    """Set a native NSMenuItem state on Cocoa's main thread."""
    from AppKit import NSApplication, NSControlStateValueOff, NSControlStateValueOn
    from PyObjCTools import AppHelper

    def apply() -> None:
        menu = NSApplication.sharedApplication().mainMenu()
        help_item = None if menu is None else menu.itemWithTitle_(HELP_MENU_TITLE)
        submenu = None if help_item is None else help_item.submenu()
        item = None if submenu is None else submenu.itemWithTitle_(DEBUG_MODE_TITLE)
        if item is not None:
            item.setState_(NSControlStateValueOn if enabled else NSControlStateValueOff)

    AppHelper.callAfter(apply)


def _show_error(message: str) -> None:
    """Present a constant, privacy-safe native alert on Cocoa's main thread."""
    from AppKit import NSAlert, NSApplication, NSWarningAlertStyle
    from PyObjCTools import AppHelper

    def show() -> None:
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_(message)
        alert.setInformativeText_("Try again, or restart RBS Desktop if the problem continues.")
        alert.setAlertStyle_(NSWarningAlertStyle)
        alert.addButtonWithTitle_("OK")
        alert.runModal()

    AppHelper.callAfter(show)


def _bootstrap_spawned_native_logging() -> None:
    """Configure before pywebview initializes when this module is unpickled.

    NiceGUI sends the menu's top-level callbacks through multiprocessing. A
    spawned child imports this module while unpickling them, before its
    ``_open_window`` target begins. The main process deliberately does nothing.
    """
    import multiprocessing
    import os

    if multiprocessing.current_process().name == "MainProcess":
        return
    directory = os.environ.get(NATIVE_LOG_DIRECTORY_ENV)
    run_id = os.environ.get(NATIVE_RUN_ID_ENV)
    verbose = os.environ.get(NATIVE_VERBOSE_ENV)
    if directory is None or run_id is None or verbose not in {"0", "1"}:
        return
    if current_runtime() is None:
        configure_logging(
            LoggingConfig(
                runtime="native",
                component="webview",
                destination="desktop",
                level="DEBUG" if verbose == "1" else "INFO",
                run_id=run_id,
                log_directory=Path(directory),
            )
        )


_bootstrap_spawned_native_logging()


__all__ = [
    "MacOSDiagnosticsMenu",
    "NATIVE_LOG_DIRECTORY_ENV",
    "NATIVE_RUN_ID_ENV",
    "NATIVE_VERBOSE_ENV",
]
