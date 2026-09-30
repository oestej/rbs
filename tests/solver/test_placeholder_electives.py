"""Placeholder electives: generic elective blocks without clinic or preferences.

These tests stay deterministic: they expand occurrences, validate headers,
and check readiness without starting a real CP-SAT search.
"""

from rbs.catalog import sample_instance as base_sample_instance
from rbs.models.elective import (
    PLACEHOLDER_ELECTIVE_COLOR,
    PLACEHOLDER_ELECTIVE_ID,
)
from rbs.models.enums import RotationKind
from rbs.models.instance import SchedulerInput
from rbs.models.schedule import Assignment
from rbs.solver.planning import expand_occurrences
from rbs.solver.readiness import _missing_elective_fallbacks
from rbs.solver.validation_assignments import _validate_assignment_header


def _instance() -> SchedulerInput:
    """Sample year with one configured 2-week elective and one request."""
    raw = base_sample_instance().model_dump(mode="json")
    source = next(rotation for rotation in raw["rotations"] if rotation["id"] == "elective")
    configured = {**source, "id": "test_elective", "code": "T-EL", "name": "Test Elective"}
    raw["rotations"].append(configured)
    raw["electives"]["rotation_options"] = [
        {"rotation_id": configured["id"], "eligible_block_sizes": [2]},
    ]
    raw["residents"][0]["elective_preferences"] = [
        {"rotation_id": configured["id"], "duration_weeks": 2}
    ]
    return SchedulerInput.model_validate(raw)


def test_placeholder_electives_off_by_default() -> None:
    assert _instance().use_placeholder_electives is False


def test_placeholder_rotation_is_neutral_gray_without_clinic() -> None:
    instance = _instance()
    placeholder = instance.rotation(PLACEHOLDER_ELECTIVE_ID)
    assert placeholder.color == PLACEHOLDER_ELECTIVE_COLOR
    assert placeholder.clinic_hours_disabled
    assert placeholder.away
    for pgy in instance.training_level_ids:
        for duration in instance.elective_block_durations_for_pgy(pgy):
            assert placeholder.allows_duration(duration, pgy=pgy)
    assert (
        instance.assignment_color(PLACEHOLDER_ELECTIVE_ID, elective=True)
        == PLACEHOLDER_ELECTIVE_COLOR
    )
    assert instance.assignment_name(PLACEHOLDER_ELECTIVE_ID, elective=True) == (
        "Placeholder (Elec)"
    )


def test_placeholder_mode_preserves_preferences() -> None:
    instance = _instance()
    enabled = instance.revised(use_placeholder_electives=True)
    assert enabled.use_placeholder_electives is True
    assert enabled.residents[0].elective_preferences == (
        instance.residents[0].elective_preferences
    )
    assert enabled.scheduling_case().use_placeholder_electives is True


def test_placeholder_mode_replaces_slots_without_clinic_backfill() -> None:
    enabled = _instance().revised(use_placeholder_electives=True)
    elective = [item for item in expand_occurrences(enabled) if item.elective]
    assert elective
    assert all(item.rotation_id == PLACEHOLDER_ELECTIVE_ID for item in elective)
    assert not any(item.elective_fallback for item in elective)
    assert not any(item.preference_managed for item in elective)

    ordinary = [item for item in expand_occurrences(_instance()) if item.elective]
    assert ordinary
    assert not any(item.rotation_id == PLACEHOLDER_ELECTIVE_ID for item in ordinary)


def test_placeholder_assignment_eligible_only_while_option_on() -> None:
    instance = _instance()
    enabled = instance.revised(use_placeholder_electives=True)
    assignment = Assignment(
        resident_id=instance.residents[0].id,
        rotation_id=PLACEHOLDER_ELECTIVE_ID,
        kind=enabled.rotation(PLACEHOLDER_ELECTIVE_ID).kind,
        elective=True,
        elective_fallback=False,
        start_week=1,
        end_week=2,
        weeks=[1, 2],
        block_start_week=1,
        block_duration_weeks=2,
    )
    errors: list[str] = []
    _validate_assignment_header(
        enabled,
        assignment,
        enabled.rotation(PLACEHOLDER_ELECTIVE_ID),
        set(range(1, enabled.calendar.weeks + 1)),
        errors,
    )
    assert errors == []

    stale: list[str] = []
    _validate_assignment_header(
        instance,
        assignment,
        instance.rotation(PLACEHOLDER_ELECTIVE_ID),
        set(range(1, instance.calendar.weeks + 1)),
        stale,
    )
    assert any("not eligible" in error for error in stale)


def test_placeholder_mode_needs_no_clinic_fallback() -> None:
    instance = _instance()
    stripped = []
    for rotation in instance.rotations:
        if rotation.kind is not RotationKind.CLINIC:
            stripped.append(rotation)
            continue
        rules = [
            rule.model_copy(
                update={
                    "block_configs": [
                        config
                        for config in rule.block_configs
                        if config.duration_weeks != 2
                    ]
                }
            )
            for rule in rotation.pgy_rules
        ]
        rules = [rule for rule in rules if rule.block_configs]
        if rules:
            stripped.append(rotation.model_copy(update={"pgy_rules": rules}))
    # model_copy skips validation so the missing fallback survives to readiness.
    broken = instance.model_copy(update={"rotations": stripped})
    staffed = {resident.pgy for resident in broken.residents}
    assert _missing_elective_fallbacks(broken, staffed)
    enabled = broken.model_copy(update={"use_placeholder_electives": True})
    assert _missing_elective_fallbacks(enabled, staffed) == []
