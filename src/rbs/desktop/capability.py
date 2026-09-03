"""Per-launch access control for the RBS Desktop loopback server.

The native application is still a web application internally: NiceGUI serves
HTTP and websocket traffic on a random loopback port and pywebview renders it.
Loopback binding prevents remote machines from connecting, but it does not
prevent another process running as the same user from discovering the port.

This module adds the second half of that boundary.  A cryptographically random
capability is placed in the *initial native-window URL only*, exchanged for an
HttpOnly session cookie, and required for every subsequent HTTP or websocket
request.  Browser and cloud packagings never install this middleware.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import parse_qs, quote

LOOPBACK_HOST = "127.0.0.1"
CAPABILITY_QUERY = "_rbs_capability"
_SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True, slots=True)
class DesktopCapability:
    """One unguessable authority for one desktop process."""

    token: str
    cookie_name: str
    origin: str

    @classmethod
    def create(cls, port: int) -> DesktopCapability:
        """Generate a capability tied to one loopback origin."""
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        token = secrets.token_urlsafe(32)
        # Cookies are scoped by host rather than port. A distinct name keeps
        # concurrently open desktop windows from replacing each other's token.
        instance = hashlib.sha256(token.encode()).hexdigest()[:16]
        return cls(
            token=token,
            cookie_name=f"rbs_desktop_{instance}",
            origin=f"http://{LOOPBACK_HOST}:{port}",
        )

    @property
    def bootstrap_url(self) -> str:
        """URL opened by pywebview to establish the private session."""
        token = quote(self.token, safe="")
        return f"{self.origin}/?{CAPABILITY_QUERY}={token}"

    def install(self, app: Any) -> None:
        """Wrap a NiceGUI/FastAPI app before its middleware stack starts."""
        app.add_middleware(
            DesktopCapabilityMiddleware,
            token=self.token,
            cookie_name=self.cookie_name,
            origin=self.origin,
        )


class DesktopCapabilityMiddleware:
    """Pure ASGI capability gate covering HTTP and websocket transports."""

    def __init__(
        self,
        app: Any,
        *,
        token: str,
        cookie_name: str,
        origin: str,
    ) -> None:
        self.app = app
        self.token = token
        self.cookie_name = cookie_name
        self.origin = origin

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        if scope_type not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        if self._is_bootstrap(scope):
            await self._bootstrap(send)
            return

        if not self._has_cookie(scope) or not self._has_trusted_origin(scope):
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await self._not_found(send)
            return

        if scope_type == "http":
            await self.app(scope, receive, self._with_security_headers(send))
        else:
            await self.app(scope, receive, send)

    def _is_bootstrap(self, scope: dict) -> bool:
        """Accept the secret exactly once, on the initial root navigation."""
        if (
            scope.get("type") != "http"
            or scope.get("method", "").upper() != "GET"
            or scope.get("path") != "/"
        ):
            return False
        try:
            values = parse_qs(
                scope.get("query_string", b"").decode("ascii"),
                keep_blank_values=True,
                max_num_fields=16,
            ).get(CAPABILITY_QUERY, ())
        except (UnicodeDecodeError, ValueError):
            return False
        return len(values) == 1 and hmac.compare_digest(values[0], self.token)

    def _has_cookie(self, scope: dict) -> bool:
        for raw_name, raw_value in scope.get("headers", ()):
            if raw_name.lower() != b"cookie":
                continue
            try:
                cookies = SimpleCookie(raw_value.decode("latin-1"))
            except CookieError:
                continue
            morsel = cookies.get(self.cookie_name)
            if morsel is not None and hmac.compare_digest(morsel.value, self.token):
                return True
        return False

    def _has_trusted_origin(self, scope: dict) -> bool:
        """Pin sockets and mutations to this instance's exact origin.

        SameSite cookies intentionally ignore ports. Without this check, a
        hostile page served by another local process on 127.0.0.1 could cause
        a browser to attach the cookie to a websocket or mutation request.
        """
        scope_type = scope.get("type")
        method = scope.get("method", "GET").upper()
        origin = self._header(scope, b"origin")
        if origin is not None:
            return hmac.compare_digest(origin, self.origin)
        return scope_type == "http" and method in _SAFE_HTTP_METHODS

    @staticmethod
    def _header(scope: dict, name: bytes) -> str | None:
        for raw_name, raw_value in scope.get("headers", ()):
            if raw_name.lower() == name:
                try:
                    return raw_value.decode("ascii")
                except UnicodeDecodeError:
                    return None
        return None

    async def _bootstrap(self, send: Any) -> None:
        # Redirect immediately so the capability is absent from the page URL,
        # browser history used by the app, referrers, asset URLs, and sockets.
        cookie = (
            f"{self.cookie_name}={self.token}; Path=/; HttpOnly; SameSite=Strict"
        ).encode("ascii")
        await send(
            {
                "type": "http.response.start",
                "status": 303,
                "headers": [
                    (b"location", b"/"),
                    (b"set-cookie", cookie),
                    (b"cache-control", b"no-store"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-length", b"0"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    async def _not_found(self, send: Any) -> None:
        body = b"Not Found"
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def _with_security_headers(send: Any):
        async def send_with_headers(message: dict) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", ()))
                headers.extend(
                    (
                        (b"content-security-policy", b"frame-ancestors 'none'"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"x-frame-options", b"DENY"),
                    )
                )
                message = {**message, "headers": headers}
            await send(message)

        return send_with_headers
