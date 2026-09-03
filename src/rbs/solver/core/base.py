from typing import Protocol

from rbs.models.enums import SolverEngineName, SolverStatus
from rbs.models.instance import SolverConfig, SolverProblem
from rbs.models.schedule import Schedule, ScheduleMeta, SolverDiagnostic


class SchedulerEngine(Protocol):
    name: SolverEngineName

    def solve(
        self,
        problem: SolverProblem,
        *,
        options: SolverConfig,
        reference_schedule: Schedule | None = None,
    ) -> Schedule: ...


def empty_schedule(
    problem: SolverProblem,
    *,
    engine: SolverEngineName,
    status: SolverStatus,
    notes: list[str],
    diagnostics: list[SolverDiagnostic] | None = None,
    wall_time_seconds: float | None = None,
) -> Schedule:
    unassigned = [resident.id for resident in problem.residents]
    return Schedule(
        meta=ScheduleMeta(
            academic_year=problem.academic_year,
            engine=engine,
            status=status,
            solver_status=status,
            wall_time_seconds=wall_time_seconds,
            diagnostics=diagnostics or [],
            notes=notes,
        ),
        unassigned=unassigned,
    )
