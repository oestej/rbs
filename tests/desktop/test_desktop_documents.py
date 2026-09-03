"""One-file lifecycle for the native desktop packaging."""

from __future__ import annotations

import asyncio
import json
import threading
import unicodedata
from pathlib import Path

import pytest

from rbs.catalog import sample_instance
from rbs.desktop.documents import (
    DesktopDocumentController,
    DocumentCardinalityError,
    ExternalDocumentChangeError,
    NoDocumentError,
    SampleWorkspaceSaveError,
    _document_lock_path,
)
from rbs.desktop.recovery import allocate_recovery_path
from rbs.desktop.settings import DesktopSettingsFile
from rbs.models.instance import SchedulerInput
from rbs.store import DownloadState, Store
from rbs.ui.host import LocalHost
from rbs.ui.session import WorkspaceSession
from rbs.workspaces import WorkspaceController


class Dialogs:
    def __init__(
        self,
        *,
        open_path: Path | None = None,
        save_path: Path | None = None,
        settings_open_path: Path | None = None,
        settings_save_path: Path | None = None,
    ) -> None:
        self.open_path = open_path
        self.save_path = save_path
        self.settings_open_path = settings_open_path
        self.settings_save_path = settings_save_path
        self.suggested_names: list[str] = []
        self.settings_suggested_names: list[str] = []

    async def choose_open_path(self) -> Path | None:
        return self.open_path

    async def choose_save_path(self, suggested_name: str) -> Path | None:
        self.suggested_names.append(suggested_name)
        return self.save_path

    async def choose_settings_open_path(self) -> Path | None:
        return self.settings_open_path

    async def choose_settings_save_path(self, suggested_name: str) -> Path | None:
        self.settings_suggested_names.append(suggested_name)
        return self.settings_save_path


def _store(path: Path, *names: str) -> Store:
    store = Store(path)
    store.init()
    for name in names:
        store.create(name, sample_instance())
    return store


def _document(path: Path, name: str = "Portable year") -> Path:
    source = _store(path.with_suffix(".sqlite"), name)
    path.write_text(source.export_workspace_rbsc(source.list()[0].id), encoding="utf-8")
    return path


def test_load_replaces_the_ephemeral_store_and_marks_the_document_clean(tmp_path) -> None:
    current = _store(tmp_path / "current.sqlite", "Discarded")
    source = _document(tmp_path / "loaded.rbsc", "Loaded")
    controller = DesktopDocumentController(current, Dialogs())

    loaded = controller.load(source)

    assert loaded.name == "Loaded"
    assert [workspace.name for workspace in current.list()] == ["Loaded"]
    assert current.current_id() == loaded.id
    assert current.get(loaded.id).download_state is DownloadState.CURRENT
    assert controller.path == source
    assert not controller.dirty


def test_invalid_or_multi_workspace_load_does_not_replace_the_open_document(tmp_path) -> None:
    current = _store(tmp_path / "current.sqlite", "Keep me")
    controller = DesktopDocumentController(current, Dialogs())
    invalid = tmp_path / "invalid.rbsc"
    invalid.write_text("not json", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        controller.load(invalid)
    assert [workspace.name for workspace in current.list()] == ["Keep me"]

    source = _store(tmp_path / "many.sqlite", "One", "Two")
    many = tmp_path / "many.rbsc"
    many.write_text(source.export_rbsc(), encoding="utf-8")

    with pytest.raises(DocumentCardinalityError, match="exactly one workspace; found 2"):
        controller.load(many)
    assert [workspace.name for workspace in current.list()] == ["Keep me"]


def test_load_rejects_a_non_rbsc_path_before_replacing_the_document(tmp_path) -> None:
    store = _store(tmp_path / "current.sqlite", "Keep me")
    controller = DesktopDocumentController(store, Dialogs())
    wrong_extension = tmp_path / "workspace.json"
    wrong_extension.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match=".rbsc extension"):
        controller.load(wrong_extension)
    assert controller.workspace.name == "Keep me"


def test_open_uses_the_native_dialog_and_cancel_changes_nothing(tmp_path) -> None:
    store = _store(tmp_path / "desktop.sqlite", "Current")
    source = _document(tmp_path / "selected.rbsc", "Selected")
    dialogs = Dialogs(open_path=source)
    controller = DesktopDocumentController(store, dialogs)

    assert asyncio.run(controller.open()).name == "Selected"
    dialogs.open_path = None
    assert asyncio.run(controller.open()) is None
    assert controller.workspace.name == "Selected"


def test_close_clears_the_ephemeral_document_without_touching_its_file(tmp_path) -> None:
    store = _store(tmp_path / "desktop.sqlite", "Current")
    destination = tmp_path / "schedule.rbsc"
    controller = DesktopDocumentController(store, Dialogs(save_path=destination))
    asyncio.run(controller.save())
    saved = destination.read_text(encoding="utf-8")

    controller.close()

    assert controller.workspace is None
    assert controller.path is None
    assert not controller.dirty
    assert store.current_id() is None
    assert destination.read_text(encoding="utf-8") == saved


def test_save_as_uses_a_native_dialog_appends_rbsc_and_rebinds(tmp_path) -> None:
    store = _store(tmp_path / "desktop.sqlite", "Academic Year")
    dialogs = Dialogs(save_path=tmp_path / "chosen-name")
    controller = DesktopDocumentController(store, dialogs)

    saved = asyncio.run(controller.save_as())

    assert saved == tmp_path / "chosen-name.rbsc"
    assert saved.exists()
    assert controller.path == saved
    assert not controller.dirty
    assert dialogs.suggested_names == ["Academic-Year-2026-2027.rbsc"]
    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert [workspace["name"] for workspace in payload["workspaces"]] == ["Academic Year"]


def test_save_overwrites_the_bound_path_without_showing_another_dialog(tmp_path) -> None:
    store = _store(tmp_path / "desktop.sqlite", "Before")
    destination = tmp_path / "schedule.rbsc"
    dialogs = Dialogs(save_path=destination)
    controller = DesktopDocumentController(store, dialogs)
    asyncio.run(controller.save())
    WorkspaceController(store).rename(controller.workspace, "After")

    saved = asyncio.run(controller.save())

    assert saved == destination
    assert dialogs.suggested_names == ["Before-2026-2027.rbsc"]
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["workspaces"][0]["name"] == "After"
    assert not controller.dirty


def test_save_refuses_to_overwrite_a_file_changed_outside_the_app(tmp_path) -> None:
    store = _store(tmp_path / "desktop.sqlite", "Local draft")
    destination = tmp_path / "schedule.rbsc"
    controller = DesktopDocumentController(store, Dialogs(save_path=destination))
    asyncio.run(controller.save())
    WorkspaceController(store).rename(controller.workspace, "Local changes")
    destination.write_text("external changes", encoding="utf-8")

    with pytest.raises(ExternalDocumentChangeError, match="changed outside"):
        asyncio.run(controller.save())

    assert destination.read_text(encoding="utf-8") == "external changes"
    assert controller.dirty


def test_two_desktop_instances_cannot_both_overwrite_the_same_version(tmp_path) -> None:
    destination = _document(tmp_path / "shared.rbsc", "Original")
    first_store = _store(tmp_path / "first.sqlite")
    second_store = _store(tmp_path / "second.sqlite")
    lock_directory = tmp_path / "locks"
    first = DesktopDocumentController(
        first_store,
        Dialogs(),
        lock_directory=lock_directory,
    )
    second = DesktopDocumentController(
        second_store,
        Dialogs(),
        lock_directory=lock_directory,
    )
    first.load(destination)
    second.load(destination)
    WorkspaceController(first_store).rename(first.workspace, "First edit")
    WorkspaceController(second_store).rename(second.workspace, "Second edit")
    start = threading.Barrier(3)
    outcomes: list[Path | BaseException] = []

    def save(controller: DesktopDocumentController) -> None:
        start.wait()
        try:
            outcomes.append(asyncio.run(controller.save()))
        except BaseException as exc:
            outcomes.append(exc)

    threads = [
        threading.Thread(target=save, args=(first,)),
        threading.Thread(target=save, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert sum(isinstance(outcome, Path) for outcome in outcomes) == 1
    assert sum(
        isinstance(outcome, ExternalDocumentChangeError) for outcome in outcomes
    ) == 1
    saved_name = json.loads(destination.read_text(encoding="utf-8"))["workspaces"][0][
        "name"
    ]
    assert saved_name in {"First edit", "Second edit"}


def test_macos_lock_identity_normalizes_path_case_and_unicode(tmp_path) -> None:
    lock_directory = tmp_path / "locks"
    first = tmp_path / "RÉSUMÉ.RBSC"
    second = tmp_path / unicodedata.normalize("NFD", "résumé.rbsc")

    assert _document_lock_path(
        first,
        lock_directory,
        platform="darwin",
    ) == _document_lock_path(second, lock_directory, platform="darwin")


def test_cancelled_save_as_does_not_bind_or_mark_the_document_clean(tmp_path) -> None:
    store = _store(tmp_path / "desktop.sqlite", "Unsaved")
    controller = DesktopDocumentController(store, Dialogs(save_path=None))

    assert controller.dirty
    assert asyncio.run(controller.save_as()) is None
    assert controller.path is None
    assert controller.dirty


def test_failed_save_as_does_not_rebind_or_clear_dirty_state(tmp_path) -> None:
    store = _store(tmp_path / "desktop.sqlite", "Unsaved")

    def fail(_path: Path, _payload: str) -> None:
        raise OSError("disk full")

    controller = DesktopDocumentController(
        store,
        Dialogs(save_path=tmp_path / "failed.rbsc"),
        atomic_writer=fail,
    )

    with pytest.raises(OSError, match="disk full"):
        asyncio.run(controller.save_as())
    assert controller.path is None
    assert controller.dirty


def test_semantic_dirty_tracking_catches_rename_and_ignores_export_metadata(tmp_path) -> None:
    store = _store(tmp_path / "desktop.sqlite", "Original")
    controller = DesktopDocumentController(
        store,
        Dialogs(save_path=tmp_path / "original.rbsc"),
    )
    asyncio.run(controller.save())
    workspace_id = controller.workspace.id

    WorkspaceController(store).mark_exported(store.get(workspace_id))
    assert not controller.dirty

    WorkspaceController(store).rename(store.get(workspace_id), "Renamed")
    assert controller.dirty


def test_application_setting_edits_persist_without_dirtying_the_document(tmp_path) -> None:
    from rbs.solver.core import get_engine

    store = _store(tmp_path / "desktop.sqlite", "Original")
    initial = store.list()[0]
    store.save_schedule(
        initial.id,
        get_engine("stub").solve(initial.instance, options=initial.instance.solver),
        expected_instance_revision=initial.instance_revision,
        expected_workspace_revision=initial.workspace_revision,
    )
    settings_path = tmp_path / "application" / "settings.json"
    controller = DesktopDocumentController(
        store,
        Dialogs(save_path=tmp_path / "original.rbsc"),
        application_settings=DesktopSettingsFile(settings_path),
    )
    asyncio.run(controller.save())
    workspace = controller.workspace
    raw = workspace.instance.model_dump(mode="json")
    raw["solver"]["num_workers"] = 3
    raw["lock_through_today"] = True
    raw["color_scheme"]["name"] = "Local palette"
    raw["rotations"][0]["color"] = "#123456"
    revised = SchedulerInput.model_validate(raw)

    host = LocalHost(store, document_io=controller)
    session = WorkspaceSession(
        store,
        workspace_host=host,
        principal=host.principal(None),
        workspace_id=workspace.id,
    )
    session.persist_instance(
        revised,
        preserve_schedule=workspace.schedule is not None,
    )

    assert not controller.dirty
    assert controller.workspace.schedule is not None
    persisted = json.loads(settings_path.read_text(encoding="utf-8"))
    assert persisted["solver"]["num_workers"] == 3
    assert persisted["lock_through_today"] is True
    assert persisted["colors"]["scheme"]["name"] == "Local palette"
    assert persisted["colors"]["rotations"][revised.rotations[0].id] == "#123456"


def test_save_settings_exports_current_preferences_without_rebinding_document(
    tmp_path,
) -> None:
    store = _store(tmp_path / "desktop.sqlite", "Current")
    settings_path = tmp_path / "application" / "settings.json"
    dialogs = Dialogs(
        save_path=tmp_path / "current.rbsc",
        settings_save_path=tmp_path / "portable-settings",
    )
    controller = DesktopDocumentController(
        store,
        dialogs,
        application_settings=DesktopSettingsFile(settings_path),
    )
    asyncio.run(controller.save())
    raw = controller.workspace.instance.model_dump(mode="json")
    raw["solver"]["num_workers"] = 3
    raw["color_scheme"]["name"] = "Portable palette"
    revised = SchedulerInput.model_validate(raw)
    WorkspaceController(store).save_instance(controller.workspace, revised)

    destination = asyncio.run(controller.save_settings())

    assert destination == tmp_path / "portable-settings.json"
    assert dialogs.settings_suggested_names == ["settings.json"]
    assert controller.path == tmp_path / "current.rbsc"
    assert not controller.dirty
    exported = json.loads(destination.read_text(encoding="utf-8"))
    assert exported["solver"]["num_workers"] == 3
    assert exported["colors"]["scheme"]["name"] == "Portable palette"


def test_load_settings_installs_and_applies_preferences_without_dirtying_rbsc(
    tmp_path,
) -> None:
    from rbs.solver.core import get_engine

    store = _store(tmp_path / "desktop.sqlite", "Current")
    initial = store.list()[0]
    store.save_schedule(
        initial.id,
        get_engine("stub").solve(initial.instance, options=initial.instance.solver),
        expected_instance_revision=initial.instance_revision,
        expected_workspace_revision=initial.workspace_revision,
    )
    imported_path = tmp_path / "imported.json"
    imported_file = DesktopSettingsFile(imported_path)
    raw = sample_instance().model_dump(mode="json")
    raw["solver"]["num_workers"] = 3
    raw["lock_through_today"] = True
    raw["color_scheme"]["name"] = "Imported palette"
    raw["rotations"][0]["color"] = "#123456"
    assert imported_file.capture(SchedulerInput.model_validate(raw))

    settings_path = tmp_path / "application" / "settings.json"
    dialogs = Dialogs(
        save_path=tmp_path / "current.rbsc",
        settings_open_path=imported_path,
    )
    controller = DesktopDocumentController(
        store,
        dialogs,
        application_settings=DesktopSettingsFile(settings_path),
    )
    asyncio.run(controller.save())

    loaded = asyncio.run(controller.load_settings())

    assert loaded is not None
    assert loaded.instance.solver.num_workers == 3
    assert loaded.instance.lock_through_today
    assert loaded.instance.color_scheme.name == "Imported palette"
    assert loaded.instance.rotations[0].color == "#123456"
    assert loaded.schedule is not None
    assert controller.path == tmp_path / "current.rbsc"
    assert not controller.dirty
    installed = json.loads(settings_path.read_text(encoding="utf-8"))
    assert installed == json.loads(imported_path.read_text(encoding="utf-8"))


def test_invalid_or_cancelled_settings_import_leaves_application_unchanged(
    tmp_path,
) -> None:
    store = _store(tmp_path / "desktop.sqlite", "Current")
    settings_path = tmp_path / "application" / "settings.json"
    invalid = tmp_path / "invalid.json"
    invalid.write_text("not json", encoding="utf-8")
    dialogs = Dialogs(settings_open_path=invalid)
    controller = DesktopDocumentController(
        store,
        dialogs,
        application_settings=DesktopSettingsFile(settings_path),
    )
    before_file = settings_path.read_text(encoding="utf-8")
    before_instance = controller.workspace.instance

    with pytest.raises(ValueError, match="settings file could not be read"):
        asyncio.run(controller.load_settings())
    assert controller.workspace.instance == before_instance
    assert settings_path.read_text(encoding="utf-8") == before_file

    dialogs.settings_open_path = None
    assert asyncio.run(controller.load_settings()) is None
    assert controller.workspace.instance == before_instance


def test_open_rejects_pre_v6_documents_with_a_clear_error(tmp_path) -> None:
    from pydantic import ValidationError

    source_store = _store(tmp_path / "source.sqlite")
    legacy = source_store.create("Legacy", sample_instance())
    payload = json.loads(source_store.export_workspace_rbsc(legacy.id))
    payload["schema_version"] = 3
    document = tmp_path / "legacy.rbsc"
    document.write_text(json.dumps(payload), encoding="utf-8")

    target = _store(tmp_path / "target.sqlite")
    controller = DesktopDocumentController(target, Dialogs())

    with pytest.raises(ValidationError, match="Input should be 6"):
        controller.load(document)


def test_atomic_write_failure_preserves_the_existing_file(tmp_path, monkeypatch) -> None:
    import rbs.desktop.documents as documents

    store = _store(tmp_path / "desktop.sqlite", "New content")
    destination = tmp_path / "existing.rbsc"
    destination.write_text("old content", encoding="utf-8")
    controller = DesktopDocumentController(store, Dialogs(save_path=destination))

    def fail_replace(_source, _destination) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(documents.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        asyncio.run(controller.save_as())
    assert destination.read_text(encoding="utf-8") == "old content"
    assert list(tmp_path.glob(".existing.rbsc.*.tmp")) == []
    assert controller.path is None


def test_save_without_a_workspace_is_rejected(tmp_path) -> None:
    store = _store(tmp_path / "empty.sqlite")
    controller = DesktopDocumentController(store, Dialogs(save_path=tmp_path / "x.rbsc"))

    with pytest.raises(NoDocumentError, match="no RBS document"):
        asyncio.run(controller.save())


def test_new_document_replaces_the_workspace_and_advances_generation(tmp_path) -> None:
    store = _store(tmp_path / "desktop.sqlite", "Old document")
    controller = DesktopDocumentController(
        store,
        Dialogs(save_path=tmp_path / "old.rbsc"),
    )
    asyncio.run(controller.save())
    generation = controller.generation

    workspace = controller.new()

    assert workspace.name == "Untitled"
    assert workspace.instance.residents == []
    assert workspace.instance.rotations == []
    assert [item.id for item in store.list()] == [workspace.id]
    assert controller.path is None
    assert controller.dirty
    assert controller.generation == generation + 1


def test_new_sample_document_opens_the_bundled_demonstration(tmp_path) -> None:
    store = _store(tmp_path / "desktop.sqlite", "Old document")
    controller = DesktopDocumentController(store, Dialogs())

    workspace = controller.new(sample=True)

    assert workspace.name.startswith("Sample ")
    assert workspace.instance.residents
    assert workspace.instance.rotations
    assert workspace.is_sample
    assert not controller.dirty


def test_sample_document_requires_save_as_and_becomes_a_regular_document(tmp_path) -> None:
    store = _store(tmp_path / "desktop.sqlite", "Old document")
    destination = tmp_path / "sample-copy.rbsc"
    dialogs = Dialogs(save_path=destination)
    controller = DesktopDocumentController(store, dialogs)
    controller.new(sample=True)

    with pytest.raises(SampleWorkspaceSaveError, match="Save As"):
        asyncio.run(controller.save())

    assert dialogs.suggested_names == []
    assert controller.workspace.is_sample

    assert asyncio.run(controller.save_as()) == destination
    assert not controller.workspace.is_sample
    assert not controller.dirty
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["workspaces"][0]["is_sample"] is False


def test_open_document_is_checkpointed_and_cleared_on_orderly_shutdown(
    tmp_path,
) -> None:
    store = _store(tmp_path / "desktop.sqlite", "Before")
    recovery = allocate_recovery_path(
        tmp_path / "recovery",
        pid=123,
        token="a" * 32,
    )
    destination = tmp_path / "saved.rbsc"
    controller = DesktopDocumentController(
        store,
        Dialogs(save_path=destination),
        recovery_path=recovery,
    )

    controller.mark_new()
    assert recovery.is_file()
    assert recovery.read_bytes().startswith(b"SQLite format 3\x00")
    assert not Path(f"{recovery}-wal").exists()
    assert not Path(f"{recovery}-shm").exists()
    assert Store(recovery).list()[0].name == "Before"

    WorkspaceController(store).rename(controller.workspace, "After")
    assert Store(recovery).list()[0].name == "After"

    asyncio.run(controller.save())
    assert destination.is_file()
    assert not controller.dirty
    assert recovery.is_file()
    assert Store(recovery).list()[0].name == "After"

    assert controller.clear_recovery_checkpoint()
    assert not recovery.exists()


def test_clean_opened_document_is_checkpointed_for_crash_recovery(tmp_path) -> None:
    source = _document(tmp_path / "opened.rbsc", "Open during crash")
    store = _store(tmp_path / "desktop.sqlite")
    recovery = allocate_recovery_path(
        tmp_path / "recovery",
        pid=123,
        token="9" * 32,
    )
    controller = DesktopDocumentController(
        store,
        Dialogs(),
        recovery_path=recovery,
    )

    controller.load(source)

    assert not controller.dirty
    assert Store(recovery).list()[0].name == "Open during crash"


def test_sample_data_is_checkpointed_while_it_is_open(tmp_path) -> None:
    store = _store(tmp_path / "desktop.sqlite")
    recovery = allocate_recovery_path(
        tmp_path / "recovery",
        pid=123,
        token="8" * 32,
    )
    controller = DesktopDocumentController(
        store,
        Dialogs(),
        recovery_path=recovery,
    )

    controller.new(sample=True)

    assert not controller.dirty
    assert Store(recovery).list()[0].is_sample


def test_failed_checkpoint_preserves_the_previous_recoverable_draft(
    tmp_path, monkeypatch
) -> None:
    import rbs.desktop.documents as documents

    store = _store(tmp_path / "desktop.sqlite", "Recoverable")
    recovery = allocate_recovery_path(
        tmp_path / "recovery",
        pid=123,
        token="b" * 32,
    )
    controller = DesktopDocumentController(
        store,
        Dialogs(),
        recovery_path=recovery,
    )
    controller.mark_new()

    def fail_replace(_source, _destination) -> None:
        raise OSError("recovery volume full")

    monkeypatch.setattr(documents.os, "replace", fail_replace)
    WorkspaceController(store).rename(controller.workspace, "Newer edit")

    assert controller.recovery_error == "recovery volume full"
    assert Store(recovery).list()[0].name == "Recoverable"


def test_stale_recovery_is_adopted_as_an_untitled_dirty_document(tmp_path) -> None:
    source = _store(tmp_path / "source.sqlite", "Recovered work")
    stale = allocate_recovery_path(
        tmp_path / "stale-recovery",
        pid=123,
        token="d" * 32,
    )
    DesktopDocumentController(
        source,
        Dialogs(),
        recovery_path=stale,
    ).mark_new()
    target = _store(tmp_path / "target.sqlite")
    recovery = allocate_recovery_path(
        tmp_path / "recovery",
        pid=456,
        token="c" * 32,
    )
    controller = DesktopDocumentController(
        target,
        Dialogs(),
        recovery_path=recovery,
    )

    workspace = controller.restore_recovery(stale)

    assert workspace.name == "Recovered work"
    assert controller.path is None
    assert controller.dirty
    assert controller.recovered_from == stale
    assert recovery.is_file()
    assert not stale.exists()


def test_legacy_rbsc_recovery_draft_is_still_adopted(tmp_path) -> None:
    stale = _document(tmp_path / "stale.rbsc", "Legacy recovered work")
    target = _store(tmp_path / "target.sqlite")
    recovery = allocate_recovery_path(
        tmp_path / "recovery",
        pid=456,
        token="1" * 32,
    )
    controller = DesktopDocumentController(
        target,
        Dialogs(),
        recovery_path=recovery,
    )

    workspace = controller.restore_recovery(stale)

    assert workspace.name == "Legacy recovered work"
    assert controller.path is None
    assert controller.dirty
    assert recovery.read_bytes().startswith(b"SQLite format 3\x00")
    assert not stale.exists()


def test_recovery_source_is_retained_without_a_replacement_checkpoint(tmp_path) -> None:
    source = _store(tmp_path / "source.sqlite", "Only copy")
    stale = allocate_recovery_path(
        tmp_path / "stale-recovery",
        pid=123,
        token="e" * 32,
    )
    DesktopDocumentController(
        source,
        Dialogs(),
        recovery_path=stale,
    ).mark_new()
    target = _store(tmp_path / "target.sqlite")
    controller = DesktopDocumentController(target, Dialogs(), recovery_path=None)

    controller.restore_recovery(stale)

    assert stale.is_file()
    assert controller.path is None
    assert controller.dirty


def test_recovery_can_checkpoint_back_to_the_same_draft_path(tmp_path) -> None:
    source = _store(tmp_path / "source.sqlite", "Same-path copy")
    stale = allocate_recovery_path(
        tmp_path / "stale-recovery",
        pid=123,
        token="f" * 32,
    )
    DesktopDocumentController(
        source,
        Dialogs(),
        recovery_path=stale,
    ).mark_new()
    target = _store(tmp_path / "target.sqlite")
    controller = DesktopDocumentController(target, Dialogs(), recovery_path=stale)

    controller.restore_recovery(stale)

    assert stale.is_file()
    assert Store(stale).list()[0].name == "Same-path copy"
    assert controller.path is None
    assert controller.dirty
