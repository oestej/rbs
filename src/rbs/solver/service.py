"""Solver-side implementation of the portable solve contract."""

from __future__ import annotations

import json

from pydantic import ValidationError

from rbs.models.instance import SolverConfig, SolverProblem
from rbs.models.schedule import Schedule
from rbs.solver.contract import (
    SolveError,
    SolveFailure,
    SolveRequest,
    SolveResponse,
    SolveSuccess,
)


def solve_request(request: SolveRequest) -> SolveSuccess:
    """Solve one already-validated request.

    The engine import lives here so importing the contract or a UI-side client
    never loads OR-Tools or the internal compiler stack.
    """
    return SolveSuccess(
        request_id=request.request_id,
        solution=solve_problem(
            request.build_problem(),
            options=request.options,
            reference_solution=request.reference_solution,
        ),
    )


def solve_problem(
    problem: SolverProblem,
    *,
    options: SolverConfig,
    reference_solution: Schedule | None = None,
) -> Schedule:
    """Solve a portable problem through the stable in-process API.

    This is the sole bridge into the private compiler/engine implementation.
    Callers that need process isolation should use :class:`SolverProcessClient`
    with the same problem, options, and solution models.
    """
    from rbs.solver.core import get_engine

    return get_engine(options.engine).solve(
        problem,
        options=options,
        reference_schedule=reference_solution,
    )


def handle_request_json(payload: str | bytes) -> SolveResponse:
    """Turn one JSON request into one JSON-serializable response.

    Expected request failures become structured errors.  Unexpected engine
    failures are also contained so stdout can remain a valid protocol stream.
    """
    request_id = _request_id_if_present(payload)
    try:
        request = SolveRequest.model_validate_json(payload)
    except ValidationError as exc:
        return _failure(request_id, _validation_code(exc), _message(exc))
    except ValueError as exc:
        return _failure(request_id, "invalid_request", _message(exc))

    try:
        return solve_request(request)
    except Exception as exc:  # noqa: BLE001 - the process boundary must stay valid JSON
        return _failure(request.request_id, "solver_error", _message(exc))


def _failure(request_id: str | None, code, message: str) -> SolveFailure:
    return SolveFailure(
        request_id=request_id,
        error=SolveError(code=code, message=message),
    )


def _request_id_if_present(payload: str | bytes) -> str | None:
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    request_id = value.get("request_id")
    return request_id if isinstance(request_id, str) and request_id else None


SOLVE_ERROR_MESSAGE_LIMIT = 500


def _message(exc: Exception) -> str:
    """Render one line of failure detail for the solve response.

    The caller already owns the input data, so this is not a redaction
    boundary — it keeps tracebacks, multi-line validation dumps, and large
    echoed inputs from bloating the structured protocol response.
    """
    text = " ".join(str(exc).split())
    if len(text) > SOLVE_ERROR_MESSAGE_LIMIT:
        text = text[:SOLVE_ERROR_MESSAGE_LIMIT].rstrip() + "..."
    return text or type(exc).__name__


def _validation_code(exc: ValidationError) -> str:
    errors = exc.errors()
    if errors and all(error.get("loc", ())[:1] == ("problem",) for error in errors):
        return "invalid_problem"
    return "invalid_request"
