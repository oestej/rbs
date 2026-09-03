"""OR-Tools CP-SAT scheduler facade.

Compilation, hard constraints, objective construction, decoding, post-processing,
and final validation live in separate modules so their statuses and metrics do
not get conflated.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from rbs.models.enums import SolverEngineName, SolverStatus
from rbs.models.instance import SolverConfig, SolverProblem
from rbs.models.schedule import Schedule
from rbs.solver.core.base import SchedulerEngine, empty_schedule
from rbs.solver.core.compile import compile_problem
from rbs.solver.core.context import ModelBuildError
from rbs.solver.core.decode import decode_solution
from rbs.solver.core.diagnostics import explain_infeasibility
from rbs.solver.planning import resolve_clinic_block_band
from rbs.solver.tuning import portfolio_plan


class CpSatEngine:
    name = SolverEngineName.CP_SAT

    def solve(
        self,
        instance: SolverProblem,
        *,
        options: SolverConfig,
        reference_schedule: Schedule | None = None,
    ) -> Schedule:
        reference_schedule = _compatible_reference(instance, reference_schedule)
        schedule = self._solve_portfolio(
            instance,
            options=options,
            reference_schedule=reference_schedule,
        )
        if not _needs_band_relaxation(instance, options, schedule):
            return _with_infeasibility_diagnostics(instance, options, schedule)
        # The curriculum-derived clinic band is a convenience, not a rule the
        # program asked for. Vacation, locks, or manual blocks can leave it
        # unsatisfiable, and an empty year is a worse answer than an uneven one.
        relaxed_options = options.model_copy(
            update={"auto_balance_clinic_blocks": False}
        )
        fallback = self._solve_portfolio(
            instance,
            options=relaxed_options,
            reference_schedule=reference_schedule,
        )
        if fallback.is_empty():
            return _with_infeasibility_diagnostics(instance, options, schedule)
        fallback.meta.notes = [
            *fallback.meta.notes,
            "automatic clinic balance could not be satisfied and was dropped for this solve",
        ]
        return fallback

    def _solve_portfolio(
        self,
        instance: SolverProblem,
        *,
        options: SolverConfig,
        reference_schedule: Schedule | None,
    ) -> Schedule:
        """Race a few independent seeds and keep the best schedule.

        The search settles into one of two basins roughly at random, and extra
        time does not escape the worse one - measured flat from 30s to 240s. A
        handful of concurrent seeds does escape it. CP-SAT releases the GIL, so
        threads give real parallelism and the attempts share one wall clock.
        """
        attempts, workers = portfolio_plan(options)
        if attempts == 1:
            return self._solve_once(
                instance,
                options=options,
                workers=workers,
                reference_schedule=reference_schedule,
            )

        base = options.random_seed or 0
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=attempts) as pool:
            schedules = list(
                pool.map(
                    lambda index: self._solve_once(
                        instance,
                        options=options,
                        workers=workers,
                        seed=base + index,
                        reference_schedule=reference_schedule,
                    ),
                    range(attempts),
                )
            )
        best = min(schedules, key=_attempt_rank)
        best.meta.wall_time_seconds = time.perf_counter() - started
        if not best.is_empty():
            best.meta.notes = [
                *best.meta.notes,
                f"best of {attempts} concurrent solves x {workers} workers",
            ]
        return best

    def _solve_once(
        self,
        instance: SolverProblem,
        *,
        options: SolverConfig,
        workers: int | None = None,
        seed: int | None = None,
        reference_schedule: Schedule | None = None,
    ) -> Schedule:
        try:
            from ortools.sat.python import cp_model
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("the cp_sat engine requires the 'ortools' package") from exc

        started = time.perf_counter()
        try:
            problem = compile_problem(
                instance,
                options,
                cp_model,
                reference_schedule=reference_schedule,
            )
        except ModelBuildError as exc:
            return empty_schedule(
                instance,
                engine=self.name,
                status=SolverStatus.INFEASIBLE,
                notes=[str(exc)],
                wall_time_seconds=time.perf_counter() - started,
            )

        deadline = started + options.time_limit_seconds
        worker_count = max(1, workers if workers is not None else options.num_workers)
        chosen_seed = seed if seed is not None else options.random_seed
        best_solver = None

        final_objective = (
            (problem.clinic.quality_bound + 1) * problem.clinic.stability_cost
            + problem.clinic.quality_cost
        )

        def new_solver(*, final_phase: bool):
            remaining = max(0.001, deadline - time.perf_counter())
            phase_solver = cp_model.CpSolver()
            phase_solver.parameters.max_time_in_seconds = remaining
            phase_solver.parameters.num_search_workers = worker_count
            # Matching tiers are exact lexicographic phases. The configured
            # relative gap is meaningful only for the final clinic objective.
            if final_phase and options.relative_gap is not None:
                phase_solver.parameters.relative_gap_limit = options.relative_gap
            if chosen_seed is not None:
                phase_solver.parameters.random_seed = chosen_seed
            return phase_solver

        def finish(
            phase_solver,
            status: SolverStatus,
            *,
            final_phase: bool,
        ) -> Schedule:
            objective = (
                float(phase_solver.Value(final_objective))
                if problem.clinic.has_objective
                else None
            )
            schedule = decode_solution(
                problem,
                phase_solver,
                status,
                solver_objective=objective,
                solver_best_bound=(
                    float(phase_solver.BestObjectiveBound())
                    if final_phase and problem.clinic.has_objective
                    else None
                ),
            )
            schedule.meta.wall_time_seconds = time.perf_counter() - started
            return schedule

        def no_solution(phase_solver, result) -> Schedule:
            solver_status = _status(result, cp_model)
            notes = [f"CP-SAT returned {phase_solver.StatusName(result)}"]
            if solver_status is SolverStatus.UNKNOWN:
                notes.append(
                    f"no feasible schedule within {options.time_limit_seconds:g}s"
                )
            return empty_schedule(
                instance,
                engine=self.name,
                status=solver_status,
                notes=notes,
                wall_time_seconds=time.perf_counter() - started,
            )

        matching_phases = []
        if problem.matching.fallback_literals:
            matching_phases.append(
                ("fallback", problem.matching.fallback_count, False, 0)
            )
        matching_phases.extend(
            (f"rank-{rank}", expression, True, len(literals))
            for rank, (expression, literals) in enumerate(
                zip(
                    problem.matching.rank_counts,
                    problem.matching.rank_literals,
                    strict=True,
                ),
                start=1,
            )
            if literals
        )

        try:
            for _label, expression, maximize, theoretical_best in matching_phases:
                if best_solver is not None and time.perf_counter() >= deadline:
                    return finish(best_solver, SolverStatus.FEASIBLE, final_phase=False)
                if maximize:
                    problem.context.model.Maximize(expression)
                else:
                    problem.context.model.Minimize(expression)
                phase_solver = new_solver(final_phase=False)
                result = phase_solver.Solve(problem.context.model)
                phase_status = _status(result, cp_model)
                if phase_status not in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}:
                    if best_solver is not None:
                        return finish(
                            best_solver,
                            SolverStatus.FEASIBLE,
                            final_phase=False,
                        )
                    return no_solution(phase_solver, result)
                best_solver = phase_solver
                achieved = int(round(phase_solver.ObjectiveValue()))
                tier_proven = (
                    phase_status is SolverStatus.OPTIMAL
                    or achieved == theoretical_best
                )
                if not tier_proven:
                    return finish(
                        phase_solver,
                        SolverStatus.FEASIBLE,
                        final_phase=False,
                    )
                problem.context.model.Add(expression == achieved)

            if best_solver is not None and time.perf_counter() >= deadline:
                return finish(best_solver, SolverStatus.FEASIBLE, final_phase=False)
            problem.context.model.Minimize(final_objective)
            final_solver = new_solver(final_phase=True)
            result = final_solver.Solve(problem.context.model)
        except KeyboardInterrupt:
            if best_solver is not None:
                return finish(best_solver, SolverStatus.FEASIBLE, final_phase=False)
            return empty_schedule(
                instance,
                engine=self.name,
                status=SolverStatus.UNKNOWN,
                notes=["solve interrupted"],
                wall_time_seconds=time.perf_counter() - started,
            )

        solver_status = _status(result, cp_model)
        if solver_status not in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}:
            if best_solver is not None:
                return finish(best_solver, SolverStatus.FEASIBLE, final_phase=False)
            return no_solution(final_solver, result)
        return finish(final_solver, solver_status, final_phase=True)


def _attempt_rank(schedule: Schedule) -> tuple:
    """Order attempts by the same lexicographic tiers used inside each solve."""
    objective = schedule.meta.solver_objective
    metrics = schedule.meta.metrics
    return (
        schedule.is_empty(),
        metrics.elective_fallback_blocks,
        *(-count for count in metrics.elective_preference_rank_counts),
        objective if objective is not None else float("inf"),
    )


def _needs_band_relaxation(
    instance: SolverProblem,
    options: SolverConfig,
    schedule: Schedule,
) -> bool:
    """Whether an automatic clinic band is what made this solve come back empty."""
    if not schedule.is_empty():
        return False
    _low, _high, automatic = resolve_clinic_block_band(instance, options)
    return automatic


def _with_infeasibility_diagnostics(
    instance: SolverProblem,
    options: SolverConfig,
    schedule: Schedule,
) -> Schedule:
    if schedule.meta.status is not SolverStatus.INFEASIBLE:
        return schedule
    diagnostics = explain_infeasibility(instance, options)
    if not diagnostics:
        return schedule
    schedule.meta.diagnostics = diagnostics
    schedule.meta.notes = [
        *schedule.meta.notes,
        *(diagnostic.message for diagnostic in diagnostics),
    ]
    return schedule


def _compatible_reference(
    instance: SolverProblem,
    reference_schedule: Schedule | None,
) -> Schedule | None:
    if (
        reference_schedule is None
        or reference_schedule.is_empty()
        or reference_schedule.meta.academic_year != instance.academic_year
    ):
        return None
    return reference_schedule


def get_cp_sat_engine() -> SchedulerEngine:
    return CpSatEngine()


def _status(result, cp_model) -> SolverStatus:
    return {
        cp_model.OPTIMAL: SolverStatus.OPTIMAL,
        cp_model.FEASIBLE: SolverStatus.FEASIBLE,
        cp_model.INFEASIBLE: SolverStatus.INFEASIBLE,
        cp_model.UNKNOWN: SolverStatus.UNKNOWN,
        cp_model.MODEL_INVALID: SolverStatus.UNKNOWN,
    }.get(result, SolverStatus.UNKNOWN)
