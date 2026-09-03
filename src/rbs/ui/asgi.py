"""Small ASGI compatibility guards for the NiceGUI transport."""

from __future__ import annotations


class EarlyDisconnectGuard:
    """Stop an already-disconnected request before Engine.IO translates it.

    Engine.IO's ASGI translator returns an empty environment when the first
    event is a disconnect, then its request handler indexes ``REQUEST_METHOD``.
    Peeking and replaying the first live event keeps normal requests unchanged
    while allowing abandoned polling requests to end quietly.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        first_event = await receive()
        if first_event.get("type") == "http.disconnect":
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [(b"content-length", b"0")],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        if first_event.get("type") == "websocket.disconnect":
            return

        first_event_pending = True

        async def receive_with_replay():
            nonlocal first_event_pending
            if first_event_pending:
                first_event_pending = False
                return first_event
            return await receive()

        await self.app(scope, receive_with_replay, send)


def guard_nicegui_socket_mount(app) -> bool:
    """Wrap NiceGUI's Socket.IO mount once; return whether it was found."""
    for route in app.routes:
        if getattr(route, "path", None) != "/_nicegui_ws":
            continue
        if not isinstance(route.app, EarlyDisconnectGuard):
            route.app = EarlyDisconnectGuard(route.app)
        return True
    return False
