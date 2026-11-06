"""Deterministic orchestration regressions: no real search required."""
from types import SimpleNamespace

import pytest

from rbs.models.enums import SolverEngineName, SolverStatus
from rbs.models.instance import SolverProblem
from rbs.models.schedule import Assignment, Schedule, ScheduleMeta
from rbs.solver.budget import BudgetExpired, SolveBudget
from rbs.solver.core import cp_sat
from rbs.solver.core.base import empty_schedule


def _candidate(objective=0):
    return Schedule(
        meta=ScheduleMeta(academic_year="2026-2027", engine=SolverEngineName.CP_SAT,
                          status=SolverStatus.FEASIBLE, solver_objective=objective),
        assignments=[Assignment(resident_id="r", rotation_id="clinic", start_week=1,
                                end_week=1, weeks=[1])],
    )


@pytest.mark.parametrize("retry_status", [SolverStatus.UNKNOWN, SolverStatus.INFEASIBLE])
def test_relaxed_retry_has_time_and_owns_final_status(instance, monkeypatch, retry_status):
    problem = SolverProblem.from_instance(instance)
    now = [100.0]
    calls = []
    monkeypatch.setattr(cp_sat.time, "perf_counter", lambda: now[0])
    monkeypatch.setattr(cp_sat, "resolve_clinic_block_band", lambda *_: (0, 1, True))
    monkeypatch.setattr(cp_sat, "_with_infeasibility_diagnostics", lambda i, o, s, r, **kw: s)

    def portfolio(self, instance, *, deadline, options, **kwargs):
        calls.append((deadline, now[0], options.auto_balance_clinic_blocks))
        now[0] = deadline
        return empty_schedule(instance, engine=self.name,
                              status=SolverStatus.INFEASIBLE if len(calls) == 1 else retry_status,
                              notes=[])

    monkeypatch.setattr(cp_sat.CpSatEngine, "_solve_portfolio", portfolio)
    result = cp_sat.CpSatEngine().solve(
        problem, options=instance.solver.revised(time_limit_seconds=10),
    )
    assert calls == [(105.6, 100, True), (108, 105.6, False)]
    assert result.meta.status is retry_status
    assert result.meta.wall_time_seconds == 8


def test_attending_failure_keeps_alternative_candidate(monkeypatch):
    first, second = _candidate(1), _candidate(2)
    now = [100.0]
    calls = []
    monkeypatch.setattr(cp_sat.time, "perf_counter", lambda: now[0])

    def complete(instance, candidate, facts, *, options, deadline):
        calls.append((candidate, deadline))
        now[0] = deadline
        if candidate is first:
            candidate.meta.status = SolverStatus.UNKNOWN
            candidate.meta.validation_errors = ["uncovered clinic"]
        return candidate

    monkeypatch.setattr(cp_sat, "_with_attending_work", complete)
    result = cp_sat._complete_candidates(None, [second, first], {}, options=None, deadline=110)
    assert result is second
    assert calls == [(first, 105), (second, 110)]


def test_expired_compilation_is_unknown_not_infeasible(instance, monkeypatch):
    def compile(*args, **kwargs):
        raise BudgetExpired()
    monkeypatch.setattr(cp_sat, "compile_problem", compile)
    result = cp_sat.CpSatEngine()._solve_portfolio(
        SolverProblem.from_instance(instance), options=instance.solver, reference_schedule=None,
    )
    assert result.meta.status is SolverStatus.UNKNOWN
    assert not result.meta.validation_errors


def test_diagnostics_stop_probing_at_shared_deadline(instance, monkeypatch):
    from rbs.solver.core import diagnostics
    now = [100.0]
    calls = []
    monkeypatch.setattr(cp_sat.time, "perf_counter", lambda: now[0])
    monkeypatch.setattr(diagnostics, "uncoverable_clinic_sessions", lambda *a, **kw: [])

    def probe(problem, options, resident, *, deadline, **kwargs):
        calls.append(resident.id)
        now[0] = deadline
        return None

    monkeypatch.setattr(diagnostics, "_resident_curriculum_can_cover_year", probe)
    diagnostics.explain_infeasibility(
        SolverProblem.from_instance(instance), instance.solver, deadline=101,
    )
    assert len(calls) == 1


def test_compile_profile_timeout_never_becomes_zero_capacity(monkeypatch):
    from rbs.solver.core.attending_envelope import AttendingEnvelope
    envelope = object.__new__(AttendingEnvelope)
    envelope.deadline = 0
    monkeypatch.setattr(cp_sat.time, "perf_counter", lambda: 1)
    with pytest.raises(BudgetExpired):
        envelope._precepting_solve(SimpleNamespace(), None, relaxed=False)


def test_budget_reserves_retry_and_bounds_diagnostics(monkeypatch):
    monkeypatch.setattr(cp_sat.time, "perf_counter", lambda: 109)
    budget = SolveBudget(100, 10)
    assert budget.search_deadline(attendings=True) == 108
    assert budget.first_attempt_deadline(108, fallback=True) == pytest.approx(105.6)
    assert budget.diagnostic_deadline() == 110
