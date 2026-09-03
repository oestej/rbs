from __future__ import annotations

import asyncio
import io
import json
import uuid

import httpx
from fastapi import FastAPI

from rbs.logging import LoggingConfig, configure_logging
from rbs.ui.diagnostics import (
    CLIENT_ERROR_SCRIPT,
    ClientErrorLimiter,
    ClientErrorReport,
    install_client_error_endpoint,
)


def test_client_report_schema_accepts_only_non_content_metadata() -> None:
    report = ClientErrorReport(
        kind="error",
        error_name="TypeError",
        asset="app.js",
        line=10,
        column=4,
        session_id=str(uuid.uuid4()),
    )

    assert report.asset == "app.js"
    assert "message" not in ClientErrorReport.model_fields
    assert "stack" not in ClientErrorReport.model_fields
    assert "console" not in CLIENT_ERROR_SCRIPT


def test_client_limiter_allows_only_five_reports_per_page() -> None:
    limiter = ClientErrorLimiter()
    session_id = str(uuid.uuid4())

    assert [limiter.allow(session_id, now=1.0) for _ in range(6)] == [
        True,
        True,
        True,
        True,
        True,
        False,
    ]


def test_client_error_endpoint_logs_safe_metadata_and_rejects_cross_site() -> None:
    stream = io.StringIO()
    runtime = configure_logging(
        LoggingConfig(
            runtime="local",
            component="ui",
            destination="stdout",
            stream=stream,
        )
    )
    app = FastAPI()
    install_client_error_endpoint(app)
    payload = {
        "kind": "error",
        "error_name": "TypeError",
        "asset": "app.js",
        "line": 2,
        "column": 3,
        "session_id": str(uuid.uuid4()),
    }

    async def exercise_endpoint() -> tuple[int, int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            accepted = await client.post(
                "/_rbs/diagnostics/client-error",
                json=payload,
                headers={"sec-fetch-site": "same-origin"},
            )
            rejected = await client.post(
                "/_rbs/diagnostics/client-error",
                json=payload,
                headers={"sec-fetch-site": "cross-site"},
            )
        return accepted.status_code, rejected.status_code

    try:
        assert asyncio.run(exercise_endpoint()) == (204, 403)
    finally:
        runtime.close()

    record = next(
        record
        for record in (json.loads(line) for line in stream.getvalue().splitlines())
        if record["event"] == "browser.unhandled_error"
    )
    assert record["event"] == "browser.unhandled_error"
    assert record["error_name"] == "TypeError"
    assert record["asset"] == "app.js"
    assert "message" not in record
