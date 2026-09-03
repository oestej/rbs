"""Versioned JSON contract shared by solver callers and implementations.

The contract deliberately contains only domain models.  A solver process does
not need to know about a workspace database, a web request, or the UI that
initiated a solve.  Likewise, a caller does not need to import OR-Tools or any
of the model-compilation code.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias
from uuid import uuid4

from pydantic import Field, TypeAdapter

from rbs.models.common import StrictModel
from rbs.models.instance import SolverConfig, SolverProblem
from rbs.models.schedule import Schedule

SOLVE_PROTOCOL = "rbs.solve"
SOLVE_PROTOCOL_VERSION = 4


class SolveRequest(StrictModel):
    """Everything a standalone solver needs for one deterministic request.

    ``problem`` is a complete semantic scheduling problem. It deliberately
    excludes workspace presentation and UI workflow fields.
    """

    protocol: Literal["rbs.solve"] = SOLVE_PROTOCOL
    version: Literal[4] = SOLVE_PROTOCOL_VERSION
    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    problem: SolverProblem
    options: SolverConfig
    reference_solution: Schedule | None = None

    @classmethod
    def from_problem(
        cls,
        problem: SolverProblem,
        *,
        options: SolverConfig,
        reference_solution: Schedule | None = None,
        request_id: str | None = None,
    ) -> SolveRequest:
        values = {
            "problem": SolverProblem.from_instance(problem),
            "options": options,
            "reference_solution": reference_solution,
        }
        if request_id is not None:
            values["request_id"] = request_id
        return cls(**values)

    def build_problem(self) -> SolverProblem:
        """Return the validated, UI-independent solver input."""
        return self.problem


class SolveError(StrictModel):
    """A machine-readable failure safe to return across a process boundary."""

    code: Literal["invalid_request", "invalid_problem", "solver_error"]
    message: str = Field(min_length=1)


class SolveSuccess(StrictModel):
    protocol: Literal["rbs.solve"] = SOLVE_PROTOCOL
    version: Literal[4] = SOLVE_PROTOCOL_VERSION
    request_id: str | None = None
    status: Literal["ok"] = "ok"
    solution: Schedule


class SolveFailure(StrictModel):
    protocol: Literal["rbs.solve"] = SOLVE_PROTOCOL
    version: Literal[4] = SOLVE_PROTOCOL_VERSION
    request_id: str | None = None
    status: Literal["error"] = "error"
    error: SolveError


SolveResponse: TypeAlias = Annotated[
    SolveSuccess | SolveFailure,
    Field(discriminator="status"),
]

_RESPONSE_ADAPTER = TypeAdapter(SolveResponse)


def parse_response_json(payload: str | bytes) -> SolveResponse:
    """Validate a response without coupling callers to a concrete transport."""
    return _RESPONSE_ADAPTER.validate_json(payload)


def request_json_schema() -> dict:
    return SolveRequest.model_json_schema()


def response_json_schema() -> dict:
    return _RESPONSE_ADAPTER.json_schema()
