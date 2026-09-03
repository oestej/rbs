"""The download indicator, the close gate, and what the desktop build omits."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from rbs.catalog import sample_instance
from rbs.store import DownloadState, Store
from rbs.ui.host import EvictionNotice, LocalHost, Principal, SessionStatus
from rbs.ui.session import WorkspaceSession
from rbs.ui.workspaces.status import PILL_ALERT, PILL_MUTED, PILL_OK, PILL_WARN
from rbs.workspaces import WorkspaceController


def _created(before: set[int]) -> list:
    from nicegui import ui

    return [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]


def _labels(created) -> set[str]:
    return {
        str(getattr(element, "_text", "") or "")
        for element in created
        if getattr(element, "_text", None)
    }


def _session(tmp_path) -> tuple[WorkspaceSession, Store]:
    store = Store(tmp_path / "desk.sqlite")
    store.init()
    store.create("AY 2026-2027", sample_instance())
    return WorkspaceSession(store=store, workspace_id=store.list()[0].id), store


def _dirty_session(tmp_path) -> tuple[WorkspaceSession, Store]:
    """A session whose workspace has unsaved edits past its initial revision."""
    session, store = _session(tmp_path)
    workspace = store.list()[0]
    WorkspaceController(store).save_instance(
        workspace,
        workspace.instance.model_copy(update={"lock_through_today": True}),
    )
    return session, store


# ---- the indicator -----------------------------------------------------


def test_a_never_downloaded_workspace_reads_as_such(tmp_path) -> None:
    from rbs.ui.workspaces.status import download_summary

    _session_obj, store = _session(tmp_path)

    label, tone = download_summary(store.list()[0])

    assert label == "Never downloaded"
    assert tone == PILL_ALERT


def test_a_saved_workspace_reads_as_downloaded(tmp_path) -> None:
    from rbs.ui.workspaces.status import download_summary

    _session_obj, store = _session(tmp_path)
    workspace = WorkspaceController(store).mark_exported(store.list()[0])

    label, tone = download_summary(workspace)

    assert label.startswith("Downloaded ")
    assert tone == PILL_OK


def test_an_edited_workspace_reads_as_changed(tmp_path) -> None:
    from rbs.ui.workspaces.status import download_summary

    _session_obj, store = _session(tmp_path)
    workspace = store.list()[0]
    workspace = WorkspaceController(store).mark_exported(workspace)
    WorkspaceController(store).save_instance(
        workspace,
        workspace.instance.model_copy(update={"lock_through_today": True}),
    )

    label, tone = download_summary(store.get(workspace.id))

    assert label == "Changes since download"
    assert tone == PILL_WARN


def test_sample_data_has_one_red_header_pill_and_a_disabled_save(tmp_path) -> None:
    from nicegui import ui

    from rbs.ui.app_documents import _save_button
    from rbs.ui.app_status import _download_chip

    store = Store(tmp_path / "sample.sqlite")
    store.init()
    sample_workspace = store.create(
        "Sample 2026-2027",
        sample_instance(),
        is_sample=True,
    )
    session = WorkspaceSession(store=store, workspace_id=sample_workspace.id)
    before = set(ui.context.client.elements)

    _download_chip(session, sample_workspace)
    _save_button(session, sample_workspace)

    created = _created(before)
    sample_badge = next(
        element
        for element in created
        if element.__class__.__name__ == "Badge"
        and getattr(element, "_text", None) == "Sample Data"
    )
    save = next(
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Save"
    )
    assert "rbs-pill--alert" in sample_badge._classes
    assert sample_badge.visible
    assert save._props.get("disable")
    assert "Save as…" in _labels(created)


def test_browser_save_as_clears_the_sample_flag_after_the_write_succeeds(tmp_path) -> None:
    from types import SimpleNamespace

    from rbs.ui.workspaces.close import save_binding

    store = Store(tmp_path / "sample.sqlite")
    store.init()
    workspace = store.create("Sample 2026-2027", sample_instance(), is_sample=True)
    session = WorkspaceSession(store=store, workspace_id=workspace.id)

    record, javascript = save_binding(session, workspace, force_picker=True)
    assert "save_as=true" in javascript
    assert store.get(workspace.id).is_sample

    record(SimpleNamespace(args="saved:sample-copy.rbsc"))

    converted = store.get(workspace.id)
    assert not converted.is_sample
    assert converted.download_state is DownloadState.CURRENT


def test_relative_ages_read_naturally() -> None:
    from rbs.ui.workspaces.status import _relative_age

    now = datetime.now(UTC)
    assert _relative_age((now - timedelta(seconds=5)).isoformat()) == "just now"
    assert _relative_age((now - timedelta(minutes=12)).isoformat()) == "12 min ago"
    assert _relative_age((now - timedelta(hours=1)).isoformat()) == "1 hour ago"
    assert _relative_age((now - timedelta(days=3)).isoformat()) == "3 days ago"
    assert _relative_age("not a timestamp") == "recently"


# ---- the close gate ----------------------------------------------------


def test_closing_an_unsaved_workspace_delays_its_close_button(tmp_path) -> None:
    from nicegui import ui

    from rbs.ui.workspaces.close import CLOSE_CONFIRM_DELAY_SECONDS, open_close_dialog

    session, store = _dirty_session(tmp_path)
    workspace = store.list()[0]
    assert workspace.has_unsaved_changes
    closed: list[int] = []
    before = set(ui.context.client.elements)

    open_close_dialog(session, workspace, closed.append)

    created = _created(before)
    assert not [
        element for element in created if element.__class__.__name__ == "Input"
    ], "closing no longer asks for a typed workspace name"
    close_button = _close_button(created)
    assert f"({CLOSE_CONFIRM_DELAY_SECONDS})" in str(close_button._props.get("label"))
    assert close_button._props.get("disable")
    assert any("discards all of it permanently" in text for text in _labels(created))
    assert closed == []


def test_closing_a_saved_workspace_closes_without_a_dialog(tmp_path) -> None:
    from nicegui import ui

    from rbs.ui.workspaces.close import open_close_dialog

    session, store = _session(tmp_path)
    WorkspaceController(store).mark_exported(store.list()[0])
    workspace = store.list()[0]
    closed: list[int] = []
    before = set(ui.context.client.elements)

    open_close_dialog(session, workspace, closed.append)

    assert closed == [workspace.id]
    assert not [
        element
        for element in _created(before)
        if element.__class__.__name__ == "Dialog"
    ]


def test_closing_a_fresh_workspace_closes_without_a_dialog(tmp_path) -> None:
    from nicegui import ui

    from rbs.ui.workspaces.close import close_workspace, open_close_dialog

    session, store = _session(tmp_path)
    workspace = store.list()[0]
    assert not workspace.has_unsaved_changes
    closed: list[int] = []
    before = set(ui.context.client.elements)

    open_close_dialog(session, workspace, closed.append)

    assert closed == [workspace.id]
    assert not [
        element
        for element in _created(before)
        if element.__class__.__name__ == "Dialog"
    ]

    close_workspace(session, store.get(workspace.id))

    assert store.list() == []
    assert session.workspace_id is None
    assert not [
        element
        for element in _created(before)
        if element.__class__.__name__ in {"Dialog", "Input"}
    ]


def test_an_unsaved_close_offers_to_save_first(tmp_path) -> None:
    from nicegui import ui

    from rbs.ui.workspaces.close import open_close_dialog

    session, store = _dirty_session(tmp_path)
    before = set(ui.context.client.elements)

    open_close_dialog(session, store.list()[0], lambda _id: None)

    buttons = {
        element._props.get("label")
        for element in _created(before)
        if element.__class__.__name__ == "Button"
    }
    assert "Save and close" in buttons
    assert any(
        isinstance(label, str) and label.startswith("Close workspace") for label in buttons
    )


def test_closing_sample_data_does_not_open_a_save_prompt(tmp_path) -> None:
    from nicegui import ui

    from rbs.ui.workspaces.close import close_workspace

    store = Store(tmp_path / "sample.sqlite")
    store.init()
    sample_workspace = store.create(
        "Sample 2026-2027",
        sample_instance(),
        is_sample=True,
    )
    session = WorkspaceSession(store=store, workspace_id=sample_workspace.id)
    before = set(ui.context.client.elements)

    close_workspace(session, sample_workspace)

    assert store.list() == []
    assert session.workspace_id is None
    assert not [
        element
        for element in _created(before)
        if element.__class__.__name__ in {"Dialog", "Input"}
    ]


def test_the_close_button_arms_after_its_countdown(tmp_path) -> None:
    from nicegui import ui

    from rbs.ui.workspaces.close import CLOSE_CONFIRM_DELAY_SECONDS, open_close_dialog

    session, store = _dirty_session(tmp_path)
    workspace = store.list()[0]
    closed: list[int] = []
    before = set(ui.context.client.elements)
    open_close_dialog(session, workspace, closed.append)
    created = _created(before)
    close_button = _close_button(created)
    timer = next(e for e in created if e.__class__.__name__ == "Timer")

    _click(close_button)
    assert closed == []

    for _ in range(CLOSE_CONFIRM_DELAY_SECONDS - 1):
        timer.callback()
        _click(close_button)
        assert closed == []

    timer.callback()
    assert close_button._props.get("label") == "Close workspace"
    assert not close_button._props.get("disable")
    _click(close_button)
    assert closed == [workspace.id]


def _close_button(created):
    return next(
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and str(element._props.get("label") or "").startswith("Close workspace")
    )


def _click(button) -> None:
    from nicegui.events import ClickEventArguments

    for listener in button._event_listeners.values():
        if listener.type == "click":
            listener.handler(ClickEventArguments(sender=button, client=button.client))


# ---- what the desktop build does not show ------------------------------


def test_the_desktop_host_reports_no_retention(tmp_path) -> None:
    store = Store(tmp_path / "desk.sqlite")
    store.init()
    host = LocalHost(store)
    principal = host.principal(None)

    assert host.session_status(principal) is None
    assert host.eviction_notice(principal) is None


def test_the_retention_banner_is_absent_without_a_window(tmp_path) -> None:
    from nicegui import ui

    from rbs.ui.app_status import _retention_banner

    session, _store = _session(tmp_path)
    before = set(ui.context.client.elements)

    _retention_banner(session)

    assert _created(before) == []


def test_a_status_only_warns_inside_its_window() -> None:
    now = datetime.now(UTC)
    status = SessionStatus(
        last_mutation_at=now,
        expires_at=now + timedelta(days=90),
        workspace_count=2,
        warn_within=timedelta(days=7),
    )

    assert not status.should_warn(now)
    assert status.should_warn(now + timedelta(days=85))
    assert status.remaining(now + timedelta(days=999)) == timedelta(0)


def test_an_eviction_notice_names_what_was_lost(tmp_path) -> None:
    from nicegui import ui

    from rbs.ui.app_status import _eviction_banner

    session, _store = _session(tmp_path)

    class _Host(LocalHost):
        def eviction_notice(self, principal: Principal) -> EvictionNotice | None:
            return EvictionNotice(datetime(2026, 3, 12, tzinfo=UTC), 3)

    session.workspace_host = _Host(session.store)
    before = set(ui.context.client.elements)

    _eviction_banner(session)

    text = " ".join(_labels(_created(before)))
    assert "3 workspaces" in text
    assert "12 March 2026" in text


def test_a_workspace_marked_downloaded_reports_current(tmp_path) -> None:
    _session_obj, store = _session(tmp_path)

    assert (
        WorkspaceController(store).mark_exported(store.list()[0]).download_state
        is DownloadState.CURRENT
    )


def test_the_file_tab_lists_the_desk_with_its_save_state(tmp_path) -> None:
    from nicegui import ui

    from rbs.ui.workspaces.io import _workspace_tab

    session, store = _session(tmp_path)
    store.create("Second year", sample_instance())
    before = set(ui.context.client.elements)

    _workspace_tab(store, store.get(session.workspace_id), session, lambda: None)

    created = _created(before)
    text = _labels(created)
    badges = {
        str(element._props.get("label") or getattr(element, "_text", ""))
        for element in created
        if element.__class__.__name__ == "Badge"
    }
    # The tab is already about the open workspace, so the list is the others.
    assert "Other open workspaces" in text
    assert "This workspace" in text
    assert "Second year" in text
    assert "Never downloaded" in badges
    assert any("permanently deletes this server's copy" in item for item in text)


def test_the_desk_is_omitted_when_there_are_no_other_workspaces(tmp_path) -> None:
    from nicegui import ui

    from rbs.ui.workspaces.io import _workspace_tab

    session, store = _session(tmp_path)
    workspace = store.list()[0]
    before = set(ui.context.client.elements)

    _workspace_tab(store, workspace, session, lambda: None)

    assert "Other open workspaces" not in _labels(_created(before))


def _tab_contents(store, session):
    from nicegui import ui

    from rbs.ui.workspaces.io import _workspace_tab

    before = set(ui.context.client.elements)
    _workspace_tab(store, store.get(session.workspace_id), session, lambda: None)
    created = _created(before)
    return (
        _labels(created),
        {e._props.get("label") for e in created if e.__class__.__name__ == "Button"},
        {e._props.get("label") for e in created if e.__class__.__name__ == "Upload"},
    )


def test_the_desktop_build_keeps_whole_database_restore(tmp_path) -> None:
    """Your own machine, your own file, and no close gate to be inconsistent with."""
    session, store = _session(tmp_path)

    text, buttons, uploads = _tab_contents(store, session)

    assert "Whole database" in text
    assert "Download whole database" in buttons
    assert "Replace database" in uploads


def test_the_hosted_build_offers_no_way_to_replace_everything(tmp_path) -> None:
    """Replace would be the only path that destroys unsaved work in bulk."""
    session, store = _session(tmp_path)

    class _HostedHost(LocalHost):
        allows_database_restore = False

    session.workspace_host = _HostedHost(store)

    text, buttons, uploads = _tab_contents(store, session)

    assert "Replace database" not in uploads
    assert "Whole database" not in text
    assert "All workspaces" in text
    assert "Download all workspaces" in buttons
    # Opening a file still covers migration, including a whole-database export.
    assert "Open workspace file" in uploads


def test_the_two_packagings_declare_opposite_answers(tmp_path) -> None:
    from rbs.cloud.config import CloudConfig
    from rbs.cloud.control import ControlPlane
    from rbs.cloud.host import CloudHost
    from rbs.cloud.sessions import SessionRegistry
    from rbs.cloud.solve_pool import SolvePool
    from rbs.product import CLOUD_PRODUCT, LOCAL_PRODUCT

    config = CloudConfig(
        cf_team_domain="acme.cloudflareaccess.com",
        cf_audience="aud",
        storage_secret="secret",
        bootstrap_subjects=("subject-a",),
        control_db=tmp_path / "control.sqlite",
        data_dir=tmp_path / "data",
    )
    control = ControlPlane(config.control_db, config)
    control.init()
    cloud = CloudHost(config, control, SessionRegistry(control, config), None, SolvePool(config))

    local = LocalHost(Store(tmp_path / "local.sqlite"))

    assert local.product is LOCAL_PRODUCT
    assert cloud.product is CLOUD_PRODUCT
    assert local.allows_database_restore is True
    assert cloud.allows_database_restore is False


def test_a_never_downloaded_workspace_has_no_header_warning(tmp_path) -> None:
    """A workspace is usable before its first export, so the header stays quiet."""
    from nicegui import ui

    from rbs.ui.app_status import _download_chip, _refresh_download_chip

    session, store = _session(tmp_path)
    before = set(ui.context.client.elements)

    _download_chip(session, store.list()[0])

    badge = next(e for e in _created(before) if e.__class__.__name__ == "Badge")
    classes = " ".join(badge._classes)
    assert "rbs-pill" in classes
    assert "rbs-pill--alert" in classes
    assert badge._props.get("text-color") is None
    assert badge.visible is False
    _refresh_download_chip(session)
    assert badge.visible is False
    assert "hidden" in badge._classes
    # No Quasar bg-* class, so the pill CSS is what paints it.
    assert badge._props.get("color") is None


def test_leave_warning_only_tracks_changes_made_after_page_open(tmp_path) -> None:
    from rbs.ui.app_status import _should_warn_before_leave

    session, store = _session(tmp_path)
    controller = WorkspaceController(store)
    workspace = store.list()[0]

    # A never-downloaded workspace is not itself evidence that this page made
    # a change, so merely opening and leaving it should stay quiet.
    assert not _should_warn_before_leave(session, workspace, True)
    assert not _should_warn_before_leave(session, workspace, True)

    changed = controller.save_instance(
        workspace,
        workspace.instance.model_copy(update={"lock_through_today": True}),
    )
    assert _should_warn_before_leave(session, changed, True)

    downloaded = controller.mark_exported(changed)
    assert not _should_warn_before_leave(session, downloaded, False)

    changed_again = controller.save_instance(
        downloaded,
        downloaded.instance.model_copy(update={"lock_through_today": False}),
    )
    assert _should_warn_before_leave(session, changed_again, True)

    # Reloading establishes the already-persisted server revision as the new
    # baseline instead of repeating the warning forever.
    reloaded = WorkspaceSession(store=store, workspace_id=changed_again.id)
    assert not _should_warn_before_leave(reloaded, changed_again, True)

    session.reset_navigation(changed_again.id)
    assert session._leave_guard_baseline is None


def test_download_status_refresh_updates_the_leave_guard(tmp_path, monkeypatch) -> None:
    from rbs.ui import app as ui_app

    session, store = _session(tmp_path)
    controller = WorkspaceController(store)
    warned: list[bool] = []
    monkeypatch.setattr(
        ui_app.file_handle,
        "set_unsaved",
        lambda _ui, unsaved: warned.append(unsaved),
    )

    from rbs.ui.app_status import _download_chip

    workspace = store.list()[0]
    _download_chip(session, workspace)
    assert warned == [False]

    changed = controller.save_instance(
        workspace,
        workspace.instance.model_copy(update={"lock_through_today": True}),
    )
    from rbs.ui.app_status import _refresh_download_chip

    _refresh_download_chip(session)
    assert warned[-1] is True

    controller.mark_exported(changed)
    _refresh_download_chip(session)
    assert warned[-1] is False


def test_every_pill_tone_is_defined_with_its_own_contrast(tmp_path) -> None:
    """A tone with no CSS rule renders as unstyled text on the primary header."""
    from pathlib import Path as _Path

    from rbs.ui.workspaces.status import DOWNLOAD_LABELS, PILL_MUTED, PILL_WARN, pill_classes

    css = (_Path("src/rbs/ui/static/app.css")).read_text(encoding="utf-8")
    tones = {tone for _label, tone in DOWNLOAD_LABELS.values()} | {PILL_WARN, PILL_MUTED}

    for tone in tones:
        rule = f".rbs-pill--{tone}"
        assert rule in css, f"{tone} has no CSS rule"
        assert "color:" in css.split(rule, 1)[1][:200]
    assert pill_classes(PILL_WARN) == "rbs-pill rbs-pill--warn"


def test_the_save_menu_carries_the_workspace_not_the_tab_strip(tmp_path) -> None:
    """The other tabs are views into the schedule; this is about the file itself."""
    from nicegui import ui

    from rbs.ui.app_documents import _save_button
    from rbs.ui.app_shell import _workspace_navigation
    from rbs.ui.session import TAB_NAMES

    session, store = _session(tmp_path)
    before = set(ui.context.client.elements)

    _save_button(session, store.list()[0])
    _workspace_navigation()

    created = _created(before)
    # A menu item keeps its label on a child ItemSection, not on itself.
    menu_labels = _labels(created)
    tab_labels = {
        e._props.get("label") for e in created if e.__class__.__name__ == "Tab"
    }

    assert {"New", "Save as…"} <= menu_labels
    assert not {"About", "Workspace Settings"} & menu_labels
    uploads = [e for e in created if e.__class__.__name__ == "Upload"]
    assert any(upload._props.get("label") == "Open" for upload in uploads)
    assert "Workspace" not in tab_labels
    assert "Configuration" in tab_labels
    assert "Settings" not in tab_labels
    assert "workspace" not in TAB_NAMES


def test_native_document_packaging_uses_open_and_save_language(tmp_path) -> None:
    from pathlib import Path

    from nicegui import ui

    from rbs.ui.app_documents import _save_button, document_summary

    class _Documents:
        path = Path("/tmp/academic-year.rbsc")
        dirty = True

        async def open(self):
            return None

        async def save(self):
            return self.path

        async def save_as(self):
            return self.path

    session, store = _session(tmp_path)
    documents = _Documents()
    session.workspace_host = LocalHost(store, document_io=documents)
    before = set(ui.context.client.elements)

    _save_button(session, store.list()[0])

    created = _created(before)
    labels = _labels(created)
    button_labels = {
        element._props.get("label")
        for element in created
        if element.__class__.__name__ == "Button"
    }
    assert {"New", "Open", "Save as…"} <= labels
    assert "Settings" in button_labels
    assert "Workspace Settings" not in labels
    assert document_summary(documents) == ("Changes to save", PILL_WARN)
    documents.dirty = False
    assert document_summary(documents) == ("Saved", PILL_OK)


def test_native_close_guards_unsaved_changes(tmp_path) -> None:
    from pathlib import Path

    from nicegui import ui

    from rbs.ui.workspaces.native_documents import close_native_document

    session, store = _session(tmp_path)

    class _Documents:
        application_name = "RBS Desktop"
        path = Path("/tmp/academic-year.rbsc")
        workspace = store.get(session.workspace_id)
        dirty = True
        closed = False

        def close(self):
            self.closed = True
            self.workspace = None

    documents = _Documents()
    session.workspace_host = LocalHost(store, document_io=documents)
    container = ui.column()
    before = set(ui.context.client.elements)

    async def close_from_ui() -> None:
        with container:
            await close_native_document(session)

    asyncio.run(close_from_ui())

    buttons = {
        element._props.get("label")
        for element in _created(before)
        if element.__class__.__name__ == "Button"
    }
    assert {"Save and close", "Close without saving"} <= buttons
    assert documents.closed is False
    assert session.workspace_id is not None


def test_native_clean_close_enters_the_empty_workspace_state(tmp_path) -> None:
    from pathlib import Path

    from nicegui import ui

    from rbs.ui.workspaces.native_documents import close_native_document

    session, store = _session(tmp_path)

    class _Documents:
        application_name = "RBS Desktop"
        path = Path("/tmp/academic-year.rbsc")
        workspace = store.get(session.workspace_id)
        dirty = False

        def close(self):
            self.workspace = None
            self.path = None

    documents = _Documents()
    session.workspace_host = LocalHost(store, document_io=documents)
    container = ui.column()

    async def close_from_ui() -> None:
        with container:
            await close_native_document(session)

    asyncio.run(close_from_ui())

    assert documents.workspace is None
    assert session.workspace_id is None


def test_native_sample_close_skips_the_unsaved_changes_prompt(tmp_path) -> None:
    from nicegui import ui

    from rbs.ui.workspaces.native_documents import close_native_document

    store = Store(tmp_path / "sample.sqlite")
    store.init()
    sample_workspace = store.create(
        "Sample 2026-2027",
        sample_instance(),
        is_sample=True,
    )
    session = WorkspaceSession(store=store, workspace_id=sample_workspace.id)

    class _Documents:
        application_name = "RBS Desktop"
        path = None
        workspace = store.get(sample_workspace.id)
        dirty = True
        closed = False

        def close(self):
            self.closed = True
            self.workspace = None

    documents = _Documents()
    session.workspace_host = LocalHost(store, document_io=documents)
    container = ui.column()
    before = set(ui.context.client.elements)

    async def close_from_ui() -> None:
        with container:
            await close_native_document(session)

    asyncio.run(close_from_ui())

    assert documents.closed
    assert session.workspace_id is None
    assert "Save and close" not in _labels(_created(before))


def test_native_workspace_panel_omits_browser_and_multi_workspace_tools(tmp_path) -> None:
    from pathlib import Path

    from nicegui import ui

    from rbs.ui.workspaces.io import _workspace_tab

    class _Documents:
        path = Path("/tmp/academic-year.rbsc")
        dirty = False

    session, store = _session(tmp_path)
    session.workspace_host = LocalHost(store, document_io=_Documents())
    before = set(ui.context.client.elements)

    _workspace_tab(store, store.get(session.workspace_id), session, lambda: None)

    created = _created(before)
    labels = _labels(created)
    uploads = {e._props.get("label") for e in created if e.__class__.__name__ == "Upload"}
    buttons = {e._props.get("label") for e in created if e.__class__.__name__ == "Button"}
    assert "Desktop document" in labels
    assert "New…" in buttons
    assert str(_Documents.path) in labels
    assert not uploads
    assert "New workspace" not in buttons
    assert "Close workspace" not in buttons


def test_the_workspace_dialog_only_edits_the_current_workspace(tmp_path) -> None:
    from nicegui import ui

    from rbs.ui.app_documents import open_workspace_dialog

    session, store = _session(tmp_path)
    store.create("Second year", sample_instance())
    before = set(ui.context.client.elements)

    open_workspace_dialog(session, store.get(session.workspace_id))

    created = _created(before)
    text = _labels(created)
    buttons = {e._props.get("label") for e in created if e.__class__.__name__ == "Button"}
    uploads = {e._props.get("label") for e in created if e.__class__.__name__ == "Upload"}
    tabs = [
        e._props.get("label") for e in created if e.__class__.__name__ == "Tab"
    ]

    assert "This workspace" not in text
    assert tabs == ["General", "Colors"]
    assert "Institutional color scheme" in text
    assert "Save color scheme" in buttons
    assert "Other open workspaces" not in text
    assert "Apply changes" in buttons
    assert not {"Save workspace", "New workspace", "Close workspace"} & buttons
    assert "Whole database" not in text
    assert "All workspaces" not in text
    assert not uploads
    assert session.workspace_dialog is not None


def test_closing_a_workspace_dismisses_the_dialog_it_was_opened_from(tmp_path) -> None:
    """The dialog belongs to a page that closing remounts, so it must not outlive it."""
    from rbs.ui.app_documents import open_workspace_dialog
    from rbs.ui.workspaces.close import close_workspace

    session, store = _session(tmp_path)
    workspace = store.get(session.workspace_id)
    WorkspaceController(store).mark_exported(
        workspace
    )  # clean, so closing needs no dialog
    open_workspace_dialog(session, workspace)
    dialog = session.workspace_dialog

    close_workspace(session, store.get(workspace.id))

    assert session.workspace_dialog is None
    assert dialog.value is False


def _solved(store, workspace_id):
    from rbs.solver.core import get_engine

    workspace = store.get(workspace_id)
    return get_engine("stub").solve(workspace.instance, options=workspace.instance.solver)


def test_an_unsolved_workspace_says_so(tmp_path) -> None:
    from rbs.ui.app_status import solve_summary

    _session_obj, store = _session(tmp_path)

    assert solve_summary(store.list()[0]) == ("Not solved", PILL_MUTED)


def test_a_schedule_left_behind_by_an_edit_reads_as_out_of_date(tmp_path) -> None:
    from rbs.ui.app_status import solve_summary

    _session_obj, store = _session(tmp_path)
    workspace = store.list()[0]
    workspace = WorkspaceController(store).save_schedule(
        workspace,
        _solved(store, workspace.id),
    )
    WorkspaceController(store).save_instance(
        workspace,
        workspace.instance.model_copy(update={"lock_through_today": True}),
    )

    refreshed = store.get(workspace.id)
    assert refreshed.solution_is_out_of_date
    assert solve_summary(refreshed) == ("Solver out of date", PILL_WARN)


def test_an_incomplete_schedule_asks_for_a_solve(tmp_path) -> None:
    """The stub engine leaves every week open, which is exactly "needs solve"."""
    from rbs.ui.app_status import solve_summary

    _session_obj, store = _session(tmp_path)
    workspace = store.list()[0]
    WorkspaceController(store).save_schedule(
        workspace,
        _solved(store, workspace.id),
    )

    summary = solve_summary(store.get(workspace.id))
    assert summary == ("Needs solve", PILL_WARN)


def test_the_header_groups_workspace_file_actions_with_the_selector(tmp_path) -> None:
    """File actions stay with the selector; schedule state stays by Solve."""
    from nicegui import ui

    from rbs.ui.app_shell import _mount_shell

    session, store = _session(tmp_path)
    session.header = ui.header()
    session.body = ui.column()
    session._render_tab = lambda *_a, **_k: None
    before = set(ui.context.client.elements)

    _mount_shell(session)

    created = _created(before)
    controls = next(
        e for e in created if "rbs-workspace-controls" in " ".join(getattr(e, "_classes", []))
    )
    actions = next(
        e for e in created if "rbs-header-actions" in " ".join(getattr(e, "_classes", []))
    )
    primary = next(
        e for e in created if "rbs-header-primary" in " ".join(getattr(e, "_classes", []))
    )
    secondary = next(
        e for e in created if "rbs-header-secondary" in " ".join(getattr(e, "_classes", []))
    )

    def descendants(root):
        nodes = []
        stack = list(root.default_slot.children)
        while stack:
            node = stack.pop()
            nodes.append(node)
            for slot in node.slots.values():
                stack.extend(slot.children)
        return nodes

    control_nodes = descendants(controls)
    action_nodes = descendants(actions)
    control_types = {node.__class__.__name__ for node in control_nodes}
    action_types = {node.__class__.__name__ for node in action_nodes}

    primary_children = list(primary.default_slot.children)
    brand = next(
        node
        for node in primary_children
        if "rbs-header-brand" in " ".join(getattr(node, "_classes", []))
    )
    assert brand.__class__.__name__ == "Button"
    assert brand._props["aria-label"] == "About RBS"
    assert controls in primary_children
    secondary_children = list(secondary.default_slot.children)
    assert secondary_children.index(session.navigation.tabs) < secondary_children.index(actions)
    top_level = list(session.header.default_slot.children)
    assert top_level.index(primary) < top_level.index(secondary)

    assert "Select" in control_types
    assert {
        node._props.get("label")
        for node in control_nodes
        if node.__class__.__name__ == "Button"
    } >= {"Settings", "Save", "Close"}
    assert "Badge" in action_types
    assert "Tabs" not in control_types | action_types
    assert not any(node.__class__.__name__ == "Badge" for node in control_nodes)
    more = next(
        node
        for node in control_nodes
        if node.__class__.__name__ == "Button"
        and node._props.get("icon") == "more_vert"
    )
    assert more._props["aria-label"] == "More workspace actions"
    assert not any(node.__class__.__name__ == "Tooltip" for node in descendants(more))
    solve = next(
        node
        for node in action_nodes
        if node.__class__.__name__ == "Button"
        and node._props.get("label") == "Solve"
    )
    assert solve._props["no-caps"] is True


def test_an_empty_header_only_offers_new_and_open(tmp_path) -> None:
    from nicegui import ui

    from rbs.store import Store
    from rbs.ui.app_shell import _mount_shell
    from rbs.ui.session import WorkspaceSession

    store = Store(tmp_path / "empty.sqlite")
    store.init()
    session = WorkspaceSession(store=store)
    session.header = ui.header()
    session.body = ui.column()
    before = set(ui.context.client.elements)

    _mount_shell(session)

    created = _created(before)
    buttons = {
        element._props.get("label")
        for element in created
        if element.__class__.__name__ == "Button"
    }
    uploads = [element for element in created if element.__class__.__name__ == "Upload"]
    assert "New" in buttons
    assert any(upload._props.get("label") == "Open" for upload in uploads)
    assert not {"Save", "Close", "Solve"} & buttons
    assert not any(element.__class__.__name__ in {"Select", "Tabs"} for element in created)
    assert not any(
        "rbs-header-secondary" in " ".join(getattr(element, "_classes", []))
        for element in created
    )
    assert session.navigation is None


def test_desktop_header_uses_the_desktop_product_name(tmp_path) -> None:
    from nicegui import ui

    from rbs.store import Store
    from rbs.ui.app_shell import _mount_shell
    from rbs.ui.session import WorkspaceSession

    store = Store(tmp_path / "desktop-empty.sqlite")
    store.init()
    session = WorkspaceSession(store=store)
    session.workspace_host = LocalHost(store, document_io=object())
    session.header = ui.header()
    session.body = ui.column()
    before = set(ui.context.client.elements)

    _mount_shell(session)

    created = _created(before)
    labels = _labels(created)
    brand = next(
        element
        for element in created
        if "rbs-header-brand" in " ".join(getattr(element, "_classes", []))
    )
    assert "RBS Desktop" in labels
    assert brand._props["aria-label"] == "About RBS Desktop"


def test_the_header_actions_keep_state_and_solve_on_the_right(tmp_path) -> None:
    """Status pills precede Solve, and the group has no workspace menu."""
    from nicegui import ui

    from rbs.ui.app_shell import _mount_shell

    session, _store = _session(tmp_path)
    session.header = ui.header()
    session.body = ui.column()
    session._render_tab = lambda *_a, **_k: None
    before = set(ui.context.client.elements)

    _mount_shell(session)

    created = _created(before)
    actions = next(
        e for e in created if "rbs-header-actions" in " ".join(getattr(e, "_classes", []))
    )
    descendants = set()
    action_nodes = []
    stack = list(actions.default_slot.children)
    while stack:
        node = stack.pop()
        action_nodes.append(node)
        descendants.add(node.__class__.__name__)
        for slot in node.slots.values():
            stack.extend(slot.children)

    assert "Badge" in descendants
    assert "Button" in descendants
    tabs_in_actions = "Tabs" in descendants
    assert not tabs_in_actions, "navigation belongs in the centre, not the action group"
    assert not any(node._props.get("icon") == "more_vert" for node in action_nodes)
    solve = next(
        node
        for node in action_nodes
        if node.__class__.__name__ == "Button"
        and node._props.get("label") == "Solve"
    )
    assert solve._props["no-caps"] is True
    direct_children = list(actions.default_slot.children)
    assert direct_children[-1] is solve
    badges = [node for node in direct_children if node.__class__.__name__ == "Badge"]
    assert badges
    assert all(direct_children.index(badge) < direct_children.index(solve) for badge in badges)
