from __future__ import annotations

from rbs.models.instance import SolverConfig, SolverProblem
from rbs.models.schedule import Schedule
from rbs.solver.attending_availability import attending_week_facts, potential_capacity_view
from rbs.solver.core import kinds as rotation_kinds
from rbs.solver.core.attending_envelope import AttendingEnvelope
from rbs.solver.core.constraints import add_hard_constraints
from rbs.solver.core.context import CompiledProblem, ModelBuildError, PlanningContext
from rbs.solver.core.objective import add_clinic_objective
from rbs.solver.core.stability import add_reference_hints
from rbs.solver.readiness import check_solve_readiness


def compile_problem(
    instance: SolverProblem,
    options: SolverConfig,
    cp_model,
    *,
    reference_schedule: Schedule | None = None,
) -> CompiledProblem:
    # Unallocated curriculum weeks would otherwise surface as an opaque
    # "week N has no covering block" from inside placement.
    readiness = check_solve_readiness(instance)
    if not readiness.ready:
        raise ModelBuildError("; ".join(readiness.errors))
    context = PlanningContext.compile(instance, options, cp_model)
    if instance.attending_managed_clinic_ids:
        # Only attending-managed clinics couple resident placement to attending
        # work. Without one, the block model stays exactly as it was and the
        # weekly attending pass runs after the solve on its own.
        facts = attending_week_facts(instance, reference_schedule)
        context.capacity_view = potential_capacity_view(instance, facts)
        context.attending_envelope = AttendingEnvelope(
            instance,
            facts,
            context.model,
            cp_model,
            reference=reference_schedule,
        )
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
