import io
import json
import sqlite3
from datetime import date

import pytest
from pydantic import ValidationError

from rbs.catalog import sample_instance
from rbs.emit import dumps, dumps_bundle
from rbs.logging import LoggingConfig, configure_logging
from rbs.store import Store, WorkspaceConflictError


def test_commit_listener_observes_changed_commits_and_can_export(tmp_path) -> None:
    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    observed: list[list[str]] = []

    def listener() -> None:
        state = json.loads(store.export_rbsc())
        observed.append([workspace["name"] for workspace in state["workspaces"]])

    unsubscribe = store.add_commit_listener(listener)
    store.list()
    assert observed == []

    workspace = store.create("Committed", sample_instance())
    assert observed
    assert observed[-1] == ["Committed"]

    before_rollback = len(observed)
    with pytest.raises(RuntimeError, match="rollback"):
        with store.connect() as connection:
            connection.execute(
                "UPDATE workspaces SET name = 'Not committed' WHERE id = ?",
                (workspace.id,),
            )
            raise RuntimeError("rollback")
    assert len(observed) == before_rollback
    assert store.get(workspace.id).name == "Committed"

    unsubscribe()
    store.rename(
        workspace.id,
        "After unsubscribe",
        expected_workspace_revision=workspace.workspace_revision,
    )
    assert len(observed) == before_rollback


def test_commit_listener_failure_does_not_relabel_a_committed_edit_as_failed(tmp_path) -> None:
    diagnostics = io.StringIO()
    runtime = configure_logging(
        LoggingConfig(
            runtime="local",
            component="test",
            destination="stdout",
            stream=diagnostics,
        )
    )
    store = Store(tmp_path / "rbs.sqlite")
    store.init()

    def fail() -> None:
        raise RuntimeError("recovery disk unavailable")

    try:
        store.add_commit_listener(fail)
        workspace = store.create("Still committed", sample_instance())
    finally:
        runtime.close()

    assert store.get(workspace.id).name == "Still committed"
    records = [json.loads(line) for line in diagnostics.getvalue().splitlines()]
    assert records
    assert {record["event"] for record in records} == {"database.commit_listener_failed"}
    assert "recovery disk unavailable" not in diagnostics.getvalue()


def test_create_list_and_current(tmp_path) -> None:
    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("Year A", sample_instance())
    assert store.current_id() == workspace.id
    assert store.get(workspace.id).name == "Year A"
    assert len(store.list()) == 1


def test_ensure_sample_seeds_once(tmp_path) -> None:
    store = Store(tmp_path / "rbs.sqlite")
    first = store.ensure_sample()
    second = store.ensure_sample()
    assert first.id == second.id
    assert len(store.list()) == 1
    assert first.instance.cohort_counts() == {1: 8, 2: 8, 3: 8}
    assert first.is_sample


def test_ensure_sample_preserves_custom_constraints(tmp_path) -> None:
    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    stale = sample_instance()
    icu = stale.rotation("icu")
    rules = [
        rule.model_copy(update={"prerequisite_rotation_ids": [], "earliest_start_week": None})
        for rule in icu.pgy_rules
    ]
    custom_icu = icu.model_copy(update={"pgy_rules": rules})
    rotations = [custom_icu if rotation.id == "icu" else rotation for rotation in stale.rotations]
    stale = stale.model_copy(update={"rotations": rotations})
    workspace = store.create("Sample 2026-2027", stale)
    saved_rule = workspace.instance.rotation("icu").pgy_rule(1)
    assert saved_rule.prerequisite_rotation_ids == []
    assert saved_rule.earliest_start_week is None
    refreshed = store.ensure_sample()
    assert refreshed.id == workspace.id
    assert refreshed.catalog_id == workspace.catalog_id
    saved_rule = refreshed.instance.rotation("icu").pgy_rule(1)
    assert saved_rule.prerequisite_rotation_ids == []
    assert saved_rule.earliest_start_week is None


def test_import_export_instance(tmp_path) -> None:
    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    imported = store.import_json("from-file", dumps(sample_instance()))
    exported = json.loads(store.export_instance(imported.id))
    assert exported["academic_year"] == "2026-2027"
    assert len(exported["residents"]) == 24


def test_import_export_bundle(tmp_path) -> None:
    from rbs.solver.core import get_engine

    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    instance = sample_instance()
    schedule = get_engine("stub").solve(instance, options=instance.solver)
    imported = store.import_json("bundle", dumps_bundle(instance, schedule))
    assert imported.schedule is not None
    bundle = json.loads(store.export_bundle(imported.id))
    assert "instance" in bundle
    assert "schedule" in bundle


def test_rbsc_round_trip_replaces_and_restores_the_complete_database(tmp_path) -> None:
    from rbs.models.instance import SchedulerInput
    from rbs.solver.core import get_engine

    source = Store(tmp_path / "source.sqlite")
    source.init()
    instance = sample_instance()
    first = source.create("Primary year", instance)
    raw = instance.model_dump(mode="json")
    raw["rotations"][0]["name"] = "Second portable catalog"
    modified = SchedulerInput.model_validate(raw)
    second = source.create(
        "Next year",
        modified,
        get_engine("stub").solve(modified, options=modified.solver),
    )
    source.set_current(second.id)
    with source.connect() as connection:
        connection.execute(
            "INSERT INTO app_meta (key, value) VALUES (?, ?)",
            ("portable_test_marker", "preserved"),
        )

    payload = source.export_rbsc()
    exported = json.loads(payload)

    assert exported["format"] == "rbsc"
    assert exported["schema_version"] == 6
    assert exported["current_workspace_id"] == second.id
    assert exported["app_metadata"]["portable_test_marker"] == "preserved"
    assert {row["id"] for row in exported["workspaces"]} == {first.id, second.id}
    assert len(exported["catalogs"]) >= 2

    target = Store(tmp_path / "target.sqlite")
    target.init()
    discarded = target.create("Discard me", sample_instance())

    restored_state = target.restore_rbsc(payload)

    assert restored_state.current_workspace_id == second.id
    assert target.current_id() == second.id
    assert {workspace.name for workspace in target.list()} == {
        "Primary year",
        "Next year",
    }
    assert all(
        workspace.id != discarded.id or workspace.name != "Discard me"
        for workspace in target.list()
    )
    restored_second = target.get(second.id)
    assert restored_second.schedule is not None
    assert restored_second.schedule_revision == second.schedule_revision
    assert restored_second.instance.rotation("fmed").name == "Second portable catalog"
    with target.connect() as connection:
        marker = connection.execute(
            "SELECT value FROM app_meta WHERE key = ?",
            ("portable_test_marker",),
        ).fetchone()
    assert marker["value"] == "preserved"

    reexported = json.loads(target.export_rbsc())
    exported.pop("exported_at")
    reexported.pop("exported_at")
    assert reexported == exported


def test_rbsc_v6_omits_application_preferences_and_derived_locks(tmp_path) -> None:
    from rbs.models.instance import SchedulerInput

    raw = sample_instance().model_dump(mode="json")
    raw["color_scheme"]["name"] = "Local institution"
    raw["solver"]["num_workers"] = 3
    raw["lock_through_today"] = True
    raw["locks"][0]["source"] = "through_today"
    instance = SchedulerInput.model_validate(raw)
    store = Store(tmp_path / "source.sqlite")
    store.init()
    workspace = store.create("Portable data", instance)

    exported = json.loads(store.export_workspace_rbsc(workspace.id))
    case = exported["workspaces"][0]["case"]

    assert exported["schema_version"] == 6
    assert "color_scheme" not in case
    assert "solver" not in case
    assert "lock_through_today" not in case
    assert {lock["source"] for lock in case["locks"]} == {"manual"}
    assert not _contains_key(exported["catalogs"], "color")

    restored = Store(tmp_path / "target.sqlite")
    restored.init()
    imported = restored.import_workspace_rbsc(json.dumps(exported))[0]
    assert imported.instance.color_scheme.name == "RBS Navy & Gold"
    assert imported.instance.solver.num_workers == 8
    assert not imported.instance.lock_through_today


def test_rbsc_v6_coalesces_catalogs_that_differ_only_by_colors(tmp_path) -> None:
    from rbs.models.instance import SchedulerInput

    source = Store(tmp_path / "source.sqlite")
    source.init()
    first = sample_instance()
    raw = first.model_dump(mode="json")
    raw["rotations"][0]["color"] = "#123456"
    second = SchedulerInput.model_validate(raw)
    source.create("First", first)
    source.create("Second", second)
    assert len(source.list_catalogs()) >= 2

    exported = json.loads(source.export_rbsc())

    assert len(exported["catalogs"]) == 1
    assert {workspace["catalog_id"] for workspace in exported["workspaces"]} == {
        exported["catalogs"][0]["id"]
    }
    target = Store(tmp_path / "target.sqlite")
    target.restore_rbsc(json.dumps(exported))
    assert {workspace.name for workspace in target.list()} == {"First", "Second"}


def _contains_key(value, target: str) -> bool:
    if isinstance(value, dict):
        return target in value or any(_contains_key(item, target) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, target) for item in value)
    return False


def test_rbsc_restore_rejects_legacy_rotation_notes(tmp_path) -> None:
    source = Store(tmp_path / "source.sqlite")
    source.init()
    source.create("Legacy notes", sample_instance())
    exported = json.loads(source.export_rbsc())
    for record in exported["catalogs"]:
        record["catalog"]["rotations"][0]["notes"] = "Retired portable note"

    target = Store(tmp_path / "target.sqlite")
    with pytest.raises(ValidationError, match="notes"):
        target.restore_rbsc(json.dumps(exported))


def test_invalid_rbsc_is_rejected_before_existing_state_changes(tmp_path) -> None:
    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("Keep me", sample_instance())
    before = json.loads(store.export_rbsc())
    invalid = json.loads(store.export_rbsc())
    invalid["workspaces"][0]["catalog_id"] = 999_999

    with pytest.raises(ValueError, match="missing catalog"):
        store.restore_rbsc(json.dumps(invalid))

    assert store.get(workspace.id).name == "Keep me"
    after = json.loads(store.export_rbsc())
    before.pop("exported_at")
    after.pop("exported_at")
    assert after == before


def test_rbsc_restore_rolls_back_if_database_insertion_fails(tmp_path) -> None:
    source = Store(tmp_path / "source.sqlite")
    source.init()
    source.create("Portable year", sample_instance())
    invalid = json.loads(source.export_rbsc())
    duplicate = dict(invalid["catalogs"][0])
    duplicate["id"] = max(row["id"] for row in invalid["catalogs"]) + 1
    invalid["catalogs"].append(duplicate)

    target = Store(tmp_path / "target.sqlite")
    target.init()
    workspace = target.create("Keep me", sample_instance())
    before = json.loads(target.export_rbsc())

    with pytest.raises(sqlite3.IntegrityError, match="catalogs.content_hash"):
        target.restore_rbsc(json.dumps(invalid))

    assert target.get(workspace.id).name == "Keep me"
    after = json.loads(target.export_rbsc())
    before.pop("exported_at")
    after.pop("exported_at")
    assert after == before


def test_rbsc_preserves_a_stale_schedule_and_its_revision(tmp_path) -> None:
    from rbs.solver.core import get_engine

    source = Store(tmp_path / "source.sqlite")
    source.init()
    instance = sample_instance()
    created = source.create(
        "Stale schedule",
        instance,
        get_engine("stub").solve(instance, options=instance.solver),
    )
    residents = list(instance.residents)
    residents[0] = residents[0].model_copy(update={"name": "Changed after solve"})
    changed = instance.model_copy(update={"residents": residents})
    stale = source.save_instance(
        created.id,
        changed,
        expected_workspace_revision=created.workspace_revision,
    )
    assert stale.stale_schedule is not None

    target = Store(tmp_path / "target.sqlite")
    target.init()
    target.restore_rbsc(source.export_rbsc())

    restored = target.get(created.id)
    assert restored.schedule is None
    assert restored.stale_schedule is not None
    assert restored.instance_revision == stale.instance_revision
    assert restored.schedule_revision == stale.schedule_revision


def test_save_instance_and_delete(tmp_path) -> None:
    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("edit-me", sample_instance())
    instance = workspace.instance
    residents = list(instance.residents)
    residents[0] = residents[0].model_copy(
        update={"name": "Renamed Person", "days_off": [date(2026, 9, 15)]}
    )
    updated = instance.model_copy(update={"residents": residents})
    saved = store.save_instance(
        workspace.id,
        updated,
        expected_workspace_revision=workspace.workspace_revision,
    )
    assert saved.instance.residents[0].name == "Renamed Person"
    assert saved.instance.residents[0].days_off == [date(2026, 9, 15)]
    assert saved.instance_revision == workspace.instance_revision + 1
    assert saved.schedule is None
    store.delete(
        workspace.id,
        expected_workspace_revision=saved.workspace_revision,
    )
    assert store.list() == []


def test_save_instance_can_preserve_a_matching_schedule(tmp_path) -> None:
    from rbs.solver.core import get_engine

    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    instance = sample_instance()
    schedule = get_engine("stub").solve(instance, options=instance.solver)
    workspace = store.create("preserve-me", instance, schedule)
    updated = instance.model_copy(update={"lock_through_today": True})

    saved = store.save_instance(
        workspace.id,
        updated,
        expected_workspace_revision=workspace.workspace_revision,
        preserve_schedule=True,
    )

    assert saved.instance.lock_through_today
    assert saved.schedule is not None
    assert saved.instance_revision == workspace.instance_revision + 1
    assert saved.schedule_revision == saved.instance_revision
    assert saved.schedule.meta.source_instance_revision == saved.instance_revision


def test_incomplete_manual_schedule_is_persisted_as_needing_solve(tmp_path) -> None:
    from rbs.models.enums import SolverEngineName, SolverStatus
    from rbs.models.schedule import Schedule, ScheduleMeta
    from rbs.ui.app_status import _workspace_status

    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("manual-gap", sample_instance())
    incomplete = Schedule(
        meta=ScheduleMeta(
            academic_year=workspace.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.UNKNOWN,
            solver_status=SolverStatus.UNKNOWN,
        )
    )

    saved = store.save_schedule(
        workspace.id,
        incomplete,
        expected_instance_revision=workspace.instance_revision,
        expected_workspace_revision=workspace.workspace_revision,
    )

    assert saved.schedule is not None
    assert saved.schedule.meta.status is SolverStatus.UNKNOWN
    assert _workspace_status(saved) == "Needs solve · 1248 schedule weeks open"


def test_save_instance_keeps_the_last_solution_and_marks_it_out_of_date(tmp_path) -> None:
    from rbs.solver.core import get_engine
    from rbs.ui.app_status import _workspace_status

    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    instance = sample_instance()
    workspace = store.create(
        "dirty-solution",
        instance,
        get_engine("stub").solve(instance, options=instance.solver),
    )
    residents = list(instance.residents)
    residents[0] = residents[0].model_copy(update={"name": "Updated Name"})
    updated = instance.model_copy(update={"residents": residents})

    dirty = store.save_instance(
        workspace.id,
        updated,
        expected_workspace_revision=workspace.workspace_revision,
    )

    assert dirty.schedule is None
    assert dirty.stale_schedule is not None
    assert dirty.latest_schedule is dirty.stale_schedule
    assert dirty.solution_is_out_of_date
    assert dirty.schedule_revision == workspace.instance_revision
    assert _workspace_status(dirty) == "Solution out of date"

    refreshed = store.save_schedule(
        workspace.id,
        get_engine("stub").solve(updated, options=updated.solver),
        expected_instance_revision=dirty.instance_revision,
        expected_workspace_revision=dirty.workspace_revision,
    )

    assert refreshed.schedule is not None
    assert refreshed.stale_schedule is None
    assert not refreshed.solution_is_out_of_date


def test_import_and_apply_constraint_catalog(tmp_path) -> None:
    store = Store(tmp_path / "rbs.sqlite")
    workspace = store.ensure_sample()
    raw = workspace.instance.constraint_catalog().model_dump(mode="json")
    raw["rotations"][0]["name"] = "Imported FMED"
    record = store.import_catalog_json("Imported", json.dumps(raw))

    updated = store.set_catalog(
        workspace.id,
        record.id,
        expected_workspace_revision=workspace.workspace_revision,
    )
    assert updated.catalog_id == record.id
    assert updated.instance.rotations[0].name == "Imported FMED"
    assert updated.instance_revision == workspace.instance_revision + 1


def test_stale_solve_cannot_overwrite_newer_instance(tmp_path) -> None:
    from rbs.solver.core import get_engine

    store = Store(tmp_path / "rbs.sqlite")
    workspace = store.ensure_sample()
    stale_schedule = get_engine("stub").solve(workspace.instance, options=workspace.instance.solver)
    store.save_instance(
        workspace.id,
        workspace.instance,
        expected_workspace_revision=workspace.workspace_revision,
    )

    try:
        store.save_schedule(
            workspace.id,
            stale_schedule,
            expected_instance_revision=workspace.instance_revision,
            expected_workspace_revision=workspace.workspace_revision,
        )
    except ValueError as exc:
        assert "changed" in str(exc)
    else:
        raise AssertionError("expected stale schedule save to be rejected")


def test_non_input_edit_also_invalidates_an_in_flight_solve(tmp_path) -> None:
    from rbs.solver.core import get_engine

    store = Store(tmp_path / "rbs.sqlite")
    workspace = store.ensure_sample()
    result = get_engine("stub").solve(workspace.instance, options=workspace.instance.solver)

    renamed = store.rename(
        workspace.id,
        "Renamed during solve",
        expected_workspace_revision=workspace.workspace_revision,
    )
    assert renamed.instance_revision == workspace.instance_revision

    with pytest.raises(WorkspaceConflictError, match="changed"):
        store.save_schedule(
            workspace.id,
            result,
            expected_instance_revision=workspace.instance_revision,
            expected_workspace_revision=workspace.workspace_revision,
        )


def test_stale_workspace_commands_are_rejected_consistently(tmp_path) -> None:
    store = Store(tmp_path / "rbs.sqlite")
    stale = store.ensure_sample()
    current = store.rename(
        stale.id,
        "Current name",
        expected_workspace_revision=stale.workspace_revision,
    )

    with pytest.raises(WorkspaceConflictError, match="reload"):
        store.rename(
            stale.id,
            "Lost update",
            expected_workspace_revision=stale.workspace_revision,
        )
    with pytest.raises(WorkspaceConflictError, match="reload"):
        store.delete(
            stale.id,
            expected_workspace_revision=stale.workspace_revision,
        )
    with pytest.raises(WorkspaceConflictError, match="reload"):
        store.mark_exported(
            stale.id,
            expected_workspace_revision=stale.workspace_revision,
        )

    assert store.get(stale.id).name == current.name


def test_internal_catalog_snapshots_are_collected_when_replaced(tmp_path) -> None:
    store = Store(tmp_path / "rbs.sqlite")
    workspace = store.ensure_sample()
    first_rotations = list(workspace.instance.rotations)
    first_rotations[0] = first_rotations[0].model_copy(update={"name": "First revision"})
    first = store.save_instance(
        workspace.id,
        workspace.instance.model_copy(update={"rotations": first_rotations}),
        expected_workspace_revision=workspace.workspace_revision,
    )
    replaced_catalog_id = first.catalog_id
    assert store.get_catalog(replaced_catalog_id).managed

    second_rotations = list(first.instance.rotations)
    second_rotations[0] = second_rotations[0].model_copy(update={"name": "Second revision"})
    second = store.save_instance(
        first.id,
        first.instance.model_copy(update={"rotations": second_rotations}),
        expected_workspace_revision=first.workspace_revision,
    )

    with pytest.raises(KeyError):
        store.get_catalog(replaced_catalog_id)
    assert store.get_catalog(second.catalog_id).managed


def test_user_catalogs_remain_pinned_when_no_workspace_references_them(tmp_path) -> None:
    store = Store(tmp_path / "rbs.sqlite")
    workspace = store.ensure_sample()
    original_catalog_id = workspace.catalog_id
    raw = workspace.instance.constraint_catalog().model_dump(mode="json")
    raw["rotations"][0]["name"] = "Pinned imported constraints"
    imported = store.import_catalog_json("Keep this", json.dumps(raw))
    selected = store.set_catalog(
        workspace.id,
        imported.id,
        expected_workspace_revision=workspace.workspace_revision,
    )
    store.set_catalog(
        selected.id,
        original_catalog_id,
        expected_workspace_revision=selected.workspace_revision,
    )

    assert not store.get_catalog(imported.id).managed


def test_persisted_v1_catalogs_are_rejected_on_load(tmp_path) -> None:
    path = tmp_path / "v1-catalog.sqlite"
    store = Store(path)
    store.init()
    instance = sample_instance()
    legacy = instance.constraint_catalog().model_dump(mode="json")
    legacy["schema_version"] = 1

    with store.connect() as connection:
        catalog_id = connection.execute(
            """
            INSERT INTO catalogs
                (name, schema_version, content_hash, catalog_json, created_at, updated_at)
            VALUES ('Legacy v1', 1, 'legacy-v1-hash', ?, 'now', 'now')
            """,
            (json.dumps(legacy),),
        ).lastrowid
        workspace_id = connection.execute(
            """
            INSERT INTO workspaces
                (name, academic_year, catalog_id, instance_json, schedule_json,
                 instance_revision, schedule_revision, created_at, updated_at)
            VALUES ('Legacy workspace', ?, ?, ?, NULL, 1, NULL, 'now', 'now')
            """,
            (
                instance.academic_year,
                catalog_id,
                dumps(instance.scheduling_case()),
            ),
        ).lastrowid

    store.init()

    with pytest.raises(ValidationError, match="Input should be 5"):
        store.get(workspace_id)
