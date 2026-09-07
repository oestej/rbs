"""Open generated PDF exports outside the browser download flow.

Browsers open the export in a new tab; the desktop app hands it to the
user's preferred PDF viewer instead of rendering it inside its own window.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PDF_EXPORT_ROUTE = "/_exports/pdf/{token}"
EXPORT_TTL_SECONDS = 10 * 60

_pending_exports: dict[str, tuple[bytes, str, float]] = {}


def stage_pdf_export(content: bytes, filename: str) -> str:
    """Stash PDF bytes behind an unguessable one-shot token; return its path."""
    _prune_pdf_exports()
    token = secrets.token_urlsafe(24)
    _pending_exports[token] = (bytes(content), filename, time.monotonic())
    return f"/_exports/pdf/{token}"


def take_staged_pdf_export(token: str) -> tuple[bytes, str] | None:
    """Pop a staged export, honouring single use and expiry."""
    _prune_pdf_exports()
    entry = _pending_exports.pop(token, None)
    if entry is None:
        return None
    content, filename, staged_at = entry
    if time.monotonic() - staged_at > EXPORT_TTL_SECONDS:
        return None
    return content, filename


def _prune_pdf_exports() -> None:
    now = time.monotonic()
    expired = [
        token
        for token, (_content, _filename, staged_at) in _pending_exports.items()
        if now - staged_at > EXPORT_TTL_SECONDS
    ]
    for token in expired:
        del _pending_exports[token]


def present_pdf_export(content: bytes, filename: str, *, native: bool) -> str:
    """Open a PDF export: system viewer on desktop, new tab in browsers."""
    if native:
        return open_with_system_viewer(content, filename)
    from nicegui import ui

    ui.navigate.to(stage_pdf_export(content, filename), new_tab=True)
    return filename


def open_with_system_viewer(content: bytes, filename: str) -> str:
    """Write a PDF export and open it in the user's preferred PDF viewer."""
    path = write_export_tempfile(content, filename)
    command = system_viewer_command(path)
    if command is None:
        startfile = getattr(os, "startfile", None)
        if startfile is None:  # pragma: no cover - non-Windows fallback
            raise OSError("no system PDF viewer available")
        startfile(str(path))
    else:
        subprocess.Popen(command)
    return str(path)


def write_export_tempfile(content: bytes, filename: str) -> Path:
    """Write export bytes to a recognisable temp PDF file."""
    stem = Path(filename).stem[:40] or "export"
    handle = tempfile.NamedTemporaryFile(prefix=f"{stem}-", suffix=".pdf", delete=False)
    try:
        handle.write(bytes(content))
    finally:
        handle.close()
    return Path(handle.name)


def system_viewer_command(path: Path) -> list[str] | None:
    """OS command opening path in the default viewer; None means os.startfile."""
    if sys.platform == "darwin":
        return ["open", str(path)]
    if sys.platform == "win32":
        return None
    return ["xdg-open", str(path)]
