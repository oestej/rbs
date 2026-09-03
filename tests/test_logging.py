from __future__ import annotations

import asyncio
import io
import json
import logging
import uuid

from rbs.logging import (
    LoggingConfig,
    LoggingContextMiddleware,
    configure_logging,
    get_logger,
    log_context,
    relay_solver_stderr,
)


def _runtime(stream: io.StringIO, *, level: str = "DEBUG"):
    return configure_logging(
        LoggingConfig(
            runtime="cli",
            component="test",
            destination="stderr",
            level=level,
            stream=stream,
        )
    )


def _records(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_application_and_stdlib_logs_share_one_versioned_json_schema() -> None:
    stream = io.StringIO()
    runtime = _runtime(stream)
    try:
        get_logger("example").info(
            "solver.completed",
            duration_ms=12,
            private_payload="must be dropped",
        )
        logging.getLogger("dependency.example").warning("dependency warning")
    finally:
        runtime.close()

    records = _records(stream)
    assert [record["event"] for record in records] == [
        "solver.completed",
        "dependency.log",
    ]
    assert {record["schema_version"] for record in records} == {1}
    assert {record["service"] for record in records} == {"rbs"}
    assert {record["runtime"] for record in records} == {"cli"}
    assert {record["run_id"] for record in records} == {runtime.run_id}
    assert records[0]["duration_ms"] == 12
    assert "private_payload" not in records[0]


def test_privacy_processor_redacts_foreign_text_and_omits_exception_messages() -> None:
    stream = io.StringIO()
    runtime = _runtime(stream)
    try:
        logging.getLogger("dependency.example").error(
            "alice@example.org /Users/alice/Documents/case.rbsc "
            "https://example.test/open?_rbs_capability=secret"
        )
        try:
            raise ValueError("resident Alice and secret-token")
        except ValueError:
            get_logger("example").exception("document.open_failed")
    finally:
        runtime.close()

    rendered = stream.getvalue()
    assert "alice@example.org" not in rendered
    assert "/Users/alice" not in rendered
    assert "case.rbsc" not in rendered
    assert "_rbs_capability=secret" not in rendered
    assert "resident Alice" not in rendered
    assert "secret-token" not in rendered
    records = _records(stream)
    assert records[1]["exception"]["type"] == "ValueError"
    assert "message" not in records[1]["exception"]


def test_context_accepts_only_canonical_opaque_identifiers_and_does_not_leak() -> None:
    stream = io.StringIO()
    runtime = _runtime(stream)
    request_id = str(uuid.uuid4())
    try:
        with log_context(request_id=request_id, session_id="a-persons-name"):
            get_logger("example").info("request.completed")
        get_logger("example").info("request.completed")
    finally:
        runtime.close()

    first, second = _records(stream)
    assert first["request_id"] == request_id
    assert "session_id" not in first
    assert "request_id" not in second


def test_invalid_level_falls_back_to_info_without_echoing_the_value() -> None:
    stream = io.StringIO()
    runtime = _runtime(stream, level="super-secret-value")
    try:
        get_logger("example").debug("debug.hidden")
        get_logger("example").info("info.visible")
    finally:
        runtime.close()

    assert [record["event"] for record in _records(stream)] == [
        "logging.level_invalid",
        "info.visible",
    ]
    assert "super-secret-value" not in stream.getvalue()


def test_unstructured_solver_stderr_is_fingerprinted_not_copied() -> None:
    stream = io.StringIO()
    runtime = _runtime(stream)
    try:
        relay_solver_stderr(b"resident Alice <alice@example.org>", exit_code=1)
    finally:
        runtime.close()

    rendered = stream.getvalue()
    assert "resident Alice" not in rendered
    assert "alice@example.org" not in rendered
    record = _records(stream)[0]
    assert record["event"] == "solver.stderr_rejected"
    assert record["stderr_bytes"] > 0
    assert len(record["stderr_sha256"]) == 64


def test_reconfiguration_replaces_the_rbs_handler_instead_of_duplicating() -> None:
    first = io.StringIO()
    second = io.StringIO()
    first_runtime = _runtime(first)
    second_runtime = _runtime(second)
    try:
        get_logger("example").info("application.started")
    finally:
        second_runtime.close()
        first_runtime.close()

    assert first.getvalue() == ""
    assert len(_records(second)) == 1


def test_application_logger_is_silent_without_a_configured_runtime(capsys) -> None:
    stream = io.StringIO()
    runtime = _runtime(stream)
    runtime.close()

    get_logger("example").warning("application.not_configured")

    assert capsys.readouterr() == ("", "")
    assert stream.getvalue() == ""


def test_asgi_context_generates_a_fresh_request_id_and_clears_it_afterward() -> None:
    stream = io.StringIO()
    runtime = _runtime(stream)

    async def app(_scope, _receive, _send) -> None:
        get_logger("http").info("request.selected_operation")

    async def receive() -> dict:
        return {"type": "http.request", "body": b""}

    async def send(_message: dict) -> None:
        pass

    middleware = LoggingContextMiddleware(app)
    try:
        asyncio.run(middleware({"type": "http"}, receive, send))
        asyncio.run(middleware({"type": "http"}, receive, send))
        get_logger("http").info("request.outside")
    finally:
        runtime.close()

    first, second, outside = [
        record for record in _records(stream) if record["logger"] == "rbs.http"
    ]
    assert first["request_id"] != second["request_id"]
    assert "request_id" not in outside
