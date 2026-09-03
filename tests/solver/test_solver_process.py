"""The versioned boundary between callers and the standalone solver."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from datetime import date

import pytest

from rbs.catalog import sample_instance
from rbs.clinic_locks import clinic_slot_is_in_automatic_lock_window
from rbs.logging import LoggingConfig, configure_logging
from rbs.models.enums import Session, SolverEngineName, Weekday
from rbs.models.instance import SolverProblem
from rbs.models.schedule import AssignedClinic, SolverDiagnostic
from rbs.solver.client import SolverProcessClient, SolverProcessError
from rbs.solver.contract import (
    SOLVE_PROTOCOL,
    SOLVE_PROTOCOL_VERSION,
    SolveFailure,
    SolveRequest,
    SolveSuccess,
    parse_response_json,
)
from rbs.solver.service import SOLVE_ERROR_MESSAGE_LIMIT, _message, handle_request_json


def _stub_instance():
    instance = sample_instance()
    return instance.model_copy(
        update={"solver": instance.solver.model_copy(update={"engine": SolverEngineName.STUB})}
    )


def _request(instance, *, request_id: str) -> SolveRequest:
    return SolveRequest.from_problem(
        SolverProblem.from_instance(instance),
        options=instance.solver,
        request_id=request_id,
    )


def test_request_contains_one_self_contained_ui_independent_problem() -> None:
    instance = _stub_instance().revised(lock_through_today=True)

    request = _request(instance, request_id="solve-123")
    payload = json.loads(request.model_dump_json())

    assert payload["protocol"] == SOLVE_PROTOCOL
    assert payload["version"] == SOLVE_PROTOCOL_VERSION
    assert payload["request_id"] == "solve-123"
    assert "residents" in payload["problem"]
    assert payload["problem"]["rotations"]
    assert "color_scheme" not in payload["problem"]
    assert "lock_through_today" not in payload["problem"]
    assert payload["problem"]["clinic_lock_cutoff_date"] == date.today().isoformat()
    assert "color" not in payload["problem"]["rotations"][0]
    assert "color" not in payload["problem"]["clinic_policy"]["sites"][0]
    assert "color" not in payload["problem"]["electives"]
    assert "solver" not in payload["problem"]
    assert payload["options"]["engine"] == SolverEngineName.STUB.value
    assert "case" not in payload and "constraints" not in payload
    assert request.build_problem() == SolverProblem.from_instance(instance)


def test_solver_problem_schema_excludes_presentation_and_workflow_fields() -> None:
    schema = json.dumps(SolverProblem.model_json_schema())

    assert '"lock_through_today"' not in schema
    assert '"color_scheme"' not in schema
    assert '"color"' not in schema


def test_projected_problem_retains_resolved_clinic_lock_semantics() -> None:
    instance = _stub_instance().revised(lock_through_today=True)
    cutoff = instance.calendar.first_week_start
    problem = SolverProblem.from_instance(instance, today=cutoff)
    slot = AssignedClinic(weekday=Weekday.MONDAY, session=Session.MORNING)

    assert problem.clinic_lock_cutoff_date == cutoff
    assert clinic_slot_is_in_automatic_lock_window(problem, slot, 1)


def test_service_returns_a_typed_solution_with_the_same_request_id() -> None:
    request = _request(_stub_instance(), request_id="solve-456")

    response = handle_request_json(request.model_dump_json())

    assert isinstance(response, SolveSuccess)
    assert response.request_id == request.request_id
    assert response.solution.meta.engine is SolverEngineName.STUB
    assert len(response.solution.unassigned) == len(request.problem.residents)


def test_structured_diagnostics_round_trip_over_the_json_contract() -> None:
    request = _request(_stub_instance(), request_id="diagnostic-response")
    response = handle_request_json(request.model_dump_json())
    assert isinstance(response, SolveSuccess)
    diagnostic = SolverDiagnostic(
        code="resident_vacation_coverage",
        message="Resident cannot cover all weeks.",
        resident_ids=[request.problem.residents[0].id],
        weeks=[9, 10],
        suggestions=["Move a vacation week."],
    )
    response.solution.meta.diagnostics = [diagnostic]

    restored = parse_response_json(response.model_dump_json())

    assert isinstance(restored, SolveSuccess)
    assert restored.solution.meta.diagnostics == [diagnostic]


def test_invalid_wire_payload_returns_a_structured_error() -> None:
    response = handle_request_json('{"request_id":"bad-request","version":99}')

    assert isinstance(response, SolveFailure)
    assert response.request_id == "bad-request"
    assert response.error.code == "invalid_request"


def test_error_messages_are_single_line_and_bounded() -> None:
    assert _message(ValueError("boom")) == "boom"
    assert _message(ValueError("  padded  ")) == "padded"
    assert _message(ValueError("")) == "ValueError"

    multiline = _message(RuntimeError("line one\nline two\n  line three"))
    assert multiline == "line one line two line three"

    long_message = _message(RuntimeError("x" * (SOLVE_ERROR_MESSAGE_LIMIT + 100)))
    assert len(long_message) == SOLVE_ERROR_MESSAGE_LIMIT + 3
    assert long_message.endswith("...")
    assert "\n" not in long_message


def test_invalid_problem_is_not_reported_as_an_engine_crash() -> None:
    instance = _stub_instance()
    request = _request(instance, request_id="bad-problem")
    payload = json.loads(request.model_dump_json())
    payload["problem"]["residents"][0]["pgy"] = 99

    response = handle_request_json(json.dumps(payload))

    assert isinstance(response, SolveFailure)
    assert response.request_id == "bad-problem"
    assert response.error.code == "invalid_problem"
    assert "no curriculum for training level(s): PGY 99" in response.error.message


def test_standalone_process_reads_stdin_and_writes_only_one_response() -> None:
    request = _request(_stub_instance(), request_id="process-1")

    completed = subprocess.run(
        [sys.executable, "-m", "rbs.solver"],
        input=request.model_dump_json(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0
    diagnostics = [json.loads(line) for line in completed.stderr.splitlines()]
    assert [record["event"] for record in diagnostics] == [
        "solver.started",
        "solver.completed",
    ]
    assert all(record["runtime"] == "solver" for record in diagnostics)
    assert all(record["schema_version"] == 1 for record in diagnostics)
    response = parse_response_json(completed.stdout)
    assert isinstance(response, SolveSuccess)
    assert response.request_id == "process-1"


def test_standalone_process_uses_nonzero_exit_for_bad_input() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "rbs.solver"],
        input="not json",
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 2
    response = parse_response_json(completed.stdout)
    assert isinstance(response, SolveFailure)
    assert response.error.code == "invalid_request"


def test_process_client_relays_child_diagnostics_through_the_parent_pipeline() -> None:
    stream = io.StringIO()
    runtime = configure_logging(
        LoggingConfig(
            runtime="local",
            component="ui",
            destination="stdout",
            stream=stream,
        )
    )
    instance = _stub_instance()
    try:
        SolverProcessClient().solve(
            SolverProblem.from_instance(instance),
            options=instance.solver,
            timeout=30,
        )
    finally:
        runtime.close()

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [record["event"] for record in records] == [
        "solver.process_started",
        "solver.started",
        "solver.completed",
        "solver.process_completed",
    ]
    assert {record["runtime"] for record in records} == {"local"}
    assert {record["run_id"] for record in records} == {runtime.run_id}
    assert len({record["solve_id"] for record in records}) == 1


def test_process_client_surfaces_structured_solver_errors() -> None:
    client = SolverProcessClient(
        command=(
            sys.executable,
            "-c",
            (
                "import json,sys; request=json.load(sys.stdin); "
                    "print(json.dumps({'protocol':'rbs.solve','version':4,"
                "'request_id':request['request_id'],'status':'error',"
                "'error':{'code':'solver_error','message':'boom'}}))"
            ),
        )
    )

    with pytest.raises(SolverProcessError, match="boom") as caught:
        instance = _stub_instance()
        client.solve(
            SolverProblem.from_instance(instance),
            options=instance.solver,
            timeout=30,
        )

    assert caught.value.code == "solver_error"
