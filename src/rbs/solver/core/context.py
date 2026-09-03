from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rbs.models.clinic import ClinicSlot
from rbs.models.instance import SolverConfig, SolverProblem
from rbs.models.resident import Resident
from rbs.models.rotation import Rotation
from rbs.models.schedule import Schedule
from rbs.solver.planning import Occurrence, expand_occurrences, legal_starts


class ModelBuildError(ValueError):
    pass


@dataclass(frozen=True)
class ClinicDecision:
    domain: tuple[ClinicSlot, ...]
    selected: tuple[Any, ...]
    pick: int

    def selected_slots(self, solver) -> list[ClinicSlot]:
        return [
            slot
            for slot, selected in zip(self.domain, self.selected, strict=True)
            if solver.Value(selected)
        ]


def new_clinic_decision(
    model, name: str, domain: list[ClinicSlot] | tuple[ClinicSlot, ...], pick: int
) -> ClinicDecision:
    slots = tuple(domain)
    if pick < 1 or pick > len(slots):
        raise ModelBuildError(f"{name}: cannot choose {pick} clinic slots from {len(slots)}")
    selected = tuple(model.NewBoolVar(f"{name}:s{index}") for index in range(len(slots)))
    model.Add(sum(selected) == pick)
    return ClinicDecision(domain=slots, selected=selected, pick=pick)


@dataclass
class ClinicModelState:
    decisions: dict[str, ClinicDecision]
    in_clinic: dict[tuple[str, int], list[tuple[tuple[str, ...], Any, Any, Any]]]
    """Clinic half-days keyed by ``(resident_id, week)``.

    A resident occupies exactly one rotation per week, so the resident is the
    stable identity here; the occurrence that supplies a literal is whichever
    one the solver placed over that week. Values are
    ``(occurrence_keys, weekday, session, literal)``, where ``occurrence_keys``
    lists every candidate occurrence the shared literal stands for.
    """

    attending_variables: list[Any]
    has_objective: bool
    stability_comparisons: int = 0
    stability_cost: Any = 0
    quality_cost: Any = 0
    quality_bound: int = 0


@dataclass(frozen=True)
class ElectiveMatchingState:
    """Lexicographic matching expressions assembled with hard constraints."""

    fallback_literals: tuple[Any, ...] = ()
    rank_literals: tuple[tuple[Any, ...], ...] = ()

    @property
    def fallback_count(self):
        return sum(self.fallback_literals)

    @property
    def rank_counts(self) -> tuple[Any, ...]:
        return tuple(sum(literals) for literals in self.rank_literals)


@dataclass
class PlanningContext:
    model: Any
    instance: SolverProblem
    options: SolverConfig
    residents: dict[str, Resident]
    rotations: dict[str, Rotation]
    occurrences: list[Occurrence]
    weeks: tuple[int, ...]
    starts: dict[str, tuple[int, ...]]
    placements: dict[tuple[str, int], Any]
    by_resident: dict[str, list[Occurrence]]
    by_rotation: dict[str, list[Occurrence]]

    @classmethod
    def compile(
        cls,
        instance: SolverProblem,
        options: SolverConfig,
        cp_model,
    ) -> PlanningContext:
        model = cp_model.CpModel()
        residents = instance.residents_by_id
        rotations = instance.rotations_by_id
        try:
            occurrences = expand_occurrences(instance)
        except ValueError as exc:
            raise ModelBuildError(str(exc)) from exc
        weeks = tuple(range(1, instance.calendar.weeks + 1))
        starts: dict[str, tuple[int, ...]] = {}
        placements: dict[tuple[str, int], Any] = {}
        by_resident = {resident_id: [] for resident_id in residents}
        by_rotation = {rotation_id: [] for rotation_id in rotations}

        for occurrence in occurrences:
            rotation = rotations[occurrence.rotation_id]
            resident = residents[occurrence.resident_id]
            legal = tuple(
                legal_starts(
                    occurrence,
                    resident,
                    rotation,
                    instance.calendar,
                    vacation_weeks=instance.resident_scheduling_vacation_weeks(resident.id),
                    allow_blocks_to_span_four_week_boundaries=(
                        options.allow_blocks_to_span_four_week_boundaries
                    ),
                )
            )
            if not legal:
                raise ModelBuildError(
                    f"{occurrence.resident_id} has no legal start weeks for "
                    f"{occurrence.rotation_id} ({occurrence.duration_weeks}wk)"
                )
            starts[occurrence.key] = legal
            by_resident[occurrence.resident_id].append(occurrence)
            by_rotation[occurrence.rotation_id].append(occurrence)
            for start in legal:
                placements[occurrence.key, start] = model.NewBoolVar(f"x:{occurrence.key}@{start}")

        return cls(
            model=model,
            instance=instance,
            options=options,
            residents=residents,
            rotations=rotations,
            occurrences=occurrences,
            weeks=weeks,
            starts=starts,
            placements=placements,
            by_resident=by_resident,
            by_rotation=by_rotation,
        )


@dataclass(frozen=True)
class CompiledProblem:
    context: PlanningContext
    clinic: ClinicModelState
    matching: ElectiveMatchingState
    reference_schedule: Schedule | None = None
