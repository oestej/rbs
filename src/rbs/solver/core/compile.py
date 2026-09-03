from __future__ import annotations

from rbs.models.instance import SolverConfig, SolverProblem
from rbs.models.schedule import Schedule
from rbs.solver.core import kinds as rotation_kinds
from rbs.solver.core.constraints import add_hard_constraints
from rbs.solver.core.context import CompiledProblem, PlanningContext
from rbs.solver.core.objective import add_clinic_objective
from rbs.solver.core.stability import add_reference_hints


def compile_problem(
    instance: SolverProblem,
    options: SolverConfig,
    cp_model,
    *,
    reference_schedule: Schedule | None = None,
) -> CompiledProblem:
    context = PlanningContext.compile(instance, options, cp_model)
    matching = add_hard_constraints(context)
    decisions = rotation_kinds.apply_constraints(context)
    clinic = add_clinic_objective(
        context,
        decisions,
        reference_schedule=reference_schedule,
    )
    # The clinic objective can add flexible overlay decisions. Hint only after
    # it has finished so the warm start covers those choices as well as block
    # placement and dedicated Clinic decisions.
    add_reference_hints(context, clinic.decisions, reference_schedule)
    return CompiledProblem(
        context=context,
        clinic=clinic,
        matching=matching,
        reference_schedule=reference_schedule,
    )
