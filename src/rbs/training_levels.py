"""Safe edits for the workspace's configured training levels."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rbs.models.instance import SchedulerInput


def add_training_level(
    instance: SchedulerInput,
    *,
    code: str,
    label: str,
) -> SchedulerInput:
    """Add an empty training level without assuming another level's rules apply."""
    normalized_code, normalized_label = _validated_identity(instance, code, label)
    new_key = max(instance.training_level_ids, default=0) + 1
    raw = instance.model_dump(mode="json")
    raw["requirements"].append(
        {
            "pgy": new_key,
            "code": normalized_code,
            "label": normalized_label,
            "blocks": [],
        }
    )

    return SchedulerInput.model_validate(raw)


def reorder_training_levels(
    instance: SchedulerInput,
    training_level_ids: Sequence[int],
) -> SchedulerInput:
    """Persist one display order and apply it to every training-level rule list."""
    ordered_ids = tuple(int(pgy) for pgy in training_level_ids)
    current_ids = instance.training_level_ids
    if len(ordered_ids) != len(current_ids) or set(ordered_ids) != set(current_ids):
        raise ValueError(
            "training-level order must include every configured level exactly once"
        )
    if ordered_ids == current_ids:
        return instance

    raw = instance.model_dump(mode="json")
    order = {pgy: index for index, pgy in enumerate(ordered_ids)}
    curricula = {
        int(curriculum["pgy"]): curriculum
        for curriculum in raw["requirements"]
    }
    raw["requirements"] = [curricula[pgy] for pgy in ordered_ids]

    for rotation in raw["rotations"]:
        rotation["pgy_rules"].sort(
            key=lambda rule: order[int(rule["pgy"])]
        )
    raw["rotation_groups"].sort(key=lambda group: order[int(group["pgy"])])

    _reorder_allocation_rules(
        raw["clinic_policy"].get("allocation_rules", []),
        order,
    )
    return SchedulerInput.model_validate(raw)


def update_training_level(
    instance: SchedulerInput,
    pgy: int,
    code: str,
    label: str,
) -> SchedulerInput:
    """Update a level without changing the stable key used by rules or residents."""
    instance.curriculum_for(pgy)
    normalized_code, normalized_label = _validated_identity(
        instance,
        code,
        label,
        excluding=pgy,
    )
    raw = instance.model_dump(mode="json")
    curriculum = next(
        item for item in raw["requirements"] if int(item["pgy"]) == pgy
    )
    curriculum["code"] = normalized_code
    curriculum["label"] = normalized_label
    return SchedulerInput.model_validate(raw)


def remove_training_level(instance: SchedulerInput, pgy: int) -> SchedulerInput:
    """Remove an unused level and all rules scoped exclusively to its key."""
    curriculum = instance.curriculum_for(pgy)
    if len(instance.requirements) == 1:
        raise ValueError("a workspace must keep at least one training level")
    residents = [
        resident.name for resident in instance.residents if resident.pgy == pgy
    ]
    if residents:
        raise ValueError(
            f"move or remove {len(residents)} resident(s) from "
            f"{curriculum.display_label} before deleting it"
        )
    sole_rule_rotations = [
        rotation.code
        for rotation in instance.rotations
        if len(rotation.pgy_rules) == 1 and rotation.pgy_rules[0].pgy == pgy
    ]
    if sole_rule_rotations:
        raise ValueError(
            f"{curriculum.display_label} is the only configured level for rotation(s): "
            + ", ".join(sole_rule_rotations)
        )

    raw = instance.model_dump(mode="json")
    raw["requirements"] = [
        item for item in raw["requirements"] if int(item["pgy"]) != pgy
    ]
    raw["rotation_groups"] = [
        group for group in raw["rotation_groups"] if int(group["pgy"]) != pgy
    ]
    for rotation in raw["rotations"]:
        rotation["pgy_rules"] = [
            rule for rule in rotation["pgy_rules"] if int(rule["pgy"]) != pgy
        ]
        _remove_clinic_concurrency(rotation.get("clinic"), pgy)

    raw["clinic_policy"]["allocation_rules"] = [
        rule
        for rule in raw["clinic_policy"].get("allocation_rules", [])
        if rule.get("pgy") is None or int(rule["pgy"]) != pgy
    ]
    return SchedulerInput.model_validate(raw)


def _validated_identity(
    instance: SchedulerInput,
    code: str,
    label: str,
    *,
    excluding: int | None = None,
) -> tuple[str, str]:
    normalized_code = code.strip().upper()
    normalized_label = label.strip()
    if not normalized_code:
        raise ValueError("training-level code cannot be empty")
    if len(normalized_code) > 5:
        raise ValueError("training-level code must be 5 characters or fewer")
    if not normalized_label:
        raise ValueError("training-level label cannot be empty")
    if any(
        curriculum.pgy != excluding
        and curriculum.short_code.casefold() == normalized_code.casefold()
        for curriculum in instance.requirements
    ):
        raise ValueError(f"training-level code {normalized_code!r} already exists")
    if any(
        curriculum.pgy != excluding
        and curriculum.display_label.casefold() == normalized_label.casefold()
        for curriculum in instance.requirements
    ):
        raise ValueError(f"training level {normalized_label!r} already exists")
    return normalized_code, normalized_label


def _reorder_allocation_rules(
    rules: list[dict[str, Any]],
    order: dict[int, int],
) -> None:
    """Reorder level-scoped rules without moving overall or resident overrides."""
    level_rules = iter(
        sorted(
            (rule for rule in rules if rule.get("pgy") is not None),
            key=lambda rule: order[int(rule["pgy"])],
        )
    )
    for index, rule in enumerate(rules):
        if rule.get("pgy") is not None:
            rules[index] = next(level_rules)


def _remove_clinic_concurrency(clinic: Any, pgy: int) -> None:
    if not isinstance(clinic, dict):
        return
    limits = clinic.get("max_concurrent_by_pgy")
    if not isinstance(limits, dict):
        return
    limits.pop(str(pgy), None)
    limits.pop(pgy, None)
