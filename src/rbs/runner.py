from __future__ import annotations

import uuid
from pathlib import Path
from time import monotonic

from rbs.emit import write_schedule
from rbs.ingest import load_instance
from rbs.logging import get_logger, log_context
from rbs.models.enums import SolverEngineName
from rbs.models.instance import SchedulerInput, SolverProblem
from rbs.models.schedule import Schedule
from rbs.solver.service import solve_problem

PathLike = str | Path


def run_schedule(
    input_path: PathLike,
    output_path: PathLike | None = None,
    *,
    catalog_path: PathLike | None = None,
    engine: str | SolverEngineName | None = None,
) -> Schedule:
    instance = load_instance(input_path, catalog_path=catalog_path)
    schedule = solve_instance(instance, engine=engine)
    if output_path is not None:
        write_schedule(schedule, output_path)
    return schedule


def solve_instance(
    instance: SchedulerInput,
    engine: str | SolverEngineName | None = None,
    *,
    reference_schedule: Schedule | None = None,
) -> Schedule:
    engine_name = SolverEngineName(engine or instance.solver.engine)
    options = instance.solver.model_copy(update={"engine": engine_name})
    operation_id = str(uuid.uuid4())
    logger = get_logger("solver.in_process")
    started = monotonic()
    with log_context(operation_id=operation_id):
        logger.info(
            "solver.started",
            engine=options.engine.value,
            num_workers=options.num_workers,
            time_limit_seconds=options.time_limit_seconds,
        )
        try:
            result = solve_problem(
                SolverProblem.from_instance(instance),
                options=options,
                reference_solution=reference_schedule,
            )
        except Exception as exc:
            logger.error(
                "solver.failed",
                duration_ms=round((monotonic() - started) * 1000),
                error_code=type(exc).__name__,
                exc_info=True,
            )
            raise
        logger.info(
            "solver.completed",
            duration_ms=round((monotonic() - started) * 1000),
            outcome=result.meta.status.value,
            engine=result.meta.engine.value,
        )
        return result
