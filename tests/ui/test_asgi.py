import asyncio
from types import SimpleNamespace

from rbs.ui.asgi import EarlyDisconnectGuard, guard_nicegui_socket_mount


def test_early_http_disconnect_does_not_reach_engineio() -> None:
    app_called = False
    sent = []

    async def app(_scope, _receive, _send) -> None:
        nonlocal app_called
        app_called = True

    async def receive() -> dict[str, str]:
        return {"type": "http.disconnect"}

    async def send(event) -> None:
        sent.append(event)

    asyncio.run(EarlyDisconnectGuard(app)({"type": "http"}, receive, send))

    assert not app_called
    assert sent == [
        {
            "type": "http.response.start",
            "status": 204,
            "headers": [(b"content-length", b"0")],
        },
        {"type": "http.response.body", "body": b""},
    ]


def test_live_first_event_is_replayed_unchanged() -> None:
    first_event = {"type": "http.request", "body": b"payload"}
    received = []

    async def app(_scope, receive, _send) -> None:
        received.append(await receive())

    async def receive() -> dict:
        return first_event

    async def send(_event) -> None:
        pass

    asyncio.run(EarlyDisconnectGuard(app)({"type": "http"}, receive, send))

    assert received == [first_event]


def test_guard_wraps_only_the_nicegui_socket_mount_once() -> None:
    socket_app = object()
    socket_route = SimpleNamespace(path="/_nicegui_ws", app=socket_app)
    static_route = SimpleNamespace(path="/_nicegui/static", app=object())
    app = SimpleNamespace(routes=[socket_route, static_route])

    assert guard_nicegui_socket_mount(app)
    wrapped = socket_route.app
    assert isinstance(wrapped, EarlyDisconnectGuard)
    assert wrapped.app is socket_app
    assert guard_nicegui_socket_mount(app)
    assert socket_route.app is wrapped
    assert not isinstance(static_route.app, EarlyDisconnectGuard)
