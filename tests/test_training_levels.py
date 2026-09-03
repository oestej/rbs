from copy import deepcopy

import pytest
from pydantic import ValidationError

from rbs.catalog import sample_instance
from rbs.models.curriculum import PGYCurriculum
from rbs.models.instance import SchedulerInput, SolverProblem
from rbs.models.resident import Resident
from rbs.solver.planning import expand_occurrences
from rbs.store import Store
from rbs.training_levels import (
    add_training_level,
    remove_training_level,
    reorder_training_levels,
    update_training_level,
)


def test_legacy_curricula_derive_short_codes_and_names() -> None:
    raw = sample_instance().model_dump(mode="json")
    for curriculum in raw["requirements"]:
        curriculum.pop("code", None)
        curriculum.pop("label", None)

    restored = SchedulerInput.model_validate(raw)

    assert restored.training_level_options == {
        1: "PGY1",
        2: "PGY2",
        3: "PGY3",
    }
    assert restored.training_level_label(2) == "PGY 2"


def test_configured_training_levels_come_from_catalog_requirements() -> None:
    raw = sample_instance().model_dump(mode="json")
    raw["requirements"].append(
        {
            "pgy": 4,
            "code": "SM-F",
            "label": "Sports Medicine Fellow",
            "blocks": [],
        }
    )

    assert SchedulerInput.model_validate(raw).training_level_options == {
        1: "PGY1",
        2: "PGY2",
        3: "PGY3",
        4: "SM-F",
    }


def test_training_level_code_is_short_normalized_and_safe() -> None:
    curriculum = PGYCurriculum(pgy=4, code=" sm-f ", label=" Sports Medicine Fellow ")

    assert curriculum.short_code == "SM-F"
    assert curriculum.display_label == "Sports Medicine Fellow"

    with pytest.raises(ValidationError, match="at most 5 characters"):
        PGYCurriculum(pgy=4, code="SPORTS", label="Sports Medicine Fellow")
    with pytest.raises(ValidationError, match="letters, numbers, and hyphens"):
        PGYCurriculum(pgy=4, code="SM F", label="Sports Medicine Fellow")
    with pytest.raises(ValidationError, match="must be set to 5 characters or fewer"):
        PGYCurriculum(pgy=123)


def test_add_training_level_starts_without_inherited_curriculum_or_rules() -> None:
    instance = sample_instance()
    updated = add_training_level(
        instance,
        code="SMF",
        label="Sports Medicine Fellow",
    )

    assert updated.training_level_ids == (1, 2, 3, 4)
    assert updated.training_level_label(4, compact=True) == "SMF"
    assert updated.training_level_label(4) == "Sports Medicine Fellow"
    assert updated.curriculum_for(4).required_weeks() == 0
    for rotation in updated.rotations:
        assert all(rule.pgy != 4 for rule in rotation.pgy_rules)
        assert (
            rotation.clinic is None
            or 4 not in rotation.clinic.max_concurrent_by_pgy
        )
    assert all(group.pgy != 4 for group in updated.rotation_groups)
    assert all(rule.pgy != 4 for rule in updated.clinic_policy.allocation_rules)


def _explicitly_configure_like(
    instance: SchedulerInput,
    *,
    target: int,
    source: int,
) -> SchedulerInput:
    """Configure a test track explicitly after its neutral creation."""
    raw = instance.model_dump(mode="json")
    source_curriculum = next(
        item for item in raw["requirements"] if int(item["pgy"]) == source
    )
    target_curriculum = next(
        item for item in raw["requirements"] if int(item["pgy"]) == target
    )
    target_curriculum["blocks"] = deepcopy(source_curriculum["blocks"])
    for rotation in raw["rotations"]:
        source_rule = next(
            (
                rule
                for rule in rotation["pgy_rules"]
                if int(rule["pgy"]) == source
            ),
            None,
        )
        if source_rule is not None:
            configured_rule = deepcopy(source_rule)
            configured_rule["pgy"] = target
            rotation["pgy_rules"].append(configured_rule)
    return SchedulerInput.model_validate(raw)


def test_fellowship_level_expands_through_solver_planning() -> None:
    updated = _explicitly_configure_like(
        add_training_level(
            sample_instance(),
            code="SMF",
            label="Sports Medicine Fellow",
        ),
        target=4,
        source=2,
    )
    raw = updated.model_dump(mode="json")
    raw["residents"] = [
        Resident(id="fellow-001", name="Finley Fellow", pgy=4).model_dump(mode="json")
    ]
    raw["locks"] = []
    raw["manual_clinic_blocks"] = []
    raw["resident_rotation_overrides"] = []
    raw["special_rotations"] = []
    problem = SolverProblem.from_instance(SchedulerInput.model_validate(raw))

    occurrences = expand_occurrences(problem, require_configured_electives=False)

    assert occurrences
    assert {occurrence.pgy for occurrence in occurrences} == {4}
    assert all(
        problem.rotation(occurrence.rotation_id).allows_duration(
            occurrence.duration_weeks,
            pgy=4,
        )
        for occurrence in occurrences
    )


def test_update_and_remove_training_level_preserve_stable_references() -> None:
    instance = sample_instance()
    added = add_training_level(
        instance,
        code="SMF",
        label="Sports Medicine Fellow",
    )
    renamed = update_training_level(
        added,
        4,
        "CF",
        "Cardiology Fellow",
    )

    assert renamed.training_level_ids[-1] == 4
    assert renamed.training_level_label(4, compact=True) == "CF"
    assert all(rule.pgy != 4 for rule in renamed.rotation("fmed").pgy_rules)
    assert remove_training_level(renamed, 4) == instance


def test_training_level_identity_is_unique_and_assigned_levels_cannot_be_removed() -> None:
    instance = add_training_level(
        sample_instance(),
        code="SMF",
        label="Sports Medicine Fellow",
    )

    with pytest.raises(ValueError, match="code 'PGY1' already exists"):
        update_training_level(instance, 4, "PGY1", "Fellow")
    with pytest.raises(ValueError, match="Sports Medicine Fellow.*already exists"):
        update_training_level(instance, 1, "NEW", "Sports Medicine Fellow")

    raw = instance.model_dump(mode="json")
    raw["residents"].append(
        Resident(id="fellow-001", name="Finley Fellow", pgy=4).model_dump(mode="json")
    )
    assigned = SchedulerInput.model_validate(raw)
    with pytest.raises(ValueError, match="move or remove 1 resident"):
        remove_training_level(assigned, 4)


def test_training_level_order_drives_options_and_dependent_rule_lists() -> None:
    raw = sample_instance().model_dump(mode="json")
    fmed = next(rotation for rotation in raw["rotations"] if rotation["id"] == "fmed")
    fmed["clinic"]["max_concurrent_by_pgy"] = {"1": 1, "2": 2}
    raw["clinic_policy"]["allocation_rules"].extend(
        [
            {
                "clinic_id": clinic_id,
                "pgy": pgy,
                "min_fraction": 0.0,
                "target_fraction": 0.5,
                "max_fraction": 1.0,
            }
            for pgy in (1, 2)
            for clinic_id in ("maple", "cedar")
        ]
    )
    instance = SchedulerInput.model_validate(raw)

    reordered = reorder_training_levels(instance, (2, 1, 3))

    assert reordered.training_level_ids == (2, 1, 3)
    assert list(reordered.training_level_options) == [2, 1, 3]
    assert [rule.pgy for rule in reordered.rotation("fmed").pgy_rules] == [2, 1, 3]
    assert [group.pgy for group in reordered.rotation_groups] == [2, 1]
    assert [
        rule.pgy
        for rule in reordered.clinic_policy.allocation_rules
        if rule.pgy is not None
    ] == [2, 2, 1, 1]
    assert reordered.training_level_sort_key(2) < reordered.training_level_sort_key(1)

    with pytest.raises(ValueError, match="every configured level exactly once"):
        reorder_training_levels(instance, (1, 1, 3))


def test_configured_training_levels_round_trip_through_workspace_storage(tmp_path) -> None:
    instance = add_training_level(
        sample_instance(),
        code="SMF",
        label="Sports Medicine Fellow",
    )
    instance = reorder_training_levels(instance, (4, 2, 1, 3))
    store = Store(tmp_path / "rbs.sqlite")
    store.init()

    workspace = store.create("Fellowship", instance)
    restored = store.get(workspace.id).instance

    assert restored.training_level_label(4, compact=True) == "SMF"
    assert restored.training_level_label(4) == "Sports Medicine Fellow"
    assert restored.curriculum_for(4) == instance.curriculum_for(4)
    assert restored.training_level_ids == (4, 2, 1, 3)
