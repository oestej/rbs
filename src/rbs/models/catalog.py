from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import Field, model_validator

from rbs.models.clinic import ALL_CLINIC_SITES, ClinicPolicy, ClinicRule
from rbs.models.common import StrictModel
from rbs.models.curriculum import PGYCurriculum, RotationGroup
from rbs.models.elective import (
    ElectiveConfiguration,
    apply_elective_option_defaults,
    apply_shared_elective_color,
)
from rbs.models.enums import RotationKind
from rbs.models.rotation import Rotation

if TYPE_CHECKING:
    from rbs.models.instance import SchedulerInput, SchedulingCase, SolverProblem


class ConstraintCatalog(StrictModel):
    """Versioned block constraints that can be imported and stored independently."""

    schema_version: Literal[5] = 5
    calendar_weeks: int = Field(default=52, ge=1)
    rotations: list[Rotation]
    requirements: list[PGYCurriculum] = Field(min_length=1)
    rotation_groups: list[RotationGroup] = Field(default_factory=list)
    electives: ElectiveConfiguration
    clinic_policy: ClinicPolicy

    @model_validator(mode="after")
    def check_integrity(self) -> ConstraintCatalog:
        self.electives = apply_elective_option_defaults(
            self.rotations,
            self.requirements,
            self.electives,
        )
        self.rotations = apply_shared_elective_color(self.rotations, self.electives)
        validate_catalog_integrity(
            self.rotations,
            self.requirements,
            self.rotation_groups,
            self.calendar_weeks,
            self.clinic_policy,
            self.electives,
        )
        return self

    @classmethod
    def from_instance(cls, instance: SolverProblem) -> ConstraintCatalog:
        return cls(
            schema_version=5,
            calendar_weeks=instance.calendar.weeks,
            rotations=instance.rotations,
            requirements=instance.requirements,
            rotation_groups=instance.rotation_groups,
            electives=instance.electives,
            clinic_policy=instance.clinic_policy,
        )

    def apply(self, case: SchedulingCase) -> SchedulerInput:
        from rbs.models.instance import SchedulerInput

        if case.calendar.weeks != self.calendar_weeks:
            raise ValueError(
                f"catalog models {self.calendar_weeks} weeks, "
                f"but workspace calendar has {case.calendar.weeks}"
            )
        return SchedulerInput(
            **case.model_dump(),
            rotations=self.rotations,
            requirements=self.requirements,
            rotation_groups=self.rotation_groups,
            electives=self.electives,
            clinic_policy=self.clinic_policy,
        )


def validate_catalog_integrity(
    rotations: list[Rotation],
    requirements: list[PGYCurriculum],
    rotation_groups: list[RotationGroup],
    calendar_weeks: int,
    clinic_policy: ClinicPolicy,
    electives: ElectiveConfiguration,
) -> None:
    """Validate cross-record catalog invariants through focused rule groups."""
    by_rotation = _index_rotations(rotations)
    known = set(by_rotation)
    elective_pgys = _elective_pgys_by_duration(requirements, by_rotation)
    known_levels = {curriculum.pgy for curriculum in requirements}
    _validate_elective_options(
        electives,
        by_rotation,
        elective_pgys,
        known_levels,
    )
    _validate_clinic_references(rotations, set(clinic_policy.site_ids))
    _validate_rotation_groups(rotation_groups, by_rotation, requirements)

    pgys = [item.pgy for item in requirements]
    if len(pgys) != len(set(pgys)):
        raise ValueError("requirements must have one entry per training level")
    labels = [item.display_label.casefold() for item in requirements]
    if len(labels) != len(set(labels)):
        raise ValueError("training-level labels must be unique")
    codes = [item.short_code.casefold() for item in requirements]
    if len(codes) != len(set(codes)):
        raise ValueError("training-level codes must be unique")
    _validate_training_level_references(rotations, clinic_policy, set(pgys))

    offered_by_pgy: dict[int, set[str]] = {}
    for curriculum in requirements:
        offered_by_pgy[curriculum.pgy] = _validate_curriculum(
            curriculum,
            calendar_weeks,
            by_rotation,
            electives,
        )
    _validate_placement_prerequisites(
        rotations,
        known,
        offered_by_pgy,
        calendar_weeks,
        {curriculum.pgy: curriculum.short_code for curriculum in requirements},
    )


def _validate_training_level_references(
    rotations: list[Rotation],
    clinic_policy: ClinicPolicy,
    known_levels: set[int],
) -> None:
    referenced: set[int] = set()
    for rotation in rotations:
        referenced.update(rule.pgy for rule in rotation.pgy_rules)
        if rotation.clinic is not None:
            referenced.update(rotation.clinic.max_concurrent_by_pgy)
    referenced.update(rule.pgy for rule in clinic_policy.allocation_rules if rule.pgy is not None)
    unknown = sorted(referenced - known_levels)
    if unknown:
        raise ValueError(
            "rules reference unknown training-level key(s): "
            + ", ".join(str(level) for level in unknown)
        )


def _index_rotations(rotations: list[Rotation]) -> dict[str, Rotation]:
    rotation_ids = [rotation.id for rotation in rotations]
    if len(rotation_ids) != len(set(rotation_ids)):
        raise ValueError("rotation ids must be unique")
    rotation_codes = [rotation.code.casefold() for rotation in rotations]
    if len(rotation_codes) != len(set(rotation_codes)):
        raise ValueError("rotation codes must be unique (case-insensitive)")
    return {rotation.id: rotation for rotation in rotations}


def _validate_rotation_groups(
    groups: list[RotationGroup],
    by_rotation: dict[str, Rotation],
    requirements: list[PGYCurriculum],
) -> None:
    """Validate unordered group membership against direct Mandatory requirements."""
    curricula = {curriculum.pgy: curriculum for curriculum in requirements}
    grouped: set[tuple[int, str]] = set()
    for group in groups:
        curriculum = curricula.get(group.pgy)
        if curriculum is None:
            raise ValueError(f"rotation group references unknown training-level key {group.pgy}")
        counts: dict[str, int] = {}
        for rotation_id in group.rotation_ids:
            rotation = by_rotation.get(rotation_id)
            if rotation is None:
                raise ValueError(f"rotation group references unknown rotation {rotation_id!r}")
            if rotation.kind is not RotationKind.STANDARD:
                raise ValueError(
                    f"rotation groups may contain only Mandatory rotations: {rotation_id!r}"
                )
            try:
                rotation.pgy_rule(group.pgy)
            except KeyError as exc:
                raise ValueError(
                    f"rotation group member {rotation_id!r} is unavailable to "
                    f"{curriculum.short_code}"
                ) from exc
            key = (group.pgy, rotation_id)
            if key in grouped:
                raise ValueError(
                    f"{curriculum.short_code} rotation {rotation_id!r} belongs to "
                    "more than one rotation group"
                )
            grouped.add(key)
            count = sum(
                block.count for block in curriculum.blocks if block.rotation_id == rotation_id
            )
            if count == 0:
                raise ValueError(
                    f"{curriculum.short_code} rotation group member {rotation_id!r} "
                    "must be a direct Mandatory requirement"
                )
            counts[rotation_id] = count
        if len(set(counts.values())) != 1:
            detail = ", ".join(f"{rotation_id}={count}" for rotation_id, count in counts.items())
            raise ValueError(
                f"{curriculum.short_code} rotation group members must have equal "
                f"occurrence counts ({detail})"
            )


def _elective_pgys_by_duration(
    requirements: list[PGYCurriculum],
    by_rotation: dict[str, Rotation],
) -> dict[int, set[int]]:
    result: dict[int, set[int]] = {}
    for curriculum in requirements:
        for item in curriculum.blocks:
            rotation = by_rotation.get(item.rotation_id)
            if rotation is not None and rotation.kind is RotationKind.ELECTIVE:
                result.setdefault(item.duration_weeks, set()).add(curriculum.pgy)
    return result


def _validate_elective_options(
    electives: ElectiveConfiguration,
    by_rotation: dict[str, Rotation],
    elective_pgys: dict[int, set[int]],
    known_levels: set[int],
) -> None:
    known = set(by_rotation)
    unknown = set(electives.eligible_rotation_ids) - known
    if unknown:
        raise ValueError(
            "eligible elective rotations reference unknown rotation(s): "
            + ", ".join(sorted(unknown))
        )
    invalid = [
        rotation_id
        for rotation_id in electives.eligible_rotation_ids
        if by_rotation[rotation_id].kind
        not in {RotationKind.STANDARD, RotationKind.ELECTIVE, RotationKind.FMED}
    ]
    if invalid:
        raise ValueError(
            "eligible elective rotations must be standalone Elective, Mandatory, "
            "or FMED services: " + ", ".join(invalid)
        )
    for option in electives.rotation_options:
        _validate_elective_option(
            option,
            by_rotation[option.rotation_id],
            elective_pgys,
            known_levels,
        )


def _validate_elective_option(
    option,
    rotation: Rotation,
    elective_pgys: dict[int, set[int]],
    known_levels: set[int],
) -> None:
    if not option.eligible_pgys:
        raise ValueError(
            f"eligible elective rotation {option.rotation_id!r} must select at "
            "least one eligible training level"
        )
    if not option.eligible_block_sizes:
        raise ValueError(
            f"eligible elective rotation {option.rotation_id!r} must select at "
            "least one eligible Elective block size"
        )
    unknown_pgys = set(option.eligible_pgys) - known_levels
    if unknown_pgys:
        labels = ", ".join(str(pgy) for pgy in sorted(unknown_pgys))
        raise ValueError(
            f"eligible elective rotation {option.rotation_id!r} selects training "
            f"level(s) not present in the curriculum: {labels}"
        )
    unavailable = {
        duration
        for duration in option.eligible_block_sizes
        if not (elective_pgys.get(duration, set()) & set(option.eligible_pgys))
    }
    if unavailable:
        labels = ", ".join(str(size) for size in sorted(unavailable))
        raise ValueError(
            f"eligible elective rotation {option.rotation_id!r} selects block "
            f"size(s) not present in the Elective curriculum: {labels}"
        )
    unsupported = [
        pgy
        for pgy in option.eligible_pgys
        if not any(
            duration in option.eligible_block_sizes and rotation.allows_duration(duration, pgy=pgy)
            for duration in option.eligible_block_sizes
        )
    ]
    if unsupported:
        labels = ", ".join(str(pgy) for pgy in unsupported)
        raise ValueError(
            f"eligible elective rotation {option.rotation_id!r} has no matching "
            f"Elective block configuration for training level(s): {labels}"
        )


def _validate_clinic_references(
    rotations: list[Rotation],
    known_sites: set[str],
) -> None:
    def validate_rule(label: str, rule: ClinicRule | None) -> None:
        if rule is None:
            return
        for slot in rule.slots:
            unknown = [
                site_id
                for site_id in slot.sites
                if site_id != ALL_CLINIC_SITES and site_id not in known_sites
            ]
            if unknown:
                raise ValueError(f"{label} references unknown clinic site {unknown[0]!r}")

    for rotation in rotations:
        validate_rule(rotation.id, rotation.clinic)


def _validate_curriculum(
    curriculum: PGYCurriculum,
    calendar_weeks: int,
    by_rotation: dict[str, Rotation],
    electives: ElectiveConfiguration,
) -> set[str]:
    offered = _offered_rotations(curriculum, by_rotation, electives)
    for block in curriculum.blocks:
        _validate_curriculum_block(
            curriculum.pgy,
            curriculum.short_code,
            block,
            by_rotation,
        )
    if curriculum.blocks:
        weeks = curriculum.required_weeks()
        if weeks != calendar_weeks:
            raise ValueError(
                f"{curriculum.short_code} curriculum covers {weeks} weeks, "
                f"expected {calendar_weeks}"
            )
    return offered


def _offered_rotations(
    curriculum: PGYCurriculum,
    by_rotation: dict[str, Rotation],
    electives: ElectiveConfiguration,
) -> set[str]:
    offered = {item.rotation_id for item in curriculum.blocks}
    elective_durations = {
        item.duration_weeks
        for item in curriculum.blocks
        if item.rotation_id in by_rotation
        and by_rotation[item.rotation_id].kind is RotationKind.ELECTIVE
    }
    for duration in elective_durations:
        offered.update(
            option.rotation_id
            for option in electives.rotation_options
            if option.allows(curriculum.pgy, duration)
            and by_rotation[option.rotation_id].allows_duration(
                duration,
                pgy=curriculum.pgy,
            )
        )
    return offered


def _validate_curriculum_block(
    pgy: int,
    level_code: str,
    block,
    by_rotation: dict[str, Rotation],
) -> None:
    rotation = by_rotation.get(block.rotation_id)
    if rotation is None:
        raise ValueError(f"{level_code} references unknown rotation {block.rotation_id!r}")
    if rotation.allows_duration(block.duration_weeks, pgy=pgy):
        return
    try:
        configured = rotation.configured_durations(pgy)
    except KeyError:
        configured = []
    raise ValueError(
        f"{level_code} {block.rotation_id}: duration {block.duration_weeks} not in {configured}"
    )


def _validate_placement_prerequisites(
    rotations: list[Rotation],
    known: set[str],
    offered_by_pgy: dict[int, set[str]],
    calendar_weeks: int,
    level_codes: dict[int, str],
) -> None:
    graphs: dict[int, dict[str, set[str]]] = {}
    for rotation in rotations:
        for rule in rotation.pgy_rules:
            _validate_placement_rule(
                rotation.id,
                rule,
                known,
                offered_by_pgy.get(rule.pgy, set()),
                calendar_weeks,
                level_codes.get(rule.pgy, f"training level {rule.pgy}"),
            )
            graphs.setdefault(rule.pgy, {})[rotation.id] = set(rule.prerequisite_rotation_ids)
    for pgy, graph in graphs.items():
        _validate_acyclic_prerequisites(
            level_codes.get(pgy, f"training level {pgy}"),
            graph,
        )


def _validate_placement_rule(
    rotation_id: str,
    rule,
    known: set[str],
    offered: set[str],
    calendar_weeks: int,
    level_code: str,
) -> None:
    if rule.earliest_start_week is not None and rule.earliest_start_week > calendar_weeks:
        raise ValueError(
            f"{level_code} {rotation_id}: earliest block start week "
            f"{rule.earliest_start_week} exceeds calendar week {calendar_weeks}"
        )
    for predecessor in rule.prerequisite_rotation_ids:
        if predecessor == rotation_id:
            raise ValueError(f"{level_code} {rotation_id}: a rotation cannot depend on itself")
        if predecessor not in known:
            raise ValueError(
                f"{level_code} {rotation_id}: prerequisite references unknown "
                f"rotation {predecessor!r}"
            )
        if predecessor not in offered:
            raise ValueError(
                f"{level_code} {rotation_id}: prerequisite {predecessor!r} is not "
                "in this training-level curriculum"
            )


def _validate_acyclic_prerequisites(
    level_code: str,
    graph: dict[str, set[str]],
) -> None:
    visiting: list[str] = []
    complete: set[str] = set()

    def visit(rotation_id: str) -> None:
        if rotation_id in complete:
            return
        if rotation_id in visiting:
            cycle = visiting[visiting.index(rotation_id) :] + [rotation_id]
            raise ValueError(f"{level_code} rotation prerequisite cycle: {' -> '.join(cycle)}")
        visiting.append(rotation_id)
        for predecessor in graph.get(rotation_id, set()):
            if predecessor in graph:
                visit(predecessor)
        visiting.pop()
        complete.add(rotation_id)

    for rotation_id in graph:
        visit(rotation_id)
