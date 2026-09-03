"""Access boundary around the native NiceGUI loopback server."""

from __future__ import annotations

import asyncio

from rbs.desktop.capability import (
    CAPABILITY_QUERY,
    DesktopCapability,
    DesktopCapabilityMiddleware,
)

TOKEN = "a" * 43
COOKIE_NAME = "rbs_desktop_test"
ORIGIN = "http://127.0.0.1:8123"


class RecordingApp:
    def __init__(self) -> None:
        self.scopes: list[dict] = []

    async def __call__(self, scope, _receive, send) -> None:
        self.scopes.append(scope)
        if scope["type"] == "websocket":
            await send({"type": "websocket.accept"})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"private"})


def _gate(app: RecordingApp | None = None):
    inner = app or RecordingApp()
    return (
        DesktopCapabilityMiddleware(
            inner,
            token=TOKEN,
            cookie_name=COOKIE_NAME,
            origin=ORIGIN,
        ),
        inner,
    )


def _run(scope: dict, gate: DesktopCapabilityMiddleware) -> list[dict]:
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)

    asyncio.run(gate(scope, receive, send))
    return messages


def _http_scope(
    *,
    method: str = "GET",
    path: str = "/",
    query: bytes = b"",
    cookie: str | None = None,
    origin: str | None = None,
) -> dict:
    headers: list[tuple[bytes, bytes]] = []
    if cookie is not None:
        headers.append((b"cookie", cookie.encode()))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query,
        "headers": headers,
    }


def _websocket_scope(*, cookie: str | None, origin: str | None) -> dict:
    scope = _http_scope(cookie=cookie, origin=origin)
    scope["type"] = "websocket"
    scope.pop("method")
    return scope


def _headers(messages: list[dict]) -> dict[bytes, bytes]:
    return dict(messages[0]["headers"])


def test_capability_is_random_per_launch_and_builds_a_tokenized_loopback_url() -> None:
    first = DesktopCapability.create(8123)
    second = DesktopCapability.create(8123)

    assert first.token != second.token
    assert first.cookie_name != second.cookie_name
    assert first.origin == ORIGIN
    assert first.bootstrap_url.startswith(f"{ORIGIN}/?{CAPABILITY_QUERY}=")


def test_install_registers_the_gate_without_importing_shared_ui() -> None:
    recorded: list[tuple[object, dict]] = []

    class App:
        def add_middleware(self, middleware, **options) -> None:
            recorded.append((middleware, options))

    capability = DesktopCapability(TOKEN, COOKIE_NAME, ORIGIN)
    capability.install(App())

    assert recorded == [
        (
            DesktopCapabilityMiddleware,
            {"token": TOKEN, "cookie_name": COOKIE_NAME, "origin": ORIGIN},
        )
    ]


def test_bootstrap_exchanges_query_capability_for_protected_cookie_and_redirect() -> None:
    gate, inner = _gate()

    messages = _run(
        _http_scope(query=f"{CAPABILITY_QUERY}={TOKEN}".encode()),
        gate,
    )

    assert messages[0]["status"] == 303
    headers = _headers(messages)
    assert headers[b"location"] == b"/"
    assert headers[b"cache-control"] == b"no-store"
    assert TOKEN.encode() not in headers[b"location"]
    assert headers[b"set-cookie"] == (
        f"{COOKIE_NAME}={TOKEN}; Path=/; HttpOnly; SameSite=Strict".encode()
    )
    assert inner.scopes == []


def test_query_capability_is_only_accepted_for_initial_root_get() -> None:
    gate, inner = _gate()
    query = f"{CAPABILITY_QUERY}={TOKEN}".encode()

    post = _run(_http_scope(method="POST", query=query, origin=ORIGIN), gate)
    asset = _run(_http_scope(path="/asset.js", query=query), gate)

    assert post[0]["status"] == 404
    assert asset[0]["status"] == 404
    assert inner.scopes == []


def test_http_requires_the_exact_capability_cookie() -> None:
    gate, inner = _gate()

    missing = _run(_http_scope(path="/private"), gate)
    wrong = _run(_http_scope(path="/private", cookie=f"{COOKIE_NAME}=wrong"), gate)
    allowed = _run(
        _http_scope(path="/private", cookie=f"other=x; {COOKIE_NAME}={TOKEN}"),
        gate,
    )

    assert missing[0]["status"] == 404
    assert wrong[0]["status"] == 404
    assert allowed[0]["status"] == 200
    assert len(inner.scopes) == 1
    headers = _headers(allowed)
    assert headers[b"content-security-policy"] == b"frame-ancestors 'none'"
    assert headers[b"x-frame-options"] == b"DENY"


def test_cross_origin_safe_browser_request_is_rejected() -> None:
    gate, inner = _gate()
    cookie = f"{COOKIE_NAME}={TOKEN}"

    wrong = _run(
        _http_scope(
            method="GET",
            cookie=cookie,
            origin="http://127.0.0.1:9999",
        ),
        gate,
    )
    allowed = _run(
        _http_scope(method="GET", cookie=cookie, origin=ORIGIN),
        gate,
    )

    assert wrong[0]["status"] == 404
    assert allowed[0]["status"] == 200
    assert len(inner.scopes) == 1


def test_unsafe_http_requests_require_the_exact_native_origin() -> None:
    gate, inner = _gate()
    cookie = f"{COOKIE_NAME}={TOKEN}"

    missing = _run(_http_scope(method="POST", cookie=cookie), gate)
    wrong = _run(
        _http_scope(method="POST", cookie=cookie, origin="http://127.0.0.1:9999"),
        gate,
    )
    allowed = _run(_http_scope(method="POST", cookie=cookie, origin=ORIGIN), gate)

    assert missing[0]["status"] == 404
    assert wrong[0]["status"] == 404
    assert allowed[0]["status"] == 200
    assert len(inner.scopes) == 1


def test_websocket_requires_both_cookie_and_exact_native_origin() -> None:
    gate, inner = _gate()
    cookie = f"{COOKIE_NAME}={TOKEN}"

    missing = _run(_websocket_scope(cookie=None, origin=ORIGIN), gate)
    wrong_origin = _run(
        _websocket_scope(cookie=cookie, origin="http://127.0.0.1:9999"), gate
    )
    allowed = _run(_websocket_scope(cookie=cookie, origin=ORIGIN), gate)

    assert missing == [{"type": "websocket.close", "code": 1008}]
    assert wrong_origin == [{"type": "websocket.close", "code": 1008}]
    assert allowed == [{"type": "websocket.accept"}]
    assert len(inner.scopes) == 1


def test_non_network_asgi_lifecycle_events_pass_through() -> None:
    called: list[str] = []

    async def lifecycle_app(scope, _receive, _send) -> None:
        called.append(scope["type"])

    gate = DesktopCapabilityMiddleware(
        lifecycle_app,
        token=TOKEN,
        cookie_name=COOKIE_NAME,
        origin=ORIGIN,
    )

    _run({"type": "lifespan"}, gate)

    assert called == ["lifespan"]
