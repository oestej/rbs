"""Rotation, curriculum, and manual-block edits on a scheduling instance.

Pure ``SchedulerInput -> SchedulerInput`` operations. Kept free of NiceGUI so
the scheduling rules they encode can be read and tested without a UI.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

from rbs.models.curriculum import RotationGroup
from rbs.models.elective import ElectiveConfiguration, ElectiveRotationOption
from rbs.models.enums import RotationKind
from rbs.models.instance import (
    ManualClinicBlock,
    ResidentRotationOverride,
    SchedulerInput,
)
from rbs.models.rotation import Rotation
from rbs.models.schedule import Schedule
from rbs.ui.drafts import Draft
from rbs.ui.locks import ScheduleBlock, schedule_blocks

__all__ = [
    "standard_rotations",
    "special_rotations",
    "elective_rotations",
    "rotation_editor_state",
    "rotation_from_editor_state",
    "next_mandatory_rotation_id",
    "add_mandatory_rotation",
    "remove_mandatory_rotation",
    "replace_standard_rotation",
    "replace_rotation_color",
    "replace_elective_color",
    "set_elective_eligibility",
    "add_elective_rotation",
    "replace_elective_rotation",
    "remove_elective_rotation",
    "replace_clinic_rotation",
    "replace_clinic_block_rules",
    "replace_fmed_pgy_rules",
    "add_manual_clinic_block",
    "remove_manual_clinic_block",
    "resident_missing_mandatory_rotations",
    "resident_rotation_week_totals",
    "rotation_group_members_by_pgy",
]


def standard_rotations(instance: SchedulerInput) -> list[Rotation]:
    """Rotations whose configuration is handled by the generic editor."""
    return sorted(
        (
            rotation
            for rotation in instance.rotations
            if not rotation.requires_dedicated_configuration
        ),
        key=lambda rotation: rotation.code.casefold(),
    )


def special_rotations(instance: SchedulerInput) -> list[Rotation]:
    """Rotations that require purpose-built configuration."""
    return sorted(
        (rotation for rotation in instance.rotations if rotation.requires_dedicated_configuration),
        key=lambda rotation: rotation.code.casefold(),
    )


def elective_rotations(instance: SchedulerInput) -> list[Rotation]:
    """Standalone services configured specifically for Elective time."""
    return sorted(
        (
            rotation
            for rotation in instance.rotations
            if rotation.kind is RotationKind.ELECTIVE and instance.is_elective_option(rotation.id)
        ),
        key=lambda rotation: rotation.code.casefold(),
    )


def rotation_editor_state(rotation: Rotation) -> Draft:
    """Return a detached, JSON-compatible draft containing every rotation field."""
    return rotation.model_dump(mode="json")


def rotation_from_editor_state(state: Draft) -> Rotation:
    """Build and validate a typed rotation from a UI draft."""
    return Rotation.model_validate(state)


def rotation_group_members_by_pgy(
    instance: SchedulerInput,
    rotation_id: str,
) -> dict[int, list[str]]:
    """Return the complete configured group for this rotation at each level."""
    return {
        pgy: (
            list(group.rotation_ids)
            if (group := instance.rotation_group_for(pgy, rotation_id)) is not None
            else []
        )
        for pgy in instance.training_level_ids
    }


def next_mandatory_rotation_id(instance: SchedulerInput, name: str) -> str:
    """Return a readable, stable ID that does not collide with existing rotations."""
    base = re.sub(r"[^a-z0-9]+", "_", name.strip().casefold()).strip("_")
    base = base or "mandatory_rotation"
    used = set(instance.rotations_by_id) | set(instance.special_rotations_by_id)
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def add_mandatory_rotation(
    instance: SchedulerInput,
    rotation: Rotation,
    counts: dict[tuple[int, int], int],
    *,
    eligible_as_elective: bool = False,
    eligible_elective_pgys: Iterable[int] | None = None,
    eligible_elective_block_sizes: Iterable[int] | None = None,
    elective_repeatable: bool = False,
    group_members_by_pgy: dict[int, list[str]] | None = None,
) -> SchedulerInput:
    """Add one standard rotation and replace Elective weeks with its requirements."""
    if rotation.kind is not RotationKind.STANDARD:
        raise ValueError("new Mandatory rotations must use the standard rotation kind")
    if rotation.id in instance.rotations_by_id:
        raise ValueError(f"rotation ID {rotation.id!r} is already configured")
    if any(existing.code.casefold() == rotation.code.casefold() for existing in instance.rotations):
        raise ValueError(f"rotation code {rotation.code!r} is already configured")

    requirements: dict[tuple[int, int], int] = {}
    configured_pgys = {rule.pgy for rule in rotation.pgy_rules}
    known_pgys = {curriculum.pgy for curriculum in instance.requirements}
    for (pgy, duration), raw_count in counts.items():
        count = int(raw_count)
        if count < 0:
            raise ValueError("Mandatory rotation block counts cannot be negative")
        if not count:
            continue
        level_code = instance.training_level_label(pgy, compact=True)
        if pgy not in known_pgys:
            raise ValueError(f"training level {pgy} has no configured curriculum")
        if pgy not in configured_pgys:
            raise ValueError(f"{rotation.code} has no {level_code} rotation rule")
        try:
            rotation.block_config(pgy, duration)
        except KeyError as exc:
            raise ValueError(
                f"{rotation.code} does not allow {duration}-week blocks for {level_code}"
            ) from exc
        if duration > rotation.max_consecutive_weeks:
            raise ValueError(
                f"{rotation.code}'s {duration}-week {level_code} block exceeds its "
                f"{rotation.max_consecutive_weeks}-week consecutive limit"
            )
        requirements[pgy, duration] = count
    if not requirements:
        raise ValueError("select at least one training-level requirement")

    raw = instance.model_dump(mode="json")
    raw["rotations"].append(rotation.model_dump(mode="json"))
    if eligible_as_elective:
        sizes = _normalize_elective_block_sizes(
            instance,
            rotation,
            eligible_elective_block_sizes,
        )
        raw["electives"]["rotation_options"].append(
            ElectiveRotationOption(
                rotation_id=rotation.id,
                eligible_pgys=_normalize_elective_pgys(
                    instance,
                    rotation,
                    eligible_elective_pgys,
                ),
                eligible_block_sizes=sizes,
                repeatable=elective_repeatable,
            ).model_dump(mode="json")
        )
    for curriculum in raw["requirements"]:
        pgy = int(curriculum["pgy"])
        additions = [
            (duration, count)
            for (required_pgy, duration), count in requirements.items()
            if required_pgy == pgy
        ]
        added_weeks = sum(duration * count for duration, count in additions)
        if not added_weeks:
            continue
        curriculum["blocks"] = _balance_curriculum_against_electives(
            instance,
            pgy,
            list(curriculum["blocks"]),
            delta_weeks=added_weeks,
            requirement_label=rotation.name,
        )
        curriculum["blocks"].extend(
            {
                "rotation_id": rotation.id,
                "duration_weeks": duration,
                "count": count,
            }
            for duration, count in sorted(additions)
        )
    if group_members_by_pgy is not None:
        raw["rotation_groups"] = [
            group.model_dump(mode="json")
            for group in _replace_rotation_group_members(
                instance,
                rotation.id,
                group_members_by_pgy,
                extra_rotation=rotation,
            )
        ]
    return SchedulerInput.from_payload(raw)


def remove_mandatory_rotation(
    instance: SchedulerInput,
    rotation_id: str,
) -> SchedulerInput:
    """Remove one standard rotation and repair every reference to it."""
    try:
        rotation = instance.rotation(rotation_id)
    except KeyError as exc:
        raise ValueError(f"unknown rotation {rotation_id!r}") from exc
    if rotation.kind is not RotationKind.STANDARD:
        raise ValueError("only Mandatory rotations can be removed here")

    raw = instance.model_dump(mode="json")
    for curriculum in raw["requirements"]:
        pgy = int(curriculum["pgy"])
        removed_direct_weeks = sum(
            int(block["duration_weeks"]) * int(block["count"])
            for block in curriculum["blocks"]
            if block["rotation_id"] == rotation_id
        )
        blocks = [block for block in curriculum["blocks"] if block["rotation_id"] != rotation_id]
        restored_weeks = removed_direct_weeks
        if restored_weeks:
            blocks = _balance_curriculum_against_electives(
                instance,
                pgy,
                blocks,
                delta_weeks=-restored_weeks,
            )
        curriculum["blocks"] = blocks

    raw["rotations"] = [
        configured for configured in raw["rotations"] if configured["id"] != rotation_id
    ]
    raw["rotation_groups"] = [
        {**group, "rotation_ids": remaining}
        for group in raw.get("rotation_groups", [])
        if len(
            remaining := [
                member for member in group.get("rotation_ids", []) if member != rotation_id
            ]
        )
        >= 2
    ]
    raw["electives"]["rotation_options"] = [
        option
        for option in raw["electives"]["rotation_options"]
        if option["rotation_id"] != rotation_id
    ]
    for configured in raw["rotations"]:
        for rule in configured["pgy_rules"]:
            rule["prerequisite_rotation_ids"] = [
                prerequisite
                for prerequisite in rule.get("prerequisite_rotation_ids", [])
                if prerequisite != rotation_id
            ]
    raw["locks"] = [lock for lock in raw["locks"] if lock["rotation_id"] != rotation_id]
    removed_override_groups = {
        (str(override["resident_id"]), str(group_instance_id))
        for override in raw["resident_rotation_overrides"]
        if (
            override["rotation_id"] == rotation_id
            or override["replaces_rotation_id"] == rotation_id
        )
        and (group_instance_id := override.get("group_instance_id")) is not None
    }
    raw["resident_rotation_overrides"] = [
        override
        for override in raw["resident_rotation_overrides"]
        if override["rotation_id"] != rotation_id
        and override["replaces_rotation_id"] != rotation_id
        and (
            override.get("group_instance_id") is None
            or (
                str(override["resident_id"]),
                str(override["group_instance_id"]),
            )
            not in removed_override_groups
        )
    ]
    return SchedulerInput.from_payload(raw)


def replace_standard_rotation(
    instance: SchedulerInput,
    original_id: str,
    replacement: Rotation,
    *,
    resident_overrides: list[ResidentRotationOverride | Draft] | None = None,
    eligible_as_elective: bool | None = None,
    eligible_elective_pgys: Iterable[int] | None = None,
    eligible_elective_block_sizes: Iterable[int] | None = None,
    elective_repeatable: bool | None = None,
    group_members_by_pgy: dict[int, list[str]] | None = None,
) -> SchedulerInput:
    """Replace a standard rotation and validate all catalog references together."""
    try:
        original = instance.rotation(original_id)
    except KeyError as exc:
        raise ValueError(f"unknown rotation {original_id!r}") from exc
    if original.requires_dedicated_configuration:
        raise ValueError(f"{original.name} requires its dedicated configuration section")
    if replacement.id != original_id:
        raise ValueError("rotation ID is a system key and cannot be changed")
    if replacement.kind is not RotationKind.STANDARD:
        raise ValueError("a standard rotation cannot be changed into a special rotation")

    rotations = [
        replacement if rotation.id == original_id else rotation for rotation in instance.rotations
    ]
    updates: dict[str, Any] = {"rotations": rotations}
    if group_members_by_pgy is not None:
        updates["rotation_groups"] = _replace_rotation_group_members(
            instance,
            original_id,
            group_members_by_pgy,
            extra_rotation=replacement,
        )
    if eligible_as_elective is not None:
        options = [
            option
            for option in instance.electives.rotation_options
            if option.rotation_id != original_id
        ]
        if eligible_as_elective:
            original_option = instance.electives.option_for(original_id)
            configured_sizes = (
                eligible_elective_block_sizes
                if eligible_elective_block_sizes is not None
                else instance.electives.block_sizes_for(original_id) or None
            )
            options.append(
                ElectiveRotationOption(
                    rotation_id=original_id,
                    eligible_pgys=_normalize_elective_pgys(
                        instance,
                        replacement,
                        (
                            eligible_elective_pgys
                            if eligible_elective_pgys is not None
                            else (
                                original_option.eligible_pgys
                                if original_option is not None
                                else None
                            )
                        ),
                    ),
                    eligible_block_sizes=_normalize_elective_block_sizes(
                        instance,
                        replacement,
                        configured_sizes,
                    ),
                    repeatable=(
                        elective_repeatable
                        if elective_repeatable is not None
                        else bool(original_option and original_option.repeatable)
                    ),
                )
            )
        updates["electives"] = ElectiveConfiguration(
            color=instance.electives.color,
            rotation_options=options,
        )
    if resident_overrides is not None:
        normalized = [
            override
            if isinstance(override, ResidentRotationOverride)
            else ResidentRotationOverride.model_validate(override)
            for override in resident_overrides
        ]
        for override in normalized:
            if override.rotation_id == original_id:
                continue
            resident = instance.residents_by_id.get(override.resident_id)
            group = (
                instance.rotation_group_for(resident.pgy, original_id)
                if resident is not None and override.group_instance_id is not None
                else None
            )
            if group is None or override.rotation_id not in group.rotation_ids:
                raise ValueError("resident override belongs to a different rotation")
        updates["resident_rotation_overrides"] = [
            override
            for override in instance.resident_rotation_overrides
            if not _resident_override_managed_by_rotation(
                instance,
                override,
                original_id,
            )
        ] + normalized
    return instance.revised(**updates)


def _resident_override_managed_by_rotation(
    instance: SchedulerInput,
    override: ResidentRotationOverride,
    rotation_id: str,
) -> bool:
    if override.rotation_id == rotation_id:
        return True
    if override.group_instance_id is None:
        return False
    resident = instance.residents_by_id.get(override.resident_id)
    if resident is None:
        return False
    group = instance.rotation_group_for(resident.pgy, rotation_id)
    return group is not None and override.rotation_id in group.rotation_ids


def _replace_rotation_group_members(
    instance: SchedulerInput,
    rotation_id: str,
    members_by_pgy: dict[int, list[str]],
    *,
    extra_rotation: Rotation | None = None,
) -> list[RotationGroup]:
    """Replace this rotation's level-specific group memberships atomically."""
    known = set(instance.rotations_by_id)
    if extra_rotation is not None:
        known.add(extra_rotation.id)
    requested_pgys = {int(pgy) for pgy in members_by_pgy}
    groups = [
        group
        for group in instance.rotation_groups
        if not (group.pgy in requested_pgys and rotation_id in group.rotation_ids)
    ]
    for raw_pgy, raw_members in sorted(members_by_pgy.items()):
        pgy = int(raw_pgy)
        members = list(dict.fromkeys([rotation_id, *(str(item) for item in raw_members)]))
        if len(members) < 2:
            continue
        unknown = set(members) - known
        if unknown:
            raise ValueError(
                "rotation group references unknown rotation(s): " + ", ".join(sorted(unknown))
            )
        conflict = next(
            (
                group
                for group in groups
                if group.pgy == pgy and set(group.rotation_ids) & set(members)
            ),
            None,
        )
        if conflict is not None:
            raise ValueError(
                f"a selected rotation already belongs to another "
                f"{instance.training_level_label(pgy, compact=True)} group"
            )
        groups.append(RotationGroup(pgy=pgy, rotation_ids=members))
    return groups


def replace_rotation_color(
    instance: SchedulerInput,
    rotation_id: str,
    color: str,
) -> SchedulerInput:
    """Replace only a rotation's configured block-schedule color."""
    try:
        original = instance.rotation(rotation_id)
    except KeyError as exc:
        raise ValueError(f"unknown rotation {rotation_id!r}") from exc
    if original.kind is RotationKind.ELECTIVE:
        return replace_elective_color(instance, color)
    replacement = Rotation.model_validate({**original.model_dump(mode="json"), "color": color})
    rotations = [
        replacement if rotation.id == rotation_id else rotation for rotation in instance.rotations
    ]
    return instance.revised(rotations=rotations)


def replace_elective_color(
    instance: SchedulerInput,
    color: str,
) -> SchedulerInput:
    """Replace the shared standalone-Elective color atomically."""
    configuration = ElectiveConfiguration.model_validate(
        {**instance.electives.model_dump(mode="json"), "color": color}
    )
    rotations = [
        Rotation.model_validate({**rotation.model_dump(mode="json"), "color": configuration.color})
        if rotation.kind is RotationKind.ELECTIVE
        else rotation
        for rotation in instance.rotations
    ]
    return instance.revised(rotations=rotations, electives=configuration)


def set_elective_eligibility(
    instance: SchedulerInput,
    rotation_id: str,
    *,
    eligible: bool,
    eligible_pgys: Iterable[int] | None = None,
    eligible_block_sizes: Iterable[int] | None = None,
    repeatable: bool | None = None,
) -> SchedulerInput:
    """Enable or disable one configured service as an Elective option."""
    try:
        rotation = instance.rotation(rotation_id)
    except KeyError as exc:
        raise ValueError(f"unknown rotation {rotation_id!r}") from exc
    if rotation.kind not in {
        RotationKind.STANDARD,
        RotationKind.ELECTIVE,
        RotationKind.FMED,
    }:
        raise ValueError("only Mandatory, standalone Elective, or FMED services can be eligible")
    options = [
        option
        for option in instance.electives.rotation_options
        if option.rotation_id != rotation_id
    ]
    if eligible:
        original_option = instance.electives.option_for(rotation_id)
        configured_sizes = (
            eligible_block_sizes
            if eligible_block_sizes is not None
            else instance.electives.block_sizes_for(rotation_id) or None
        )
        options.append(
            ElectiveRotationOption(
                rotation_id=rotation_id,
                eligible_pgys=_normalize_elective_pgys(
                    instance,
                    rotation,
                    (
                        eligible_pgys
                        if eligible_pgys is not None
                        else (
                            original_option.eligible_pgys if original_option is not None else None
                        )
                    ),
                ),
                eligible_block_sizes=_normalize_elective_block_sizes(
                    instance,
                    rotation,
                    configured_sizes,
                ),
                repeatable=(
                    repeatable
                    if repeatable is not None
                    else bool(original_option and original_option.repeatable)
                ),
            )
        )
    configuration = ElectiveConfiguration(
        color=instance.electives.color,
        rotation_options=options,
    )
    return instance.revised(electives=configuration)


def add_elective_rotation(
    instance: SchedulerInput,
    rotation: Rotation,
    *,
    eligible_pgys: Iterable[int] | None = None,
    eligible_block_sizes: Iterable[int] | None = None,
    repeatable: bool = False,
) -> SchedulerInput:
    """Add a standalone Elective service and make it eligible immediately."""
    if rotation.kind is not RotationKind.ELECTIVE:
        raise ValueError("new Elective rotations must use the elective rotation kind")
    if rotation.id in instance.rotations_by_id:
        raise ValueError(f"rotation ID {rotation.id!r} is already configured")
    if any(existing.code.casefold() == rotation.code.casefold() for existing in instance.rotations):
        raise ValueError(f"rotation code {rotation.code!r} is already configured")
    normalized = Rotation.model_validate(
        {**rotation.model_dump(mode="json"), "color": instance.electives.color}
    )
    configuration = ElectiveConfiguration(
        color=instance.electives.color,
        rotation_options=[
            *instance.electives.rotation_options,
            ElectiveRotationOption(
                rotation_id=normalized.id,
                eligible_pgys=_normalize_elective_pgys(
                    instance,
                    normalized,
                    eligible_pgys,
                ),
                eligible_block_sizes=_normalize_elective_block_sizes(
                    instance,
                    normalized,
                    eligible_block_sizes,
                ),
                repeatable=repeatable,
            ),
        ],
    )
    return instance.revised(
        rotations=[*instance.rotations, normalized],
        electives=configuration,
    )


def replace_elective_rotation(
    instance: SchedulerInput,
    original_id: str,
    replacement: Rotation,
    *,
    eligible_pgys: Iterable[int] | None = None,
    eligible_block_sizes: Iterable[int] | None = None,
    repeatable: bool | None = None,
) -> SchedulerInput:
    """Replace a standalone Elective service."""
    try:
        original = instance.rotation(original_id)
    except KeyError as exc:
        raise ValueError(f"unknown rotation {original_id!r}") from exc
    if original.kind is not RotationKind.ELECTIVE:
        raise ValueError(f"{original.name} is not a standalone Elective rotation")
    if replacement.id != original_id:
        raise ValueError("rotation ID is a system key and cannot be changed")
    if replacement.kind is not RotationKind.ELECTIVE:
        raise ValueError("standalone Elective rules must remain Elective rules")
    normalized = Rotation.model_validate(
        {**replacement.model_dump(mode="json"), "color": instance.electives.color}
    )
    original_option = instance.electives.option_for(original_id)
    configured_sizes = (
        eligible_block_sizes
        if eligible_block_sizes is not None
        else instance.electives.block_sizes_for(original_id) or None
    )
    options = [
        ElectiveRotationOption(
            rotation_id=original_id,
            eligible_pgys=_normalize_elective_pgys(
                instance,
                normalized,
                (
                    eligible_pgys
                    if eligible_pgys is not None
                    else (original_option.eligible_pgys if original_option is not None else None)
                ),
            ),
            eligible_block_sizes=_normalize_elective_block_sizes(
                instance,
                normalized,
                configured_sizes,
            ),
            repeatable=(
                repeatable
                if repeatable is not None
                else bool(original_option and original_option.repeatable)
            ),
        )
        if option.rotation_id == original_id
        else option
        for option in instance.electives.rotation_options
    ]
    return instance.revised(
        rotations=[
            normalized if rotation.id == original_id else rotation
            for rotation in instance.rotations
        ],
        electives=ElectiveConfiguration(
            color=instance.electives.color,
            rotation_options=options,
        ),
    )


def remove_elective_rotation(
    instance: SchedulerInput,
    rotation_id: str,
) -> SchedulerInput:
    """Remove a configured standalone Elective service and its references."""
    try:
        rotation = instance.rotation(rotation_id)
    except KeyError as exc:
        raise ValueError(f"unknown rotation {rotation_id!r}") from exc
    if rotation.kind is not RotationKind.ELECTIVE:
        raise ValueError("only standalone Elective rotations can be removed here")
    if not instance.is_elective_option(rotation_id):
        raise ValueError(f"{rotation.name} is not a configured elective option")

    raw = instance.model_dump(mode="json")
    raw["rotations"] = [
        configured for configured in raw["rotations"] if configured["id"] != rotation_id
    ]
    raw["electives"]["rotation_options"] = [
        option
        for option in raw["electives"]["rotation_options"]
        if option["rotation_id"] != rotation_id
    ]
    for configured in raw["rotations"]:
        for rule in configured["pgy_rules"]:
            rule["prerequisite_rotation_ids"] = [
                prerequisite
                for prerequisite in rule.get("prerequisite_rotation_ids", [])
                if prerequisite != rotation_id
            ]
    raw["locks"] = [lock for lock in raw["locks"] if lock["rotation_id"] != rotation_id]
    return SchedulerInput.from_payload(raw)


def _normalize_elective_block_sizes(
    instance: SchedulerInput,
    rotation: Rotation,
    values: Iterable[int] | None,
) -> list[int]:
    """Normalize an option's sizes, defaulting to compatible curriculum shapes."""
    if values is None:
        sizes = [
            duration
            for duration in instance.elective_block_sizes
            if any(
                duration in instance.elective_block_durations_for_pgy(curriculum.pgy)
                and rotation.allows_duration(duration, pgy=curriculum.pgy)
                for curriculum in instance.requirements
            )
        ]
    else:
        sizes = sorted({int(value) for value in values})
    if not sizes:
        raise ValueError("select at least one eligible Elective block size")
    return sizes


def _normalize_elective_pgys(
    instance: SchedulerInput,
    rotation: Rotation,
    values: Iterable[int] | None,
) -> list[int]:
    """Normalize eligible levels, defaulting to compatible Elective curricula."""
    if values is None:
        pgys = [
            curriculum.pgy
            for curriculum in instance.requirements
            if any(
                rotation.allows_duration(duration, pgy=curriculum.pgy)
                for duration in instance.elective_block_durations_for_pgy(curriculum.pgy)
            )
        ]
    else:
        pgys = sorted({int(value) for value in values})
    if not pgys:
        raise ValueError("select at least one training level for Elective availability")
    return pgys


def replace_clinic_rotation(
    instance: SchedulerInput,
    original_id: str,
    replacement: Rotation,
) -> SchedulerInput:
    """Replace the dedicated Clinic rotation without exposing generic scaffolding."""
    try:
        original = instance.rotation(original_id)
    except KeyError as exc:
        raise ValueError(f"unknown rotation {original_id!r}") from exc
    if original.kind is not RotationKind.CLINIC:
        raise ValueError(f"{original.name} is not a Clinic rotation")
    if replacement.id != original_id:
        raise ValueError("rotation ID is a system key and cannot be changed")
    if replacement.kind is not RotationKind.CLINIC:
        raise ValueError("Clinic block rules must remain Clinic rules")
    rotations = [
        replacement if rotation.id == original_id else rotation for rotation in instance.rotations
    ]
    return instance.revised(rotations=rotations)


def replace_clinic_block_rules(
    instance: SchedulerInput,
    original_id: str,
    replacement: Rotation,
    counts: dict[tuple[int, int], int],
) -> SchedulerInput:
    """Replace Clinic rules and keep every training level at 52 weeks using Electives."""
    return _replace_required_rotation_rules(
        instance,
        original_id,
        replacement,
        counts,
        expected_kind=RotationKind.CLINIC,
        requirement_label="Clinic",
    )


def replace_fmed_pgy_rules(
    instance: SchedulerInput,
    original_id: str,
    replacement: Rotation,
    counts: dict[tuple[int, int], int],
    *,
    eligible_as_elective: bool | None = None,
    eligible_elective_pgys: Iterable[int] | None = None,
    eligible_elective_block_sizes: Iterable[int] | None = None,
    elective_repeatable: bool | None = None,
) -> SchedulerInput:
    """Replace editable FMED staffing, block, and clinic-concurrency rules."""
    try:
        original = instance.rotation(original_id)
    except KeyError as exc:
        raise ValueError(f"unknown rotation {original_id!r}") from exc
    if original.kind is not RotationKind.FMED:
        raise ValueError(f"{original.name} is not an FMED rotation")
    if replacement.id != original_id or replacement.kind is not RotationKind.FMED:
        raise ValueError("invalid FMED training-level rule replacement")

    clinic = original.clinic.model_dump(mode="json") if original.clinic is not None else None
    if clinic is not None and replacement.clinic is not None:
        clinic["max_concurrent"] = replacement.clinic.max_concurrent
        clinic["max_concurrent_by_pgy"] = replacement.clinic.max_concurrent_by_pgy

    # Keep identity, clinic timing/site behavior, operational flags, and color
    # untouched; the dedicated editor owns only these rules and concurrency caps.
    constrained = Rotation.model_validate(
        {
            **original.model_dump(mode="json"),
            "capacity": replacement.capacity.model_dump(mode="json"),
            "pgy_rules": [rule.model_dump(mode="json") for rule in replacement.pgy_rules],
            "clinic": clinic,
        }
    )
    elective_configuration = None
    if eligible_as_elective is not None:
        options = [
            option
            for option in instance.electives.rotation_options
            if option.rotation_id != original_id
        ]
        if eligible_as_elective:
            original_option = instance.electives.option_for(original_id)
            options.append(
                ElectiveRotationOption(
                    rotation_id=original_id,
                    eligible_pgys=_normalize_elective_pgys(
                        instance,
                        constrained,
                        (
                            eligible_elective_pgys
                            if eligible_elective_pgys is not None
                            else (
                                original_option.eligible_pgys
                                if original_option is not None
                                else None
                            )
                        ),
                    ),
                    eligible_block_sizes=_normalize_elective_block_sizes(
                        instance,
                        constrained,
                        eligible_elective_block_sizes,
                    ),
                    repeatable=(
                        elective_repeatable
                        if elective_repeatable is not None
                        else bool(original_option and original_option.repeatable)
                    ),
                )
            )
        elective_configuration = ElectiveConfiguration(
            color=instance.electives.color,
            rotation_options=options,
        )
    return _replace_required_rotation_rules(
        instance,
        original_id,
        constrained,
        counts,
        expected_kind=RotationKind.FMED,
        requirement_label="FMED",
        elective_configuration=elective_configuration,
    )


def _replace_required_rotation_rules(
    instance: SchedulerInput,
    original_id: str,
    replacement: Rotation,
    counts: dict[tuple[int, int], int],
    *,
    expected_kind: RotationKind,
    requirement_label: str,
    elective_configuration: ElectiveConfiguration | None = None,
) -> SchedulerInput:
    """Replace required-block rules and balance curriculum changes with Electives."""
    try:
        original = instance.rotation(original_id)
    except KeyError as exc:
        raise ValueError(f"unknown rotation {original_id!r}") from exc
    if original.kind is not expected_kind:
        raise ValueError(f"{original.name} is not a {requirement_label} rotation")
    if replacement.id != original_id or replacement.kind is not expected_kind:
        raise ValueError(f"invalid {requirement_label} block rule replacement")
    raw = instance.model_dump(mode="json")
    if elective_configuration is not None:
        raw["electives"] = elective_configuration.model_dump(mode="json")
    raw["rotations"] = [
        replacement.model_dump(mode="json") if rotation["id"] == original_id else rotation
        for rotation in raw["rotations"]
    ]
    configured_pgys = {rule.pgy: rule for rule in replacement.pgy_rules}

    for curriculum in raw["requirements"]:
        pgy = int(curriculum["pgy"])
        original_blocks = instance.curriculum_for(pgy).blocks
        original_required_weeks = sum(
            block.duration_weeks * block.count
            for block in original_blocks
            if block.rotation_id == original_id
        )
        rule = configured_pgys.get(pgy)
        configured_durations = (
            {config.duration_weeks for config in rule.block_configs} if rule is not None else set()
        )
        desired_counts = {
            duration: max(0, int(counts.get((pgy, duration), 0)))
            for duration in configured_durations
        }
        desired_required_weeks = sum(duration * count for duration, count in desired_counts.items())
        delta_weeks = desired_required_weeks - original_required_weeks

        blocks = [block for block in curriculum["blocks"] if block["rotation_id"] != original_id]
        blocks = _balance_curriculum_against_electives(
            instance,
            pgy,
            blocks,
            delta_weeks=delta_weeks,
            requirement_label=requirement_label,
        )
        blocks.extend(
            {
                "rotation_id": original_id,
                "duration_weeks": duration,
                "count": count,
            }
            for duration, count in sorted(desired_counts.items())
            if count
        )
        curriculum["blocks"] = blocks

    return SchedulerInput.from_payload(raw)


def _balance_curriculum_against_electives(
    instance: SchedulerInput,
    pgy: int,
    blocks: list[Draft],
    *,
    delta_weeks: int,
    requirement_label: str = "Clinic",
) -> list[Draft]:
    """Offset a required-week change using direct Elective requirements only."""
    if not delta_weeks:
        return blocks

    keyed_blocks = {
        (str(block["rotation_id"]), int(block["duration_weeks"])): dict(block) for block in blocks
    }
    direct_electives = [
        (key, int(block["count"]))
        for key, block in keyed_blocks.items()
        if instance.rotation(key[0]).kind is RotationKind.ELECTIVE
    ]

    if delta_weeks > 0:
        adjustment = _exact_bounded_block_adjustment(
            direct_electives,
            delta_weeks,
        )
        if adjustment is None:
            available = sum(duration * count for (_rotation, duration), count in direct_electives)
            raise ValueError(
                f"{instance.training_level_label(pgy, compact=True)}: "
                f"{requirement_label} needs {delta_weeks} additional weeks, but "
                f"only {available} compatible direct Elective weeks can be replaced"
            )
        for key, count in adjustment.items():
            keyed_blocks[key]["count"] = int(keyed_blocks[key]["count"]) - count
    else:
        elective_shapes = [key for key, _count in direct_electives]
        for rotation in instance.rotations:
            if rotation.kind is not RotationKind.ELECTIVE:
                continue
            try:
                rule = rotation.pgy_rule(pgy)
            except KeyError:
                continue
            for config in rule.block_configs:
                key = (rotation.id, config.duration_weeks)
                if key not in elective_shapes:
                    elective_shapes.append(key)
        adjustment = _exact_unbounded_block_adjustment(
            elective_shapes,
            -delta_weeks,
        )
        if adjustment is None:
            raise ValueError(
                f"{instance.training_level_label(pgy, compact=True)}: no configured "
                "Elective block combination can absorb "
                f"{-delta_weeks} restored weeks"
            )
        for key, count in adjustment.items():
            if key in keyed_blocks:
                keyed_blocks[key]["count"] = int(keyed_blocks[key]["count"]) + count
            else:
                keyed_blocks[key] = {
                    "rotation_id": key[0],
                    "duration_weeks": key[1],
                    "count": count,
                }

    return [block for block in keyed_blocks.values() if int(block["count"]) > 0]


def _exact_bounded_block_adjustment(
    blocks: list[tuple[tuple[str, int], int]],
    target_weeks: int,
) -> dict[tuple[str, int], int] | None:
    combinations: dict[int, dict[tuple[str, int], int]] = {0: {}}
    for key, available in blocks:
        duration = key[1]
        for _ in range(available):
            for weeks, selection in sorted(combinations.items(), reverse=True):
                updated_weeks = weeks + duration
                if updated_weeks > target_weeks or updated_weeks in combinations:
                    continue
                combinations[updated_weeks] = {
                    **selection,
                    key: selection.get(key, 0) + 1,
                }
    return combinations.get(target_weeks)


def _exact_unbounded_block_adjustment(
    shapes: list[tuple[str, int]],
    target_weeks: int,
) -> dict[tuple[str, int], int] | None:
    combinations: dict[int, dict[tuple[str, int], int]] = {0: {}}
    for weeks in range(target_weeks + 1):
        selection = combinations.get(weeks)
        if selection is None:
            continue
        for key in shapes:
            updated_weeks = weeks + key[1]
            if updated_weeks > target_weeks or updated_weeks in combinations:
                continue
            combinations[updated_weeks] = {
                **selection,
                key: selection.get(key, 0) + 1,
            }
    return combinations.get(target_weeks)


def add_manual_clinic_block(
    instance: SchedulerInput,
    block: ManualClinicBlock | Draft,
) -> SchedulerInput:
    """Add a fixed resident Clinic block and validate its replacement."""
    added = (
        block if isinstance(block, ManualClinicBlock) else ManualClinicBlock.model_validate(block)
    )
    return instance.revised(manual_clinic_blocks=[*instance.manual_clinic_blocks, added])


def remove_manual_clinic_block(
    instance: SchedulerInput,
    index: int,
) -> SchedulerInput:
    """Remove a fixed resident Clinic block by its displayed position."""
    if not 0 <= index < len(instance.manual_clinic_blocks):
        raise ValueError("manual Clinic block not found")
    blocks = list(instance.manual_clinic_blocks)
    blocks.pop(index)
    return instance.revised(manual_clinic_blocks=blocks)


def _rotation_summary_category(kind: RotationKind) -> str:
    if kind is RotationKind.ELECTIVE:
        return "elective"
    if kind is RotationKind.CLINIC:
        return "clinic"
    return "mandatory"


def _resident_planned_blocks(
    instance: SchedulerInput,
    schedule: Schedule | None,
    resident_id: str,
) -> list[ScheduleBlock]:
    """Overlay pending exact manual blocks on the resident's latest schedule."""
    blocks = schedule_blocks(schedule, resident_id=resident_id)
    for lock in instance.locks:
        if lock.source != "manual" or not lock.exact_block or lock.resident_id != resident_id:
            continue
        hardcoded = ScheduleBlock(
            resident_id=resident_id,
            rotation_id=lock.rotation_id,
            start_week=lock.weeks[0],
            duration_weeks=len(lock.weeks),
            elective=lock.elective,
        )
        hardcoded_weeks = set(hardcoded.weeks)
        blocks = [block for block in blocks if not (set(block.weeks) & hardcoded_weeks)]
        blocks.append(hardcoded)
    return sorted(blocks, key=lambda block: (block.start_week, block.rotation_id))


def resident_missing_mandatory_rotations(
    instance: SchedulerInput,
    schedule: Schedule | None,
    resident_id: str,
) -> tuple[int, list[str]]:
    """Return missing mandatory block count and concise block labels.

    Direct curriculum requirements and resident-specific Mandatory overrides
    are exact requirements.
    """
    resident = next(item for item in instance.residents if item.id == resident_id)
    curriculum = instance.curriculum_for(resident.pgy)
    actual: Counter[tuple[str, int]] = Counter(
        (block.rotation_id, block.duration_weeks)
        for block in _resident_planned_blocks(instance, schedule, resident_id)
        if not block.elective
    )
    required: Counter[tuple[str, int]] = Counter()
    for block in curriculum.blocks:
        if _rotation_summary_category(instance.rotation(block.rotation_id).kind) == "mandatory":
            required[block.rotation_id, block.duration_weeks] += block.count
    for override in instance.resident_rotation_overrides:
        if override.resident_id == resident_id:
            required[override.rotation_id, override.duration_weeks] += 1

    missing_count = 0
    labels: list[str] = []
    for (rotation_id, duration), required_count in sorted(
        required.items(),
        key=lambda item: instance.rotation(item[0][0]).code.casefold(),
    ):
        present = min(actual[rotation_id, duration], required_count)
        actual[rotation_id, duration] -= present
        missing = required_count - present
        if missing <= 0:
            continue
        missing_count += missing
        rotation = instance.rotation(rotation_id)
        block_label = f"{rotation.code} ({duration} wk)"
        labels.append(f"{missing}× {block_label}" if missing > 1 else block_label)

    return missing_count, labels


def resident_rotation_week_totals(
    instance: SchedulerInput,
    resident_id: str,
) -> dict[str, int]:
    """Return configured weeks in each Rotation Summary category."""
    resident = next(item for item in instance.residents if item.id == resident_id)
    curriculum = instance.curriculum_for(resident.pgy)
    totals = {
        "mandatory": 0,
        "elective": 0,
        "clinic": 0,
    }
    for block in curriculum.blocks:
        category = _rotation_summary_category(instance.rotation(block.rotation_id).kind)
        weeks = block.duration_weeks * block.count
        totals[category] += weeks
    for block in instance.manual_clinic_blocks:
        if block.resident_id != resident_id:
            continue
        totals["clinic"] += block.duration_weeks
        totals["elective"] -= block.duration_weeks
    for override in instance.resident_rotation_overrides:
        if override.resident_id != resident_id:
            continue
        totals["mandatory"] += override.duration_weeks
        totals["elective"] -= override.duration_weeks

    return totals
