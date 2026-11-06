"""Eligibility is explicit; legacy work and deliberate overrides remain usable."""

import json

import pytest
from pydantic import ValidationError

from rbs.attending_schedule import attending_schedule_report
from rbs.catalog import sample_instance as _sample_instance
from rbs.models.attending import Attending, AttendingWorkType
from rbs.models.attending_eligibility import migrate_eligibility
from rbs.models.clinic import ClinicStaffingMode
from rbs.models.enums import Session, Weekday
from rbs.models.rbsc import RBSC_SCHEMA_VERSION, RBSCState
from rbs.models.schedule import AssignedAttendingWork
from rbs.solver.attending_availability import attending_week_facts, potential_attending_coverage
from rbs.store import Store
from rbs.ui.clinic.ops import add_clinic, remove_clinic, set_clinic_eligible_attendings


def sample_instance():
    instance = _sample_instance()
    return instance.revised(
        clinic_policy=instance.clinic_policy.revised(
            sites=[
                site.revised(staffing_mode=ClinicStaffingMode.ATTENDING_MANAGED)
                for site in instance.clinic_policy.sites
            ]
        ),
        attendings=[
            person.revised(eligible_clinic_ids=["cedar", "maple"]) for person in instance.attendings
        ],
    )


def test_new_attendings_and_clinics_require_explicit_selection():
    person = Attending(id="new", name="New attending")
    assert person.eligible_clinic_ids == []
    instance = sample_instance()
    added = instance.clinic_policy.sites[0].revised(
        id="new_clinic",
        name="New clinic",
        allocation_rules=[],
    )
    updated = add_clinic(instance, added)
    assert all("new_clinic" not in person.eligible_clinic_ids for person in updated.attendings)


def test_inverse_editor_changes_only_the_selected_clinic_and_revalidates():
    instance = sample_instance()
    person = instance.attendings[0]
    updated = set_clinic_eligible_attendings(instance, "maple", [person.id])
    assert all("cedar" in item.eligible_clinic_ids for item in updated.attendings)
    assert [item.id for item in updated.attendings if "maple" in item.eligible_clinic_ids] == [
        person.id
    ]
    with pytest.raises(ValueError, match="unknown attending"):
        set_clinic_eligible_attendings(instance, "maple", ["missing"])
    with pytest.raises(ValidationError, match="unknown clinic"):
        instance.revised(attendings=[person.revised(eligible_clinic_ids=["missing"])])
    with pytest.raises(ValidationError, match="distinct"):
        person.revised(eligible_clinic_ids=["maple", "maple"])


def test_deleting_a_clinic_removes_eligibility_references():
    instance = sample_instance()
    raw = instance.model_dump(mode="json")
    raw["attending_schedules"] = []
    for person in raw["attendings"]:
        person["preferred_weekly_schedule_half_days"] = []
        person["schedule_template_half_days"] = []
    updated = remove_clinic(type(instance).model_validate(raw), "maple")
    assert all("maple" not in person.eligible_clinic_ids for person in updated.attendings)


def test_legacy_migration_preserves_empty_lists_and_snapshots_existing_clinics():
    policy = {"sites": [{"id": key, "staffing_mode": "attending_managed"} for key in ("a", "b")]}
    case = {"attendings": [{"id": "old"}, {"id": "new", "eligible_clinic_ids": []}]}
    migrated = migrate_eligibility(case, policy)
    assert migrated["attendings"][0]["eligible_clinic_ids"] == ["a", "b"]
    assert migrated["attendings"][1]["eligible_clinic_ids"] == []
    assert "eligible_clinic_ids" not in case["attendings"][0]


def test_document_and_cache_migrate_without_changing_revisions(tmp_path):
    store = Store(tmp_path / "workspace.sqlite")
    store.init()
    workspace = store.create("Eligibility", sample_instance())
    raw = json.loads(store.export_workspace_rbsc(workspace.id))
    raw["schema_version"] = 13
    for person in raw["workspaces"][0]["case"]["attendings"]:
        person.pop("eligible_clinic_ids")
    migrated = RBSCState.model_validate(raw)
    assert migrated.schema_version == RBSC_SCHEMA_VERSION
    assert all(
        set(p.eligible_clinic_ids) == {"maple", "cedar"}
        for p in migrated.workspaces[0].case.attendings
    )
    assert RBSCState.model_validate_json(migrated.model_dump_json()) == migrated
    with store.connect() as conn:
        case = json.loads(conn.execute("SELECT instance_json FROM workspaces").fetchone()[0])
        for person in case["attendings"]:
            person.pop("eligible_clinic_ids")
        conn.execute("UPDATE workspaces SET instance_json = ?", (json.dumps(case),))
    store.init()
    restored = store.get(workspace.id)
    assert restored.instance.attendings == workspace.instance.attendings
    assert restored.workspace_revision == workspace.workspace_revision
    assert restored.instance_revision == workspace.instance_revision


def test_solver_excludes_ineligible_candidates_but_keeps_locked_overrides():
    from rbs.models.enums import SolverEngineName, SolverStatus
    from rbs.models.schedule import Schedule, ScheduleMeta

    instance = sample_instance()
    # Avoid dated or manually entered work masking the half-day under test.
    person = Attending(id="one", name="One", eligible_clinic_ids=[])
    policy = instance.clinic_policy.revised(
        sites=[
            site.revised(staffing_mode=ClinicStaffingMode.ATTENDING_MANAGED)
            for site in instance.clinic_policy.sites
        ]
    )
    instance = instance.revised(attendings=[person], attending_schedules=[], clinic_policy=policy)
    facts = attending_week_facts(instance)[1][0]
    assert facts.precepting_clinics == ()
    assert potential_attending_coverage(instance) == []
    work = AssignedAttendingWork(
        attending_id="one",
        week=1,
        weekday=Weekday.MONDAY,
        session=Session.MORNING,
        work_type=AttendingWorkType.PRECEPTING_CLINIC,
        clinic_id="cedar",
        locked=True,
    )
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.CP_SAT,
            status=SolverStatus.FEASIBLE,
        ),
        attending_work=[work],
    )
    kept = attending_week_facts(instance, schedule)[1][0].locked_work
    assert len(kept) == 1 and kept[0].manual_override
    assert any(
        issue.code == "attending_clinic_eligibility"
        for issue in attending_schedule_report(instance, schedule=schedule).warnings
    )


def test_legacy_json_import_expands_only_missing_eligibility():
    from rbs.ingest import loads_instance

    raw = sample_instance().model_dump(mode="json")
    raw["attendings"][0].pop("eligible_clinic_ids")
    raw["attendings"][1]["eligible_clinic_ids"] = []
    restored = loads_instance(json.dumps(raw))
    assert restored.attendings[0].eligible_clinic_ids == ["cedar", "maple"]
    assert restored.attendings[1].eligible_clinic_ids == []


def test_capacity_managed_mode_clears_case_and_locked_schedule_work_atomically(tmp_path):
    from rbs.models.attending import (
        AttendingSchedule,
        AttendingWeeklyWorkSchedule,
        AttendingWorkHalfDay,
    )
    from rbs.ui.attendings.ops import attending_working_draft
    from rbs.ui.clinic.ops import replace_clinic
    from rbs.workspaces import WorkspaceController

    instance = sample_instance()
    half_day = AttendingWorkHalfDay(
        weekday=Weekday.MONDAY,
        session=Session.MORNING,
        work_type=AttendingWorkType.PRECEPTING_CLINIC,
        clinic_id="cedar",
    )
    person = Attending(
        id="one",
        name="One",
        eligible_clinic_ids=["cedar", "maple"],
        preferred_weekly_schedule_half_days=[half_day],
        schedule_template_half_days=[half_day],
    )
    instance = instance.revised(
        attendings=[person],
        attending_schedules=[
            AttendingSchedule(
                attending_id=person.id,
                weeks=[
                    AttendingWeeklyWorkSchedule(week=1, half_days=[half_day], half_days_override=3),
                ],
            ),
        ],
    )
    removed = AssignedAttendingWork(
        attending_id=person.id, week=2, **half_day.model_dump(), locked=True
    )
    kept = removed.revised(clinic_id="maple", weekday=Weekday.THURSDAY)
    draft = attending_working_draft(instance, None).revised(attending_work=[removed, kept])
    store = Store(tmp_path / "switch.sqlite")
    store.init()
    controller = WorkspaceController(store)
    workspace = controller.save_schedule(store.create("Switch", instance), draft)
    updated = replace_clinic(
        instance,
        "cedar",
        instance.clinic_policy.site("cedar").revised(
            staffing_mode=ClinicStaffingMode.CAPACITY_MANAGED,
        ),
    )
    assert updated.attendings[0].eligible_clinic_ids == ["maple"]
    assert updated.attendings[0].preferred_weekly_schedule_half_days == []
    assert updated.attendings[0].schedule_template_half_days == []
    assert updated.attending_schedules[0].weeks[0].half_days == []
    assert updated.attending_schedules[0].weeks[0].half_days_override == 3
    saved = controller.save_instance(workspace, updated)
    assert saved.latest_schedule.attending_work == [kept]
    assert saved.solution_is_out_of_date
    assert kept.clinic_id in updated.attending_managed_clinic_ids


def test_capacity_managed_clinics_need_no_attendings_for_readiness():
    from rbs.models.instance import SolverProblem
    from rbs.solver.readiness import check_solve_readiness

    instance = _sample_instance().revised(attendings=[], attending_schedules=[])
    assert not instance.attending_managed_clinic_ids
    assert check_solve_readiness(SolverProblem.from_instance(instance)).ready


def test_capacity_managed_eligibility_is_cleared_and_cannot_be_added():
    instance = _sample_instance()
    assert all(not person.eligible_clinic_ids for person in instance.attendings)
    with pytest.raises(ValueError, match="Only attending-managed"):
        set_clinic_eligible_attendings(instance, "cedar", [instance.attendings[0].id])
