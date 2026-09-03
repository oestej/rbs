"""Bounded, out-of-process solving for the hosted build.

Two problems this exists to solve. First, CP-SAT is the only genuinely expensive
thing RBS does, and running it in the web process means one user's solve
competes with everyone else's page renders - and can sit long enough on the
event loop to trip a proxy's websocket idle timeout. Second, the solver settings
are user-editable and unbounded, so on a shared host they are a denial-of-service
knob rather than a preference.
"""

from __future__ import annotations

import asyncio
import uuid
from time import monotonic

from rbs.cloud.config import CloudConfig
from rbs.logging import get_logger, log_context
from rbs.models.instance import SchedulerInput, SolverProblem
from rbs.models.schedule import Schedule
from rbs.solver.client import SolverProcessClient


def clamp_solver_settings(instance: SchedulerInput, config: CloudConfig) -> SchedulerInput:
    """Bound the user-editable solver settings to what this host will allow.

    ``time_limit_seconds`` has no upper bound in the model and ``num_workers``
    no upper bound worth the name, so a single caller could otherwise occupy the
    box indefinitely.
    """
    solver = instance.solver
    time_limit = min(float(solver.time_limit_seconds), config.solve_ceiling_seconds)
    workers = min(int(solver.num_workers), max(1, config.solve_workers))
    if time_limit == solver.time_limit_seconds and workers == solver.num_workers:
        return instance
    return instance.model_copy(
        update={
            "solver": solver.model_copy(
                update={"time_limit_seconds": time_limit, "num_workers": workers}
            )
        }
    )


class SolvePool:
    """Runs bounded, killable solver subprocesses off the web event loop."""

    def __init__(
        self,
        config: CloudConfig,
        *,
        solver: SolverProcessClient | None = None,
    ) -> None:
        self._config = config
        self._size = config.resolved_solve_pool_size()
        self._gate = asyncio.Semaphore(self._size)
        self._waiting = 0
        self._solver = solver or SolverProcessClient()
        self._closing = False

    @property
    def size(self) -> int:
        return self._size

    @property
    def waiting(self) -> int:
        """How many callers are queued behind a running solve."""
        return self._waiting

    async def solve(
        self,
        instance: SchedulerInput,
        *,
        reference_schedule: Schedule | None = None,
    ) -> Schedule:
        if self._closing:
            raise RuntimeError("solver pool is shutting down")
        bounded = clamp_solver_settings(instance, self._config)
        operation_id = str(uuid.uuid4())
        logger = get_logger("solver.pool")
        started = monotonic()
        # One solve can make a second pass after relaxing the automatic Clinic
        # balance band. Portfolio attempts run concurrently, so the raw user
        # ``solve_attempts`` value must never multiply this deadline.
        deadline = bounded.solver.time_limit_seconds * 2 + 60.0
        self._waiting += 1
        with log_context(operation_id=operation_id):
            logger.info(
                "solver.queued",
                queue_depth=self._waiting,
                engine=bounded.solver.engine.value,
                num_workers=bounded.solver.num_workers,
                time_limit_seconds=bounded.solver.time_limit_seconds,
            )
        try:
            await self._gate.acquire()
        except asyncio.CancelledError:
            with log_context(operation_id=operation_id):
                logger.info(
                    "solver.cancelled",
                    duration_ms=round((monotonic() - started) * 1000),
                )
            raise
        finally:
            # Whether the wait ended in a slot or a cancellation, this caller is
            # no longer queued.
            self._waiting -= 1
        try:
            with log_context(operation_id=operation_id):
                logger.info("solver.started")
                try:
                    result = await self._solver.solve_async(
                        SolverProblem.from_instance(bounded),
                        options=bounded.solver,
                        reference_solution=reference_schedule,
                        timeout=deadline,
                    )
                except asyncio.CancelledError:
                    logger.info(
                        "solver.cancelled",
                        duration_ms=round((monotonic() - started) * 1000),
                    )
                    raise
                except Exception as exc:
                    logger.error(
                        "solver.failed",
                        duration_ms=round((monotonic() - started) * 1000),
                        error_code=getattr(exc, "code", type(exc).__name__),
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
        finally:
            self._gate.release()

    async def shutdown(self) -> None:
        """Stop accepting work and wait until every acquired slot is returned."""
        self._closing = True
        for _ in range(self._size):
            await self._gate.acquire()
        for _ in range(self._size):
            self._gate.release()
