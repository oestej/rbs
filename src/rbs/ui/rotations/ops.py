"""Rotation, curriculum, and manual-block edits on a scheduling instance.

Pure ``SchedulerInput -> SchedulerInput`` operations. Kept free of NiceGUI so
the scheduling rules they encode can be read and tested without a UI.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

from rbs.models.curriculum import BlockRequirement, RotationGroup
from rbs.models.elective import ElectiveConfiguration, ElectiveRotationOption
from rbs.models.enums import RotationKind
from rbs.models.instance import (
    ManualClinicBlock,
    ResidentRotationOverride,
    ResidentRotationWaiver,
    SchedulerInput,
)
from rbs.models.rotation import ROTATION_CODE_MAX_LENGTH, Rotation
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
    "elective_shapes_for_rotation",
    "elective_pgys_sizes_for_shapes",
    "elective_slot_rotation",
    "direct_elective_counts",
    "set_elective_allocation",
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
    elective_shapes: Iterable[tuple[int, int]] | None = None,
    elective_repeatable: bool = False,
    group_members_by_pgy: dict[int, list[str]] | None = None,
) -> SchedulerInput:
    """Add one standard rotation, spending the level's unallocated weeks.

    When ``elective_shapes`` is given it takes precedence over the explicit
    elective PGYs and sizes: PGYs and sizes derive from the marked shapes
    after funding, so the new requirements cannot leave stale sizes behind.
    """
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

    for pgy in sorted({required_pgy for required_pgy, _duration in requirements}):
        instance = _fund_requirement(
            instance,
            pgy,
            sum(
                duration * count
                for (required_pgy, duration), count in requirements.items()
                if required_pgy == pgy
            ),
            requirement_label=rotation.name,
        )

    raw = instance.model_dump(mode="json")
    raw["rotations"].append(rotation.model_dump(mode="json"))
    if elective_shapes is not None:
        pgys, sizes = elective_pgys_sizes_for_shapes(
            instance,
            rotation,
            elective_shapes,
        )
        should_offer = bool(pgys)
    elif eligible_as_elective:
        pgys = _normalize_elective_pgys(
            instance,
            rotation,
            eligible_elective_pgys,
        )
        sizes = _normalize_elective_block_sizes(
            instance,
            rotation,
            eligible_elective_block_sizes,
            eligible_pgys=pgys,
        )
        should_offer = True
    else:
        should_offer = False
    if should_offer:
        raw["electives"]["rotation_options"].append(
            ElectiveRotationOption(
                rotation_id=rotation.id,
                eligible_pgys=pgys,
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
        curriculum["blocks"] = list(curriculum["blocks"])
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
        # Dropping the blocks returns their weeks to the level's unallocated
        # pool; nothing backfills them, so the curriculum reports the gap until
        # someone spends it.
        curriculum["blocks"] = [
            block for block in curriculum["blocks"] if block["rotation_id"] != rotation_id
        ]

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
        and override.get("replaces_rotation_id") != rotation_id
        and (
            override.get("group_instance_id") is None
            or (
                str(override["resident_id"]),
                str(override["group_instance_id"]),
            )
            not in removed_override_groups
        )
    ]
    raw["resident_rotation_waivers"] = [
        waiver
        for waiver in raw.get("resident_rotation_waivers", [])
        if waiver["rotation_id"] != rotation_id
    ]
    return SchedulerInput.from_payload(raw)


def replace_standard_rotation(
    instance: SchedulerInput,
    original_id: str,
    replacement: Rotation,
    *,
    resident_overrides: list[ResidentRotationOverride | Draft] | None = None,
    resident_waivers: list[ResidentRotationWaiver | Draft] | None = None,
    eligible_as_elective: bool | None = None,
    eligible_elective_pgys: Iterable[int] | None = None,
    eligible_elective_block_sizes: Iterable[int] | None = None,
    elective_shapes: Iterable[tuple[int, int]] | None = None,
    elective_repeatable: bool | None = None,
    group_members_by_pgy: dict[int, list[str]] | None = None,
    counts: dict[tuple[int, int], int] | None = None,
) -> SchedulerInput:
    """Replace a standard rotation and validate all catalog references together.

    When ``counts`` is given, mandatory curriculum blocks for this rotation are
    rewritten per training level: a zero count makes that block shape
    elective-only, a positive count makes it a mandatory requirement. Growing
    requirements spend unallocated weeks first, then direct Elective time.

    When ``elective_shapes`` is given it takes precedence over the explicit
    elective PGYs and sizes: PGYs and sizes derive from the marked shapes
    after funding, so growing requirements cannot leave stale sizes behind.
    """
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

    if counts is not None:
        configured = {rule.pgy: rule for rule in replacement.pgy_rules}

        def desired_counts_for(pgy: int) -> dict[int, int]:
            rule = configured.get(pgy)
            allowed = (
                {config.duration_weeks for config in rule.block_configs}
                if rule is not None
                else set()
            )
            return {duration: max(0, int(counts.get((pgy, duration), 0))) for duration in allowed}

        for curriculum in instance.requirements:
            original_weeks = sum(
                block.duration_weeks * block.count
                for block in curriculum.blocks
                if block.rotation_id == original_id
            )
            desired = desired_counts_for(curriculum.pgy)
            desired_weeks = sum(duration * count for duration, count in desired.items())
            rule = configured.get(curriculum.pgy)
            cap = rule.max_total_weeks if rule is not None else None
            if cap is not None and desired_weeks > cap:
                level_code = instance.training_level_label(curriculum.pgy, compact=True)
                raise ValueError(
                    f"{level_code}: {replacement.name} requires {desired_weeks} "
                    f"mandatory weeks, exceeding its {cap}-week maximum"
                )
            delta = desired_weeks - original_weeks
            instance = _fund_requirement(
                instance,
                curriculum.pgy,
                delta,
                requirement_label=replacement.name,
            )

        requirements = []
        for curriculum in instance.requirements:
            desired = desired_counts_for(curriculum.pgy)
            blocks = [block for block in curriculum.blocks if block.rotation_id != original_id]
            blocks.extend(
                BlockRequirement(
                    rotation_id=original_id,
                    duration_weeks=duration,
                    count=count,
                )
                for duration, count in sorted(desired.items())
                if count
            )
            requirements.append(curriculum.model_copy(update={"blocks": blocks}))
        instance = instance.model_copy(update={"requirements": requirements})

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
    if elective_shapes is not None or eligible_as_elective is not None:
        options = [
            option
            for option in instance.electives.rotation_options
            if option.rotation_id != original_id
        ]
        original_option = instance.electives.option_for(original_id)
        if elective_shapes is not None:
            pgys, sizes = elective_pgys_sizes_for_shapes(
                instance,
                replacement,
                elective_shapes,
            )
            should_offer = bool(pgys)
        elif eligible_as_elective:
            pgys = _normalize_elective_pgys(
                instance,
                replacement,
                (
                    eligible_elective_pgys
                    if eligible_elective_pgys is not None
                    else (original_option.eligible_pgys if original_option is not None else None)
                ),
            )
            sizes = _normalize_elective_block_sizes(
                instance,
                replacement,
                eligible_elective_block_sizes,
                eligible_pgys=pgys,
            )
            should_offer = True
        else:
            should_offer = False
        if should_offer:
            options.append(
                ElectiveRotationOption(
                    rotation_id=original_id,
                    eligible_pgys=pgys,
                    eligible_block_sizes=sizes,
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
    if resident_waivers is not None:
        normalized_waivers = [
            waiver
            if isinstance(waiver, ResidentRotationWaiver)
            else ResidentRotationWaiver.model_validate(waiver)
            for waiver in resident_waivers
        ]
        for waiver in normalized_waivers:
            if waiver.rotation_id != original_id:
                raise ValueError("resident waiver belongs to a different rotation")
        updates["resident_rotation_waivers"] = [
            waiver
            for waiver in instance.resident_rotation_waivers
            if waiver.rotation_id != original_id
        ] + normalized_waivers
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


_ELECTIVE_SLOT_ID = "elective"
_ELECTIVE_SLOT_CODE = "ELEC"
_ELECTIVE_SLOT_NAME = "Elective"


def elective_slot_rotation(instance: SchedulerInput) -> Rotation | None:
    """The Elective curriculum placeholder: Elective-kind but not itself an option.

    Direct Elective curriculum blocks point here. The slot marks weeks the
    program has committed to Elective time without naming the service; the
    solver fills each one from resident preferences at compile time, falling
    back to Clinic. Standalone Elective services are options instead, so they
    are excluded.
    """
    return next(
        (
            rotation
            for rotation in instance.rotations
            if rotation.kind is RotationKind.ELECTIVE
            and not instance.is_elective_option(rotation.id)
        ),
        None,
    )


def direct_elective_counts(instance: SchedulerInput, pgy: int) -> dict[int, int]:
    """Return this level's direct Elective slot counts keyed by block duration."""
    return instance.direct_elective_block_counts_for_pgy(pgy)


def set_elective_allocation(
    instance: SchedulerInput,
    pgy: int,
    counts_by_duration: dict[int, int],
) -> SchedulerInput:
    """Spend or release one training level's unallocated weeks as Elective slots.

    This is the only surface that authors direct Elective curriculum blocks.
    Block sizes are chosen here, where they are a real decision, rather than
    being forced on a program before it has configured anything.
    """
    known_pgys = {curriculum.pgy for curriculum in instance.requirements}
    if pgy not in known_pgys:
        raise ValueError(f"training level {pgy} has no configured curriculum")
    desired: dict[int, int] = {}
    for raw_duration, raw_count in counts_by_duration.items():
        duration, count = int(raw_duration), int(raw_count)
        if count < 0:
            raise ValueError("Elective slot counts cannot be negative")
        if not 1 <= duration <= 5:
            raise ValueError(f"Elective block length {duration} must be 1-5 weeks")
        if count:
            desired[duration] = count

    level_code = instance.training_level_label(pgy, compact=True)
    current_weeks = sum(
        duration * count for duration, count in direct_elective_counts(instance, pgy).items()
    )
    desired_weeks = sum(duration * count for duration, count in desired.items())
    delta_weeks = desired_weeks - current_weeks
    if delta_weeks > 0:
        _require_unallocated_weeks(instance, pgy, delta_weeks, requirement_label="Elective")

    raw = instance.model_dump(mode="json")
    slot = elective_slot_rotation(instance)
    if desired:
        slot_id = _ensure_elective_slot_rules(raw, instance, slot, pgy, sorted(desired))
    elif slot is None:
        slot_id = None
    else:
        slot_id = slot.id

    slot_ids = {
        rotation.id
        for rotation in instance.rotations
        if rotation.kind is RotationKind.ELECTIVE and not instance.is_elective_option(rotation.id)
    }
    for curriculum in raw["requirements"]:
        if int(curriculum["pgy"]) != pgy:
            continue
        blocks = [block for block in curriculum["blocks"] if block["rotation_id"] not in slot_ids]
        if slot_id is not None:
            blocks.extend(
                {
                    "rotation_id": slot_id,
                    "duration_weeks": duration,
                    "count": desired[duration],
                }
                for duration in sorted(desired)
            )
        curriculum["blocks"] = blocks
        break
    else:  # pragma: no cover - guarded by the membership check above
        raise ValueError(f"{level_code} has no configured curriculum")

    return SchedulerInput.from_payload(raw)


def _ensure_elective_slot_rules(
    raw: dict[str, Any],
    instance: SchedulerInput,
    slot: Rotation | None,
    pgy: int,
    durations: list[int],
) -> str:
    """Create or widen the Elective slot rotation so it allows these block shapes."""
    if slot is None:
        used = set(instance.rotations_by_id) | set(instance.special_rotations_by_id)
        slot_id = _ELECTIVE_SLOT_ID
        suffix = 2
        while slot_id in used:
            slot_id = f"{_ELECTIVE_SLOT_ID}_{suffix}"
            suffix += 1
        codes = {existing.code.casefold() for existing in instance.rotations}
        code = _ELECTIVE_SLOT_CODE
        suffix = 2
        while code.casefold() in codes:
            code = f"{_ELECTIVE_SLOT_CODE}{suffix}"[:ROTATION_CODE_MAX_LENGTH]
            suffix += 1
        raw["rotations"].append(
            {
                "id": slot_id,
                "code": code,
                "name": _ELECTIVE_SLOT_NAME,
                "color": instance.electives.color,
                "kind": RotationKind.ELECTIVE.value,
                "pgy_rules": [
                    {
                        "pgy": pgy,
                        "block_configs": [{"duration_weeks": d} for d in durations],
                    }
                ],
                "max_consecutive_weeks": max(durations),
            }
        )
        return slot_id

    record = next(item for item in raw["rotations"] if item["id"] == slot.id)
    rule = next((item for item in record["pgy_rules"] if int(item["pgy"]) == pgy), None)
    if rule is None:
        rule = {"pgy": pgy, "block_configs": []}
        record["pgy_rules"].append(rule)
    configured = {int(config["duration_weeks"]) for config in rule["block_configs"]}
    rule["block_configs"].extend(
        {"duration_weeks": duration} for duration in durations if duration not in configured
    )
    record["max_consecutive_weeks"] = max(
        int(record.get("max_consecutive_weeks") or 1),
        max(durations),
    )
    return slot.id


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
        pgys = _normalize_elective_pgys(
            instance,
            rotation,
            (
                eligible_pgys
                if eligible_pgys is not None
                else (original_option.eligible_pgys if original_option is not None else None)
            ),
        )
        # Mandatory/FMED sizes follow Training-level rules; an explicit size
        # list is still honored for backward compatibility (raw imports, tests).
        # UI callers pass None so sizes are derived from the resolved levels.
        sizes_arg: Iterable[int] | None = eligible_block_sizes
        if sizes_arg is None and rotation.kind in {RotationKind.STANDARD, RotationKind.FMED}:
            original_sizes = (
                instance.electives.block_sizes_for(rotation_id) or None
                if original_option is not None and eligible_pgys is None
                else None
            )
            sizes_arg = original_sizes
        options.append(
            ElectiveRotationOption(
                rotation_id=rotation_id,
                eligible_pgys=pgys,
                eligible_block_sizes=_normalize_elective_block_sizes(
                    instance,
                    rotation,
                    sizes_arg,
                    eligible_pgys=pgys,
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
    """Add a standalone Elective service and make it eligible immediately.

    An option can only be filled by curriculum slots of a matching size, so any
    size this service needs but the curriculum lacks is allocated here from
    unscheduled weeks. That keeps a service and the time it can occupy in one
    step instead of requiring the program to guess block sizes up front.
    """
    if rotation.kind is not RotationKind.ELECTIVE:
        raise ValueError("new Elective rotations must use the elective rotation kind")
    if rotation.id in instance.rotations_by_id:
        raise ValueError(f"rotation ID {rotation.id!r} is already configured")
    if any(existing.code.casefold() == rotation.code.casefold() for existing in instance.rotations):
        raise ValueError(f"rotation code {rotation.code!r} is already configured")
    normalized = Rotation.model_validate(
        {**rotation.model_dump(mode="json"), "color": instance.electives.color}
    )
    pgys = _normalize_elective_pgys(instance, normalized, eligible_pgys, seedable=True)
    sizes = _normalize_elective_block_sizes(
        instance,
        normalized,
        eligible_block_sizes,
        seedable=True,
    )
    instance = _seed_elective_slots(instance, normalized, pgys, sizes)
    configuration = ElectiveConfiguration(
        color=instance.electives.color,
        rotation_options=[
            *instance.electives.rotation_options,
            ElectiveRotationOption(
                rotation_id=normalized.id,
                eligible_pgys=pgys,
                eligible_block_sizes=sizes,
                repeatable=repeatable,
            ),
        ],
    )
    return instance.revised(
        rotations=[*instance.rotations, normalized],
        electives=configuration,
    )


def _seed_elective_slots(
    instance: SchedulerInput,
    rotation: Rotation,
    pgys: list[int],
    sizes: list[int],
) -> SchedulerInput:
    """Allocate one Elective slot for every size this option can fill but lacks."""
    for pgy in pgys:
        existing = direct_elective_counts(instance, pgy)
        missing = [
            size
            for size in sizes
            if size not in existing and rotation.allows_duration(size, pgy=pgy)
        ]
        if not missing:
            continue
        desired = dict(existing)
        for size in missing:
            desired[size] = 1
        instance = set_elective_allocation(instance, pgy, desired)
    return instance


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


def elective_shapes_for_rotation(
    instance: SchedulerInput,
    rotation_id: str,
) -> set[tuple[int, int]]:
    """Return the stored elective (pgy, duration) shapes for one service.

    Faithful to the saved option: a shape is present exactly when the option
    admits it, before fillability filtering at save time.
    """
    option = instance.electives.option_for(rotation_id)
    if option is None:
        return set()
    return {
        (pgy, duration) for pgy in option.eligible_pgys for duration in option.eligible_block_sizes
    }


def elective_pgys_sizes_for_shapes(
    instance: SchedulerInput,
    rotation: Rotation,
    elective_shapes: Iterable[tuple[int, int]],
) -> tuple[list[int], list[int]]:
    """Derive explicit option PGYs and sizes from per-shape elective flags.

    Only fillable shapes survive into sizes: the duration must be Elective
    curriculum time for that level and allowed by the rotation's own rules.
    """
    shapes = {(int(pgy), int(duration)) for pgy, duration in elective_shapes}
    pgys = sorted({pgy for pgy, _ in shapes})
    sizes = sorted(
        {
            duration
            for pgy, duration in shapes
            if duration in instance.elective_block_durations_for_pgy(pgy)
            and rotation.allows_duration(duration, pgy=pgy)
        }
    )
    if pgys and not sizes:
        raise ValueError(
            "elective block shapes do not match any Elective curriculum time; "
            "check the marked shapes' training levels and block lengths"
        )
    return pgys, sizes


def _derived_elective_block_sizes(
    instance: SchedulerInput,
    rotation: Rotation,
    eligible_pgys: Iterable[int],
) -> list[int]:
    """Derive Elective sizes from Training-level rules and the Elective curriculum.

    Sizes follow the rotation's own ``block_configs`` (the second screen): a
    duration is eligible only when the Elective curriculum offers it for an
    eligible training level and the rotation allows it there.
    """
    return sorted(
        {
            duration
            for pgy in {int(value) for value in eligible_pgys}
            for duration in instance.elective_block_durations_for_pgy(pgy)
            if rotation.allows_duration(duration, pgy=pgy)
        }
    )


def _normalize_elective_block_sizes(
    instance: SchedulerInput,
    rotation: Rotation,
    values: Iterable[int] | None,
    *,
    seedable: bool = False,
    eligible_pgys: Iterable[int] | None = None,
) -> list[int]:
    """Normalize an option's sizes, defaulting to compatible curriculum shapes.

    ``seedable`` callers allocate the Elective slots they need, so they fall back
    to the shapes the service itself configures when the curriculum has none.
    When ``eligible_pgys`` is given and ``values`` is omitted, sizes are derived
    from those levels' Training-level rules instead of the global curriculum.
    """
    if values is None:
        if eligible_pgys is not None:
            sizes = _derived_elective_block_sizes(instance, rotation, eligible_pgys)
        else:
            sizes = [
                duration
                for duration in instance.elective_block_sizes
                if any(
                    duration in instance.elective_block_durations_for_pgy(curriculum.pgy)
                    and rotation.allows_duration(duration, pgy=curriculum.pgy)
                    for curriculum in instance.requirements
                )
            ]
        if not sizes and seedable:
            sizes = sorted(
                {
                    config.duration_weeks
                    for rule in rotation.pgy_rules
                    for config in rule.block_configs
                }
            )
    else:
        sizes = sorted({int(value) for value in values})
    if not sizes:
        raise ValueError("select at least one eligible Elective block size")
    return sizes


def _normalize_elective_pgys(
    instance: SchedulerInput,
    rotation: Rotation,
    values: Iterable[int] | None,
    *,
    seedable: bool = False,
) -> list[int]:
    """Normalize eligible levels, defaulting to compatible Elective curricula.

    ``seedable`` callers allocate the Elective slots they need, so they fall back
    to every level the service configures when no curriculum offers one yet.
    """
    if values is None:
        pgys = [
            curriculum.pgy
            for curriculum in instance.requirements
            if any(
                rotation.allows_duration(duration, pgy=curriculum.pgy)
                for duration in instance.elective_block_durations_for_pgy(curriculum.pgy)
            )
        ]
        if not pgys and seedable:
            configured = {rule.pgy for rule in rotation.pgy_rules}
            pgys = [
                curriculum.pgy
                for curriculum in instance.requirements
                if curriculum.pgy in configured
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
    elective_shapes: Iterable[tuple[int, int]] | None = None,
    elective_repeatable: bool | None = None,
) -> SchedulerInput:
    """Replace editable FMED staffing, block, and clinic rules.

    The dedicated editor owns concurrency caps and the eligible clinic days;
    every other clinic setting (workload, admin time, academic-day handling)
    stays as configured.

    When ``elective_shapes`` is given it takes precedence over the explicit
    elective PGYs and sizes: PGYs and sizes derive from the marked shapes
    after funding, so growing requirements cannot leave stale sizes behind.
    """
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
        clinic["slots"] = [slot.model_dump(mode="json") for slot in replacement.clinic.slots]

    # Keep identity, remaining clinic behavior, operational flags, and color
    # untouched; the dedicated editor owns only these rules, the concurrency
    # caps, and the eligible clinic days.
    constrained = Rotation.model_validate(
        {
            **original.model_dump(mode="json"),
            "capacity": replacement.capacity.model_dump(mode="json"),
            "pgy_rules": [rule.model_dump(mode="json") for rule in replacement.pgy_rules],
            "clinic": clinic,
        }
    )
    elective_configuration = None
    if elective_shapes is None and eligible_as_elective is not None:
        options = [
            option
            for option in instance.electives.rotation_options
            if option.rotation_id != original_id
        ]
        if eligible_as_elective:
            original_option = instance.electives.option_for(original_id)
            pgys = _normalize_elective_pgys(
                instance,
                constrained,
                (
                    eligible_elective_pgys
                    if eligible_elective_pgys is not None
                    else (original_option.eligible_pgys if original_option is not None else None)
                ),
            )
            options.append(
                ElectiveRotationOption(
                    rotation_id=original_id,
                    eligible_pgys=pgys,
                    eligible_block_sizes=_normalize_elective_block_sizes(
                        instance,
                        constrained,
                        eligible_elective_block_sizes,
                        eligible_pgys=pgys,
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
        elective_shapes=elective_shapes,
        elective_repeatable=elective_repeatable,
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
    elective_shapes: Iterable[tuple[int, int]] | None = None,
    elective_repeatable: bool | None = None,
) -> SchedulerInput:
    """Replace required-block rules, spending or freeing unallocated weeks.

    Pinned placements the edited requirement can no longer honor are
    released, mirroring whole-rotation removal: removing a training-level
    rule (or zeroing its blocks) orphans locks that depended on those weeks,
    so the save releases them instead of failing validation.

    When ``elective_shapes`` is given (instead of a prebuilt configuration),
    PGYs and sizes derive from the marked shapes after funding, so growing
    requirements cannot leave stale sizes behind.
    """
    try:
        original = instance.rotation(original_id)
    except KeyError as exc:
        raise ValueError(f"unknown rotation {original_id!r}") from exc
    if original.kind is not expected_kind:
        raise ValueError(f"{original.name} is not a {requirement_label} rotation")
    if replacement.id != original_id or replacement.kind is not expected_kind:
        raise ValueError(f"invalid {requirement_label} block rule replacement")
    configured_pgys = {rule.pgy: rule for rule in replacement.pgy_rules}

    def desired_counts_for(pgy: int) -> dict[int, int]:
        rule = configured_pgys.get(pgy)
        configured_durations = (
            {config.duration_weeks for config in rule.block_configs} if rule is not None else set()
        )
        return {
            duration: max(0, int(counts.get((pgy, duration), 0)))
            for duration in configured_durations
        }

    # Fund every growing level before the payload is built, so an Elective
    # rebalance is part of the same edit rather than a follow-up.
    for curriculum in instance.requirements:
        original_required_weeks = sum(
            block.duration_weeks * block.count
            for block in curriculum.blocks
            if block.rotation_id == original_id
        )
        desired = desired_counts_for(curriculum.pgy)
        desired_weeks = sum(duration * count for duration, count in desired.items())
        rule = configured_pgys.get(curriculum.pgy)
        cap = rule.max_total_weeks if rule is not None else None
        if cap is not None and desired_weeks > cap:
            level_code = instance.training_level_label(curriculum.pgy, compact=True)
            raise ValueError(
                f"{level_code}: {requirement_label} requires {desired_weeks} "
                f"mandatory weeks, exceeding its {cap}-week maximum"
            )
        delta_weeks = desired_weeks - original_required_weeks
        instance = _fund_requirement(
            instance,
            curriculum.pgy,
            delta_weeks,
            requirement_label=requirement_label,
        )

    raw = instance.model_dump(mode="json")
    if elective_shapes is not None:
        pgys, sizes = elective_pgys_sizes_for_shapes(
            instance,
            replacement,
            elective_shapes,
        )
        options = [
            option
            for option in instance.electives.rotation_options
            if option.rotation_id != original_id
        ]
        if pgys:
            original_option = instance.electives.option_for(original_id)
            options.append(
                ElectiveRotationOption(
                    rotation_id=original_id,
                    eligible_pgys=pgys,
                    eligible_block_sizes=sizes,
                    repeatable=(
                        elective_repeatable
                        if elective_repeatable is not None
                        else bool(original_option and original_option.repeatable)
                    ),
                )
            )
        raw["electives"] = ElectiveConfiguration(
            color=instance.electives.color,
            rotation_options=options,
        ).model_dump(mode="json")
    elif elective_configuration is not None:
        raw["electives"] = elective_configuration.model_dump(mode="json")
    raw["rotations"] = [
        replacement.model_dump(mode="json") if rotation["id"] == original_id else rotation
        for rotation in raw["rotations"]
    ]

    for curriculum in raw["requirements"]:
        pgy = int(curriculum["pgy"])
        desired_counts = desired_counts_for(pgy)
        blocks = [block for block in curriculum["blocks"] if block["rotation_id"] != original_id]
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

    raw["locks"] = _release_orphaned_requirement_locks(
        raw.get("locks", []),
        original,
        replacement,
        original_id,
        raw["requirements"],
        instance.residents_by_id,
    )

    return SchedulerInput.from_payload(raw)


def _release_orphaned_requirement_locks(
    locks: list[Draft],
    original: Rotation,
    replacement: Rotation,
    original_id: str,
    curricula: list[Draft],
    residents_by_id: dict[str, Any],
) -> list[Draft]:
    """Release pinned placements the edited requirement can no longer honor.

    A lock survives unless the edit took away the weeks it needs: the
    resident's training level lost its rule entirely, the remaining
    requirement is shorter than the locked weeks, or an exact-block lock's
    duration is no longer configured. Locks on other rotations and locks the
    new configuration still satisfies are kept untouched.
    """
    removed_pgys = {rule.pgy for rule in original.pgy_rules} - {
        rule.pgy for rule in replacement.pgy_rules
    }
    weeks_by_pgy: dict[int, int] = {}
    durations_by_pgy: dict[int, set[int]] = {}
    for curriculum in curricula:
        pgy = int(curriculum["pgy"])
        blocks = [block for block in curriculum["blocks"] if block["rotation_id"] == original_id]
        weeks_by_pgy[pgy] = sum(
            int(block["duration_weeks"]) * int(block["count"]) for block in blocks
        )
        durations_by_pgy[pgy] = {int(block["duration_weeks"]) for block in blocks}
    surviving = []
    for lock in locks:
        if lock.get("rotation_id") != original_id:
            surviving.append(lock)
            continue
        resident = residents_by_id.get(str(lock.get("resident_id")))
        if resident is None:
            surviving.append(lock)
            continue
        if resident.pgy in removed_pgys:
            continue
        if not lock.get("elective"):
            weeks = list(lock.get("weeks") or [])
            if len(weeks) > weeks_by_pgy.get(resident.pgy, 0):
                continue
            if lock.get("exact_block") and len(weeks) not in durations_by_pgy.get(
                resident.pgy, set()
            ):
                continue
        surviving.append(lock)
    return surviving


def _require_unallocated_weeks(
    instance: SchedulerInput,
    pgy: int,
    weeks: int,
    *,
    requirement_label: str,
) -> None:
    """Reject a requirement the training level has no unallocated weeks for."""
    available = instance.unallocated_weeks(pgy)
    if weeks > available:
        level_code = instance.training_level_label(pgy, compact=True)
        raise ValueError(
            f"{level_code}: {requirement_label} needs {weeks} weeks, but only "
            f"{available} unscheduled weeks remain; free {weeks - available} more "
            f"from {level_code}'s existing requirements first"
        )


def _fund_requirement(
    instance: SchedulerInput,
    pgy: int,
    weeks: int,
    *,
    requirement_label: str,
) -> SchedulerInput:
    """Make room for ``weeks`` of requirement, spending Elective time if needed.

    Unscheduled weeks are spent first. A curriculum that has already allocated
    everything falls back to its direct Elective time, which is the slack a
    program expects a new requirement to come out of. Only when both pools are
    too small does the edit fail.
    """
    if weeks <= 0:
        return instance
    available = instance.unallocated_weeks(pgy)
    if weeks <= available:
        return instance

    shortfall = weeks - available
    elective_counts = direct_elective_counts(instance, pgy)
    elective_weeks = sum(duration * count for duration, count in elective_counts.items())
    if shortfall > elective_weeks:
        level_code = instance.training_level_label(pgy, compact=True)
        raise ValueError(
            f"{level_code}: {requirement_label} needs {weeks} weeks, but only "
            f"{available} unscheduled and {elective_weeks} direct Elective weeks "
            f"are available to spend"
        )
    return set_elective_allocation(
        instance,
        pgy,
        _repartition_elective_weeks(elective_counts, elective_weeks - shortfall),
    )


def _repartition_elective_weeks(counts: dict[int, int], target_weeks: int) -> dict[int, int]:
    """Re-shape Elective slots to cover exactly ``target_weeks``.

    Existing block sizes are reused largest-first so a program's chosen shape
    survives wherever it divides the remaining time. Whatever is left over
    becomes a single shorter block, which is what lets a requirement of any
    length be funded instead of only those that happen to tile the old slots.
    """
    if target_weeks <= 0:
        return {}
    remaining = target_weeks
    repartitioned: dict[int, int] = {}
    for size in sorted(counts, reverse=True):
        if remaining >= size:
            repartitioned[size] = remaining // size
            remaining -= size * repartitioned[size]
    if remaining:
        repartitioned[remaining] = repartitioned.get(remaining, 0) + 1
    return repartitioned


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
    for waiver in instance.resident_rotation_waivers:
        if waiver.resident_id == resident_id:
            key = (waiver.rotation_id, waiver.duration_weeks)
            required[key] = max(0, required.get(key, 0) - 1)

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
        if override.replaces_rotation_id is not None:
            totals["elective"] -= override.duration_weeks
    for waiver in instance.resident_rotation_waivers:
        if waiver.resident_id != resident_id:
            continue
        totals["mandatory"] -= waiver.duration_weeks

    return totals
