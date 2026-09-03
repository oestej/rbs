"""Command-line entry point for the standalone solver process."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Sequence
from time import monotonic

from rbs import __version__
from rbs.logging import LoggingConfig, configure_logging, get_logger, log_context
from rbs.solver.contract import (
    SolveFailure,
    request_json_schema,
    response_json_schema,
)
from rbs.solver.service import handle_request_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rbs-solver",
        description="Solve one versioned RBS JSON request from stdin.",
    )
    parser.add_argument("--version", action="version", version=f"rbs-solver {__version__}")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("solve", "request-schema", "response-schema"),
        default="solve",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime = configure_logging(
        LoggingConfig(runtime="solver", component="solver", destination="stderr")
    )
    try:
        if args.command == "request-schema":
            _write_json(request_json_schema())
            return 0
        if args.command == "response-schema":
            _write_json(response_json_schema())
            return 0

        payload = sys.stdin.buffer.read()
        solve_id = _solve_id(payload)
        started = monotonic()
        logger = get_logger("solver")
        with log_context(solve_id=solve_id):
            logger.info("solver.started")
            response = handle_request_json(payload)
            duration_ms = round((monotonic() - started) * 1000)
            if isinstance(response, SolveFailure):
                logger.error(
                    "solver.failed",
                    duration_ms=duration_ms,
                    error_code=response.error.code,
                )
            else:
                logger.info(
                    "solver.completed",
                    duration_ms=duration_ms,
                    outcome=response.solution.meta.status.value,
                    engine=response.solution.meta.engine.value,
                )
        sys.stdout.write(response.model_dump_json())
        sys.stdout.write("\n")
        if not isinstance(response, SolveFailure):
            return 0
        return 1 if response.error.code == "solver_error" else 2
    finally:
        runtime.close()


def _write_json(value: dict) -> None:
    json.dump(value, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _solve_id(payload: bytes) -> str:
    """Reuse only a canonical UUID request ID as diagnostic correlation."""
    try:
        raw = json.loads(payload)
        candidate = raw.get("request_id") if isinstance(raw, dict) else None
        parsed = uuid.UUID(candidate) if isinstance(candidate, str) else None
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        parsed = None
    if parsed is not None and str(parsed) == candidate.lower():
        return str(parsed)
    return str(uuid.uuid4())
