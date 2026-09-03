"""Privacy-bounded diagnostics reported by the browser runtime."""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections import OrderedDict
from typing import Literal

from pydantic import Field, ValidationError, field_validator
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from rbs.logging import get_logger, log_context
from rbs.models.common import StrictModel

CLIENT_ERROR_ROUTE = "/_rbs/diagnostics/client-error"
MAX_CLIENT_REPORT_BYTES = 2048
MAX_CLIENT_REPORTS_PER_SESSION = 5
_MAX_TRACKED_SESSIONS = 1024
_SESSION_TTL_SECONDS = 60 * 60
_ASSET = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_ERROR_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


class ClientErrorReport(StrictModel):
    """Only metadata that cannot contain an exception message or page state."""

    kind: Literal["error", "unhandledrejection"]
    error_name: str = "Error"
    asset: str | None = None
    line: int | None = Field(default=None, ge=0, le=10_000_000)
    column: int | None = Field(default=None, ge=0, le=10_000_000)
    session_id: str

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        parsed = uuid.UUID(value)
        if str(parsed) != value.lower():
            raise ValueError("session_id must be a canonical UUID")
        return str(parsed)

    @field_validator("error_name")
    @classmethod
    def validate_error_name(cls, value: str) -> str:
        if not _ERROR_NAME.fullmatch(value):
            raise ValueError("error_name is not safe diagnostic metadata")
        return value

    @field_validator("asset")
    @classmethod
    def validate_asset(cls, value: str | None) -> str | None:
        if value is not None and not _ASSET.fullmatch(value):
            raise ValueError("asset must be a basename")
        return value


class ClientErrorLimiter:
    """Bound both reports per page and memory used by attacker-chosen IDs."""

    def __init__(self) -> None:
        self._sessions: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, session_id: str, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        with self._lock:
            while self._sessions:
                _key, (_count, last_seen) = next(iter(self._sessions.items()))
                if current - last_seen <= _SESSION_TTL_SECONDS:
                    break
                self._sessions.popitem(last=False)
            count, _last_seen = self._sessions.pop(session_id, (0, current))
            if count >= MAX_CLIENT_REPORTS_PER_SESSION:
                self._sessions[session_id] = (count, current)
                return False
            self._sessions[session_id] = (count + 1, current)
            while len(self._sessions) > _MAX_TRACKED_SESSIONS:
                self._sessions.popitem(last=False)
            return True


CLIENT_ERROR_SCRIPT = f"""
(() => {{
  if (window.__rbsDiagnosticsInstalled) return;
  window.__rbsDiagnosticsInstalled = true;
  const newSessionId = () => {{
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
    return `${{hex.slice(0, 8)}}-${{hex.slice(8, 12)}}-${{hex.slice(12, 16)}}-` +
      `${{hex.slice(16, 20)}}-${{hex.slice(20)}}`;
  }};
  const sessionId = newSessionId();
  let sent = 0;
  const safeName = value => {{
    const name = String(value || 'Error');
    return /^[A-Za-z][A-Za-z0-9_.-]{{0,63}}$/.test(name) ? name : 'Error';
  }};
  const assetName = source => {{
    if (!source) return null;
    try {{
      const name = new URL(source, window.location.origin).pathname.split('/').pop();
      return /^[A-Za-z0-9_.-]{{1,128}}$/.test(name || '') ? name : null;
    }} catch (_) {{ return null; }}
  }};
  const report = payload => {{
    if (sent >= {MAX_CLIENT_REPORTS_PER_SESSION}) return;
    sent += 1;
    void fetch('{CLIENT_ERROR_ROUTE}', {{
      method: 'POST',
      credentials: 'same-origin',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{...payload, session_id: sessionId}}),
      keepalive: true,
    }}).catch(() => undefined);
  }};
  window.addEventListener('error', event => report({{
    kind: 'error',
    error_name: safeName(event.error && event.error.name),
    asset: assetName(event.filename),
    line: Number.isInteger(event.lineno) ? event.lineno : null,
    column: Number.isInteger(event.colno) ? event.colno : null,
  }}));
  window.addEventListener('unhandledrejection', event => report({{
    kind: 'unhandledrejection',
    error_name: safeName(event.reason && event.reason.name),
    asset: null,
    line: null,
    column: null,
  }}));
}})();
"""


def install_client_error_endpoint(app) -> None:
    """Register the internal endpoint once on the shared NiceGUI app."""
    if getattr(app.state, "rbs_client_error_endpoint_installed", False):
        return
    limiter = ClientErrorLimiter()

    @app.post(CLIENT_ERROR_ROUTE, include_in_schema=False)
    async def report_client_error(request: Request) -> Response:
        fetch_site = request.headers.get("sec-fetch-site")
        if fetch_site not in {None, "same-origin", "none"}:
            return JSONResponse({"status": "rejected"}, status_code=403)
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
                if declared_length < 0:
                    return JSONResponse({"status": "rejected"}, status_code=400)
                if declared_length > MAX_CLIENT_REPORT_BYTES:
                    return JSONResponse({"status": "rejected"}, status_code=413)
            except ValueError:
                return JSONResponse({"status": "rejected"}, status_code=400)
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > MAX_CLIENT_REPORT_BYTES:
                return JSONResponse({"status": "rejected"}, status_code=413)
            payload.extend(chunk)
        try:
            report = ClientErrorReport.model_validate_json(bytes(payload))
        except (ValidationError, ValueError):
            return JSONResponse({"status": "rejected"}, status_code=400)
        if not limiter.allow(report.session_id):
            return Response(status_code=204)
        with log_context(session_id=report.session_id):
            get_logger("browser").warning(
                "browser.unhandled_error",
                kind=report.kind,
                error_name=report.error_name,
                asset=report.asset,
                line=report.line,
                column=report.column,
            )
        return Response(status_code=204)

    app.state.rbs_client_error_endpoint_installed = True


__all__ = [
    "CLIENT_ERROR_ROUTE",
    "CLIENT_ERROR_SCRIPT",
    "ClientErrorLimiter",
    "ClientErrorReport",
    "install_client_error_endpoint",
]
