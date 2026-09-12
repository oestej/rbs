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
from rbs.models.schedule import Schedule, SolverDiagnostic
from rbs.solver.core.base import SchedulerEngine, empty_schedule
from rbs.solver.core.compile import compile_problem
from rbs.solver.core.context import ModelBuildError
from rbs.solver.core.decode import decode_solution
from rbs.solver.core.diagnostics import explain_infeasibility
from rbs.solver.planning import resolve_clinic_block_band
from rbs.solver.reference import (
    REFERENCE_LOCK_CONFLICT,
    honored_reference_lock_diagnostic,
    reference_clinic_lock_barriers,
    reference_lock_conflict_diagnostic,
    viable_reference_clinic_locks,
)
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
            message = str(exc)
            return empty_schedule(
                instance,
                engine=self.name,
                status=SolverStatus.INFEASIBLE,
                notes=[message],
                diagnostics=[
                    SolverDiagnostic(
                        code="model_build_error",
                        message=message,
                        suggestions=[
                            "Review the named rotation, block, or lock configuration."
                        ],
                    )
                ],
                wall_time_seconds=time.perf_counter() - started,
            )

        deadline = started + options.time_limit_seconds
        worker_count = max(1, workers if workers is not None else options.num_workers)
        chosen_seed = seed if seed is not None else options.random_seed
        best_solver = None
        honored_locks, lock_barriers = _reference_lock_state(problem)

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
            for key in sorted(
                honored_locks,
                key=_lock_sort_key,
            ):
                schedule.meta.diagnostics.append(
                    honored_reference_lock_diagnostic(
                        instance, reference_schedule, key
                    )
                )
            if honored_locks:
                count = len(honored_locks)
                schedule.meta.notes.append(
                    f"{count} locked clinic "
                    f"{'session' if count == 1 else 'sessions'} "
                    "fell on a clinic day the rotation doesn't allow; an extra "
                    f"{'session was' if count == 1 else 'sessions were'} scheduled "
                    "to keep the lock"
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
            diagnostics: list[SolverDiagnostic] = []
            if solver_status is SolverStatus.INFEASIBLE and lock_barriers:
                diagnostics.append(
                    reference_lock_conflict_diagnostic(
                        instance, reference_schedule, lock_barriers
                    )
                )
            return empty_schedule(
                instance,
                engine=self.name,
                status=solver_status,
                notes=notes,
                diagnostics=diagnostics,
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
    """Order attempts by the same lexicographic tiers used inside each solve.

    Validity outranks every tier below it. Clinic-site allocation runs after the
    search, so one seed can post-process into a capacity violation while another
    stays valid, and the invalid attempt is free to carry the better objective.
    The caller cannot use it either way: an attempt with validation errors is
    reported as ``unknown`` and refused downstream.
    """
    objective = schedule.meta.solver_objective
    metrics = schedule.meta.metrics
    return (
        schedule.is_empty(),
        bool(schedule.meta.validation_errors),
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


def _lock_sort_key(item) -> tuple:
    return (item[0], item[1], item[2].value, item[3].value)


def _reference_lock_state(problem) -> tuple[dict, dict]:
    """Split reference clinic locks into honored ones and barriers.

    Honored locks were kept through a one-off extra session and warn on
    success. Barriers explain the locks no extra session can rescue; they
    stay enforced, so they are the prime suspects when the solve is
    infeasible.
    """
    reference = problem.reference_schedule
    if reference is None:
        return {}, {}
    instance = problem.context.instance
    occurrences = {
        occurrence.key: occurrence for occurrence in problem.context.occurrences
    }
    viable = viable_reference_clinic_locks(
        instance,
        reference,
        occurrences,
        problem.context.starts,
    )
    recorded = problem.clinic.synthetic_reference_locks
    honored = {key: state for key, state in viable.items() if key in recorded}
    barriers = {
        key: barrier
        for key, barrier in reference_clinic_lock_barriers(
            instance,
            reference,
            occurrences,
            problem.context.starts,
        ).items()
        # Only a lock that compiled to a hard equality can contradict the
        # model. A barrier without entries (a vacation week, for example)
        # adds no constraint, so it cannot be the cause.
        if any(
            weekday is key[2] and session is key[3]
            for _keys, weekday, session, _literal in problem.clinic.in_clinic.get(
                (key[0], key[1]), ()
            )
        )
    }
    return honored, barriers


def _with_infeasibility_diagnostics(
    instance: SolverProblem,
    options: SolverConfig,
    schedule: Schedule,
) -> Schedule:
    if schedule.meta.status is not SolverStatus.INFEASIBLE:
        return schedule
    # A reference-lock conflict is provisional: the focused probes below run
    # without the previous draft, so a conclusive one supersedes it. The
    # reference explanation is restored only when the probes find nothing.
    provisional = [
        diagnostic
        for diagnostic in schedule.meta.diagnostics
        if diagnostic.code == REFERENCE_LOCK_CONFLICT
    ]
    if provisional:
        schedule.meta.diagnostics = [
            diagnostic
            for diagnostic in schedule.meta.diagnostics
            if diagnostic.code != REFERENCE_LOCK_CONFLICT
        ]
    # A compile-time configuration error already names the exact problem. Do
    # not replace it with resident-level probes that will all fail for the same
    # shared reason. Coverage-build errors are the exception: the focused
    # probes can tell whether vacation-like weeks are the actual cause.
    if schedule.meta.diagnostics and not _needs_coverage_diagnosis(schedule):
        return schedule
    diagnostics = explain_infeasibility(instance, options)
    if not diagnostics:
        if provisional:
            schedule.meta.diagnostics = [
                *schedule.meta.diagnostics,
                *provisional,
            ]
            schedule.meta.notes = [
                *schedule.meta.notes,
                *(diagnostic.message for diagnostic in provisional),
            ]
        return schedule
    schedule.meta.diagnostics = diagnostics
    schedule.meta.notes = [
        *schedule.meta.notes,
        *(diagnostic.message for diagnostic in diagnostics),
    ]
    return schedule


def _needs_coverage_diagnosis(schedule: Schedule) -> bool:
    if len(schedule.meta.diagnostics) != 1:
        return False
    diagnostic = schedule.meta.diagnostics[0]
    if diagnostic.code != "model_build_error":
        return False
    return any(
        marker in diagnostic.message
        for marker in (
            " has no covering block",
            " has no legal start weeks for ",
        )
    )


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
