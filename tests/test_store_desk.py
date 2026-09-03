"""Per-workspace files, download tracking, and the desk cap."""

from __future__ import annotations

import json

import pytest

from rbs.catalog import sample_instance
from rbs.models.rbsc import RBSCState
from rbs.store import DeskFullError, DownloadState, Store, WorkspaceConflictError


def _store(tmp_path, **kwargs) -> Store:
    store = Store(tmp_path / "desk.sqlite", **kwargs)
    store.init()
    return store


def _workspace(store: Store, name: str = "AY 2026-2027"):
    return store.create(name, sample_instance())


# ---- download tracking -------------------------------------------------


def test_a_new_workspace_has_never_been_downloaded(tmp_path) -> None:
    workspace = _workspace(_store(tmp_path))

    assert workspace.download_state is DownloadState.NEVER
    assert workspace.exported_at is None
    assert not workspace.is_sample


def test_save_as_converts_sample_data_without_changing_it_before_success(tmp_path) -> None:
    store = _store(tmp_path)
    workspace = store.create("Sample 2026-2027", sample_instance(), is_sample=True)

    recovery_payload = json.loads(store.export_workspace_rbsc(workspace.id))
    save_as_payload = json.loads(
        store.export_workspace_rbsc(workspace.id, clear_sample=True)
    )

    assert recovery_payload["workspaces"][0]["is_sample"] is True
    assert save_as_payload["workspaces"][0]["is_sample"] is False
    assert store.get(workspace.id).is_sample

    converted = store.mark_exported(
        workspace.id,
        expected_workspace_revision=workspace.workspace_revision,
        clear_sample=True,
    )

    assert not converted.is_sample
    assert converted.download_state is DownloadState.CURRENT


def test_marking_an_export_makes_the_workspace_current(tmp_path) -> None:
    store = _store(tmp_path)
    workspace = _workspace(store)

    marked = store.mark_exported(
        workspace.id,
        expected_workspace_revision=workspace.workspace_revision,
    )

    assert marked.download_state is DownloadState.CURRENT
    assert marked.exported_at is not None
    assert marked.exported_instance_revision == marked.instance_revision


def test_editing_the_instance_makes_the_download_stale(tmp_path) -> None:
    store = _store(tmp_path)
    workspace = _workspace(store)
    marked = store.mark_exported(
        workspace.id,
        expected_workspace_revision=workspace.workspace_revision,
    )

    instance = workspace.instance.model_copy(update={"lock_through_today": True})
    store.save_instance(
        workspace.id,
        instance,
        expected_workspace_revision=marked.workspace_revision,
    )

    assert store.get(workspace.id).download_state is DownloadState.STALE


def test_solving_makes_the_download_stale_without_touching_the_instance(tmp_path) -> None:
    """A solve advances only schedule_revision, so tracking one revision is not enough."""
    store = _store(tmp_path)
    workspace = _workspace(store)
    before = store.mark_exported(
        workspace.id,
        expected_workspace_revision=workspace.workspace_revision,
    )

    schedule = _solved_schedule(store, workspace.id)
    store.save_schedule(
        workspace.id,
        schedule,
        expected_instance_revision=before.instance_revision,
        expected_workspace_revision=before.workspace_revision,
    )

    after = store.get(workspace.id)
    assert after.instance_revision == before.instance_revision
    assert after.schedule_revision != before.schedule_revision
    assert after.download_state is DownloadState.STALE


def _solved_schedule(store: Store, workspace_id: int):
    from rbs.solver.core import get_engine

    workspace = store.get(workspace_id)
    return get_engine("stub").solve(workspace.instance, options=workspace.instance.solver)


# ---- one workspace, one file -------------------------------------------


def test_a_workspace_exports_as_a_single_workspace_document(tmp_path) -> None:
    store = _store(tmp_path)
    first = _workspace(store, "First")
    _workspace(store, "Second")

    payload = json.loads(store.export_workspace_rbsc(first.id))

    assert [item["name"] for item in payload["workspaces"]] == ["First"]
    assert len(payload["catalogs"]) == 1
    assert payload["catalogs"][0]["id"] == first.catalog_id


def test_export_can_be_pinned_to_the_snapshot_the_user_chose(tmp_path) -> None:
    store = _store(tmp_path)
    stale = _workspace(store, "Before")
    store.rename(
        stale.id,
        "After",
        expected_workspace_revision=stale.workspace_revision,
    )

    with pytest.raises(WorkspaceConflictError, match="changed"):
        store.export_workspace_rbsc(
            stale.id,
            expected_workspace_revision=stale.workspace_revision,
        )


def test_importing_a_workspace_adds_to_the_desk_rather_than_replacing_it(tmp_path) -> None:
    source = _store(tmp_path / "a")
    original = _workspace(source, "Imported")
    payload = source.export_workspace_rbsc(original.id)

    target = _store(tmp_path / "b")
    existing = _workspace(target, "Already here")
    imported = target.import_workspace_rbsc(payload)

    assert [item.name for item in imported] == ["Imported"]
    assert {w.name for w in target.list()} == {"Already here", "Imported"}
    assert target.get(existing.id).name == "Already here"


def test_importing_the_same_file_twice_yields_independent_workspaces(tmp_path) -> None:
    source = _store(tmp_path / "a")
    payload = source.export_workspace_rbsc(_workspace(source).id)
    target = _store(tmp_path / "b")

    first = target.import_workspace_rbsc(payload)[0]
    second = target.import_workspace_rbsc(payload)[0]

    assert first.id != second.id
    assert len(target.list()) == 2


def test_an_imported_workspace_starts_out_downloaded(tmp_path) -> None:
    """It is by definition identical to the file the user is holding."""
    source = _store(tmp_path / "a")
    payload = source.export_workspace_rbsc(_workspace(source).id)

    imported = _store(tmp_path / "b").import_workspace_rbsc(payload)[0]

    assert imported.download_state is DownloadState.CURRENT


def test_a_round_trip_preserves_revisions_and_the_schedule(tmp_path) -> None:
    source = _store(tmp_path / "a")
    workspace = _workspace(source)
    exported = source.save_schedule(
        workspace.id,
        _solved_schedule(source, workspace.id),
        expected_instance_revision=workspace.instance_revision,
        expected_workspace_revision=workspace.workspace_revision,
    )

    imported = _store(tmp_path / "b").import_workspace_rbsc(
        source.export_workspace_rbsc(workspace.id)
    )[0]

    assert imported.instance_revision == exported.instance_revision
    assert imported.schedule_revision == exported.schedule_revision
    assert imported.academic_year == exported.academic_year
    assert (imported.schedule is None) == (exported.schedule is None)


def test_import_remaps_catalog_references(tmp_path) -> None:
    """The file's catalog ids mean nothing on the receiving desk."""
    source = _store(tmp_path / "a")
    payload = json.loads(source.export_workspace_rbsc(_workspace(source).id))
    payload["catalogs"][0]["id"] = 9999
    payload["workspaces"][0]["catalog_id"] = 9999

    imported = _store(tmp_path / "b").import_workspace_rbsc(json.dumps(payload))[0]

    assert imported.catalog_id != 9999
    assert imported.instance.rotations


def test_importing_a_file_with_no_workspaces_is_refused(tmp_path) -> None:
    empty = RBSCState(exported_at="2026-03-01T12:00:00+00:00")
    payload = json.dumps(empty.model_dump(mode="json"))

    with pytest.raises(ValueError, match="no workspaces"):
        _store(tmp_path).import_workspace_rbsc(payload)


def test_pre_v6_files_are_rejected(tmp_path) -> None:
    from pydantic import ValidationError

    source = _store(tmp_path / "a")
    payload = json.loads(source.export_workspace_rbsc(_workspace(source).id))
    payload["schema_version"] = 1

    with pytest.raises(ValidationError, match="Input should be 6"):
        _store(tmp_path / "b").import_workspace_rbsc(json.dumps(payload))


# ---- desk cap ----------------------------------------------------------


def test_the_desk_cap_stops_a_desk_becoming_an_archive(tmp_path) -> None:
    store = _store(tmp_path, max_workspaces=2)
    _workspace(store, "One")
    _workspace(store, "Two")

    with pytest.raises(DeskFullError, match="close one"):
        _workspace(store, "Three")


def test_the_desk_cap_applies_to_imports_too(tmp_path) -> None:
    source = _store(tmp_path / "a")
    payload = source.export_workspace_rbsc(_workspace(source).id)
    target = _store(tmp_path / "b", max_workspaces=1)
    _workspace(target, "Only one allowed")

    with pytest.raises(DeskFullError):
        target.import_workspace_rbsc(payload)


def test_closing_a_workspace_frees_a_slot(tmp_path) -> None:
    store = _store(tmp_path, max_workspaces=1)
    workspace = _workspace(store, "One")

    store.delete(
        workspace.id,
        expected_workspace_revision=workspace.workspace_revision,
    )

    assert _workspace(store, "Two").name == "Two"


def test_an_uncapped_desk_is_the_default(tmp_path) -> None:
    store = _store(tmp_path)

    for index in range(5):
        _workspace(store, f"Workspace {index}")

    assert store.workspace_count() == 5


def test_replacing_a_database_cannot_overfill_a_capped_desk(tmp_path) -> None:
    """Replace bypassed the cap entirely before this; it is the one bulk path in."""
    source = _store(tmp_path / "a")
    for name in ("One", "Two", "Three"):
        _workspace(source, name)
    whole = source.export_rbsc()

    with pytest.raises(DeskFullError, match="more than"):
        _store(tmp_path / "b", max_workspaces=2).restore_rbsc(whole)


def test_a_whole_database_file_can_be_opened_rather_than_restored(tmp_path) -> None:
    """Which is why the hosted build needs no replace at all."""
    source = _store(tmp_path / "a")
    for name in ("One", "Two"):
        _workspace(source, name)

    target = _store(tmp_path / "b")
    _workspace(target, "Already here")
    imported = target.import_workspace_rbsc(source.export_rbsc())

    assert [item.name for item in imported] == ["One", "Two"]
    assert {w.name for w in target.list()} == {"Already here", "One", "Two"}
