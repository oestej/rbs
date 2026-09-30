"""The shared user-facing report for failed solves."""

from rbs.models.enums import SolverEngineName, SolverStatus
from rbs.models.schedule import Schedule, ScheduleMeta, SolverDiagnostic
from rbs.solver.failure_report import describe_solver_failure, format_report_text


def _schedule(
    status: SolverStatus,
    *,
    solver_status: SolverStatus | None = None,
    notes: list[str] | None = None,
    validation_errors: list[str] | None = None,
    diagnostics: list[SolverDiagnostic] | None = None,
) -> Schedule:
    return Schedule(
        meta=ScheduleMeta(
            academic_year="2026-2027",
            engine=SolverEngineName.CP_SAT,
            status=status,
            solver_status=solver_status,
            notes=notes or [],
            validation_errors=validation_errors or [],
            diagnostics=diagnostics or [],
        )
    )


def test_success_needs_no_report() -> None:
    assert (
        describe_solver_failure(_schedule(SolverStatus.OPTIMAL, solver_status=SolverStatus.OPTIMAL))
        is None
    )
    assert (
        describe_solver_failure(
            _schedule(SolverStatus.FEASIBLE, solver_status=SolverStatus.FEASIBLE)
        )
        is None
    )


def test_infeasible_surfaces_solver_diagnostics() -> None:
    diagnostic = SolverDiagnostic(
        code="locked_capacity_conflict",
        message="Night Float weeks 5-6: 2 residents are locked there.",
        suggestions=["Move or delete one of the locked blocks on those weeks."],
    )
    report = describe_solver_failure(
        _schedule(
            SolverStatus.INFEASIBLE,
            notes=["CP-SAT returned INFEASIBLE"],
            diagnostics=[diagnostic],
        )
    )

    assert report is not None
    assert report.title == "No feasible block schedule"
    assert report.notification == "Block schedule is infeasible"
    assert report.diagnostics == (diagnostic,)
    text = format_report_text(report)
    assert diagnostic.message in text
    assert "Move or delete one of the locked blocks" in text


def test_infeasible_without_diagnostics_hides_solver_jargon() -> None:
    report = describe_solver_failure(
        _schedule(
            SolverStatus.INFEASIBLE,
            solver_status=SolverStatus.INFEASIBLE,
            notes=["CP-SAT returned INFEASIBLE", "best of 4 concurrent solves x 8 workers"],
        )
    )

    assert report is not None
    assert report.title == "No feasible block schedule"
    assert len(report.diagnostics) == 1
    (fallback,) = report.diagnostics
    assert "CP-SAT" not in fallback.message
    assert "concurrent solves" not in fallback.message
    assert fallback.suggestions
    assert "CP-SAT" not in format_report_text(report)


def test_timeout_reports_the_time_limit() -> None:
    report = describe_solver_failure(
        _schedule(
            SolverStatus.UNKNOWN,
            solver_status=SolverStatus.UNKNOWN,
            notes=["CP-SAT returned UNKNOWN", "no feasible schedule within 60s"],
        )
    )

    assert report is not None
    assert report.title == "No schedule found in time"
    assert "does not prove" in report.detail


def test_clinic_failure_reports_placement() -> None:
    report = describe_solver_failure(
        _schedule(
            SolverStatus.UNKNOWN,
            solver_status=SolverStatus.FEASIBLE,
            validation_errors=["Maple capacity exceeded"],
            diagnostics=[
                SolverDiagnostic(
                    code="clinic_allocation_capacity",
                    message="Maple: 3 clinic half-days exceed capacity.",
                )
            ],
        )
    )

    assert report is not None
    assert report.title == "Block schedule found, but clinic placement failed"
    assert "not accepted" in report.detail


def test_model_build_error_reports_configuration() -> None:
    report = describe_solver_failure(
        _schedule(
            SolverStatus.INFEASIBLE,
            diagnostics=[
                SolverDiagnostic(
                    code="model_build_error",
                    message="Clinic · PGY1 has no compatible 2-week fallback.",
                )
            ],
        )
    )

    assert report is not None
    assert report.title == "Cannot build schedule model"


def test_a_solve_stopped_by_configuration_reports_that_no_model_was_built(monkeypatch) -> None:
    # Each readiness conflict keeps its own code, so the report must still
    # recognize the build failure rather than call the rules contradictory.
    from rbs.catalog import sample_instance
    from rbs.solver.core import cp_sat, get_engine
    from rbs.solver.failure_report import MODEL_NOT_BUILT_NOTE

    instance = sample_instance()
    blocked = instance.revised(rotations=[
        rotation.model_copy(update={"max_consecutive_weeks": 2})
        if rotation.id == "icu"
        else rotation
        for rotation in instance.rotations
    ])
    checks = []
    readiness = cp_sat.check_solve_readiness
    monkeypatch.setattr(
        cp_sat,
        "check_solve_readiness",
        lambda *args, **kwargs: checks.append(True) or readiness(*args, **kwargs),
    )

    schedule = get_engine("cp_sat").solve(
        blocked, options=blocked.solver.model_copy(update={"solve_attempts": 1}),
    )
    report = describe_solver_failure(schedule)

    assert MODEL_NOT_BUILT_NOTE in schedule.meta.notes
    assert [diagnostic.code for diagnostic in schedule.meta.diagnostics] == [
        "block_exceeds_consecutive_limit"
    ]
    assert report is not None
    assert report.title == "Cannot build schedule model"
    assert "ICU · PGY1" in report.diagnostics[0].message
    # Readiness ran once, before any compile; the report reuses its issues.
    assert checks == [True]


def test_infeasible_search_without_the_build_note_still_reports_contradiction() -> None:
    report = describe_solver_failure(
        _schedule(
            SolverStatus.INFEASIBLE,
            solver_status=SolverStatus.INFEASIBLE,
            diagnostics=[
                SolverDiagnostic(
                    code="uncoverable_clinic_session",
                    message="Ada Lovelace cannot place Clinic.",
                )
            ],
        )
    )
    assert report is not None
    assert report.title == "No feasible block schedule"
