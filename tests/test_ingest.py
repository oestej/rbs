import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from rbs.catalog import sample_instance
from rbs.emit import dumps, dumps_bundle
from rbs.ingest import load_instance, loads_instance, parse_workspace_payload
from rbs.models.instance import SchedulerInput

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_committed_sample_input_loads() -> None:
    path = REPO_ROOT / "data" / "sample_input.json"
    instance = load_instance(path)
    assert instance.academic_year == "2026-2027"
    assert instance.solver.time_limit_seconds == 60
    assert instance.cohort_counts() == {1: 8, 2: 8, 3: 8}
    assert instance.curriculum_for(1).required_weeks() == 52
    assert instance.curriculum_for(2).required_weeks() == 52
    assert instance.curriculum_for(3).required_weeks() == 52
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "rotations" not in raw
    assert "requirements" not in raw


def test_sample_round_trip() -> None:
    original = sample_instance()
    restored = SchedulerInput.model_validate(original.model_dump(mode="json"))
    assert restored.academic_year == original.academic_year
    assert restored.cohort_counts() == original.cohort_counts()
    assert [r.id for r in restored.rotations] == [r.id for r in original.rotations]


def test_legacy_phased_rotations_are_rejected() -> None:
    raw = sample_instance().model_dump(mode="json")
    raw["rotations"][0]["phases"] = []
    raw["rotations"][0]["pairing"] = {}

    with pytest.raises(ValidationError, match="phases|pairing"):
        SchedulerInput.model_validate(raw)


def test_workspace_import_rejects_legacy_rotation_notes() -> None:
    payload = sample_instance().model_dump(mode="json")
    payload["rotations"][0]["notes"] = "Retired free-text rule"

    with pytest.raises(ValidationError, match="notes"):
        loads_instance(json.dumps(payload))


def test_academic_half_day_overrides_round_trip_with_the_workspace() -> None:
    raw = sample_instance().model_dump(mode="json")
    raw["academic_half_day_overrides"] = [
        {
            "week": 12,
            "weekday": "tuesday",
            "session": "morning",
        }
    ]

    restored = SchedulerInput.model_validate(raw)
    case = restored.scheduling_case()

    assert restored.academic_half_day_overrides[0].week == 12
    assert case.academic_half_day_overrides == restored.academic_half_day_overrides


def test_resident_clinic_half_days_round_trip_with_the_workspace() -> None:
    raw = sample_instance().model_dump(mode="json")
    raw["residents"][0]["clinic_half_days"] = [
        {
            "weekday": "tuesday",
            "session": "morning",
            "sites": ["maple"],
        }
    ]

    restored = SchedulerInput.model_validate(raw)
    case = restored.scheduling_case()

    half_day = restored.residents[0].clinic_half_days[0]
    assert half_day.weekday.value == "tuesday"
    assert half_day.session.value == "morning"
    assert half_day.sites == ["maple"]
    assert case.residents[0].clinic_half_days == [half_day]


def test_load_self_contained_file(sample_input_path) -> None:
    instance = load_instance(sample_input_path)
    assert instance.cohort_counts() == {1: 8, 2: 8, 3: 8}
    assert len(instance.rotations) == len(sample_instance().rotations)


def test_load_residents_only_merges_default_catalog(tmp_path) -> None:
    payload = sample_instance().model_dump(mode="json")
    payload.pop("rotations")
    payload.pop("requirements")
    path = tmp_path / "residents_only.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    instance = load_instance(path)
    assert instance.curriculum_for(1).required_weeks() == 52
    assert instance.curriculum_for(2).required_weeks() == 52


def test_rejects_unknown_field() -> None:
    payload = sample_instance().model_dump(mode="json")
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        loads_instance(json.dumps(payload))


def test_rejects_duplicate_resident_ids() -> None:
    payload = sample_instance().model_dump(mode="json")
    payload["residents"][1]["id"] = payload["residents"][0]["id"]
    with pytest.raises(ValidationError, match="resident ids must be unique"):
        loads_instance(json.dumps(payload))


def test_rejects_vacation_week_out_of_range() -> None:
    payload = sample_instance().model_dump(mode="json")
    payload["residents"][0]["vacation_weeks"] = [0, 12]
    with pytest.raises(ValidationError, match="outside 1..52"):
        loads_instance(json.dumps(payload))


def test_rejects_individual_day_off_outside_academic_year() -> None:
    payload = sample_instance().model_dump(mode="json")
    payload["residents"][0]["days_off"] = ["2027-06-28"]

    with pytest.raises(ValidationError, match="outside academic year"):
        loads_instance(json.dumps(payload))


def test_rejects_unknown_rotation_reference() -> None:
    payload = sample_instance().model_dump(mode="json")
    payload["requirements"][0]["blocks"][0]["rotation_id"] = "not_a_rotation"
    with pytest.raises(ValidationError, match="unknown rotation"):
        loads_instance(json.dumps(payload))


def test_rejects_illegal_block_duration() -> None:
    payload = sample_instance().model_dump(mode="json")
    payload["requirements"][0]["blocks"][0]["duration_weeks"] = 3
    with pytest.raises(ValidationError, match="duration 3"):
        loads_instance(json.dumps(payload))


def test_parse_bundle_round_trip(instance) -> None:
    from rbs.solver.core import get_engine

    schedule = get_engine("stub").solve(instance, options=instance.solver)
    bundle = json.loads(dumps_bundle(instance, schedule))
    restored, restored_schedule = parse_workspace_payload(bundle)
    assert restored.cohort_counts() == instance.cohort_counts()
    assert restored_schedule is not None
    assert restored_schedule.meta.engine == schedule.meta.engine


def test_parse_bundle_rejects_schedule_for_different_instance(instance) -> None:
    from rbs.solver.core import get_engine

    schedule = get_engine("stub").solve(instance, options=instance.solver)
    payload = json.loads(dumps_bundle(instance, schedule))
    payload["schedule"]["meta"]["academic_year"] = "2030-2031"
    with pytest.raises(ValueError, match="does not match"):
        parse_workspace_payload(payload)


def test_parse_rejects_schedule_only() -> None:
    with pytest.raises(ValueError, match="instance"):
        parse_workspace_payload({"meta": {"academic_year": "2026-2027"}})


def test_dumps_pretty_json(instance) -> None:
    text = dumps(instance)
    assert text.endswith("\n")
    parsed = json.loads(text)
    assert parsed["academic_year"] == "2026-2027"
