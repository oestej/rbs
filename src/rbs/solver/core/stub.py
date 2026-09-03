from rbs.models.enums import SolverEngineName, SolverStatus
from rbs.models.instance import SolverConfig, SolverProblem
from rbs.models.schedule import Schedule
from rbs.solver.core.base import SchedulerEngine, empty_schedule


class StubEngine:
    """Passthrough engine: validates the pipeline without assigning blocks."""

    name = SolverEngineName.STUB

    def solve(
        self,
        problem: SolverProblem,
        *,
        options: SolverConfig,
        reference_schedule: Schedule | None = None,
    ) -> Schedule:
        _ = options, reference_schedule
        return empty_schedule(
            problem,
            engine=self.name,
            status=SolverStatus.NOT_IMPLEMENTED,
            notes=[
                "stub engine emits an empty schedule so ingest/output can be tested "
                "before the CP-SAT model is wired up"
            ],
        )


def get_stub_engine() -> SchedulerEngine:
    return StubEngine()
