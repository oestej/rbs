import asyncio
from types import SimpleNamespace

import pytest

from rbs.ui.deferred_sections import DeferredSections, defer_sections


def _idle(loader: DeferredSections, generation: int | None = None) -> None:
    asyncio.run(loader._preload_next(SimpleNamespace(
        args=loader._props["generation"] if generation is None else generation,
    )))


@pytest.fixture
def section_page():
    from nicegui import ui

    root = ui.column()
    loaded = []
    editors = {}
    selections = []
    current = [True]
    active = [True]
    with root:
        with ui.tabs() as tabs:
            for name in ("first", "second", "third"):
                ui.tab(name)
        with ui.tab_panels(tabs, value="first"):
            panels = {name: ui.tab_panel(name) for name in ("first", "second", "third")}

        def render(name):
            loaded.append(name)
            editors[name] = ui.input("Draft", value=name)

        loader = defer_sections(
            tabs,
            panels,
            render,
            active_section="first",
            on_section_change=lambda event: selections.append(event.value),
            can_preload=lambda: current[0],
            is_active=lambda: active[0],
        )
    yield SimpleNamespace(
        root=root, tabs=tabs, panels=panels, loader=loader, loaded=loaded,
        editors=editors, selections=selections, current=current, active=active,
    )
    if not root.is_deleted:
        root.delete()


def test_idle_preloading_builds_one_section_without_changing_selection_or_drafts(section_page):
    page = section_page
    assert page.loaded == ["first"]
    first = page.editors["first"]
    first.set_value("Unfinished edit")

    _idle(page.loader)

    assert page.loaded == ["first", "second"]
    assert page.tabs.value == "first"
    assert page.selections == []
    assert page.editors["first"] is first
    assert first.value == "Unfinished edit"
    assert not first.is_deleted

    _idle(page.loader)
    assert page.loaded == ["first", "second", "third"]
    assert not page.loader._props["pending"]
    _idle(page.loader)
    page.tabs.set_value("second")
    page.tabs.set_value("first")
    assert page.loaded == ["first", "second", "third"]
    assert page.selections == ["second", "first"]
    assert first.value == "Unfinished edit"


def test_click_overtakes_an_in_flight_idle_request_without_duplicate_rendering(section_page):
    page = section_page
    generation = page.loader._props["generation"]

    async def navigate():
        task = asyncio.create_task(page.loader._preload_next(SimpleNamespace(args=generation)))
        await asyncio.sleep(0)
        page.tabs.set_value("third")
        await task

    asyncio.run(navigate())
    assert page.loaded == ["first", "third"]
    assert page.tabs.value == "third"
    _idle(page.loader)
    assert page.loaded == ["first", "third", "second"]
    assert page.tabs.value == "third"


def test_invalidated_page_cancels_pending_work(section_page):
    page = section_page
    page.current[0] = False
    _idle(page.loader)
    page.current[0] = True
    _idle(page.loader)
    assert page.loaded == ["first"]
    assert not page.loader._props["pending"]


def test_page_switch_overtaking_an_idle_request_pauses_until_return(section_page):
    page = section_page
    page.active[0] = False
    _idle(page.loader)
    assert page.loaded == ["first"]
    assert page.loader._props["pending"]
    page.active[0] = True
    _idle(page.loader)
    assert page.loaded == ["first", "second"]


def test_removing_page_ignores_late_idle_requests(section_page):
    page = section_page
    page.root.delete()
    _idle(page.loader)
    assert page.loaded == ["first"]


def test_failed_preload_leaves_current_editor_usable_and_can_retry_on_selection(section_page):
    from nicegui import ui

    page = section_page
    original = page.loader._render

    def broken(name):
        ui.label("Partial editor")
        raise ValueError("render failed")

    page.loader._render = broken
    _idle(page.loader)
    assert not page.loader._props["pending"]
    assert page.panels["second"].default_slot.children == []
    assert not page.editors["first"].is_deleted
    page.loader._render = original
    page.tabs.set_value("second")
    assert page.loaded == ["first", "second"]


@pytest.mark.parametrize("tab", ["rotations", "settings", "clinic_schedule"])
@pytest.mark.parametrize("invalidate", [None, "edit", "switch_workspace"])
def test_workspace_changes_cancel_hidden_section_preloads(tmp_path, tab, invalidate):
    from nicegui import ui

    from rbs.catalog import sample_instance
    from rbs.store import Store
    from rbs.ui.app_shell import _render_tab
    from rbs.ui.session import WorkspaceSession

    store = Store(tmp_path / "preloading.sqlite")
    store.init()
    workspace = store.create("Preloading", sample_instance())
    session = WorkspaceSession(store=store, workspace_id=workspace.id, active_tab=tab)
    session._render_tab = _render_tab
    root = ui.column()
    try:
        session.panels[tab] = root
        before = set(root.client.elements)
        session.refresh_panel(tab)
        loader = next(
            element for key, element in root.client.elements.items()
            if key not in before and isinstance(element, DeferredSections)
        )
        if invalidate == "edit":
            session.mark_stale()
        elif invalidate == "switch_workspace":
            session.reset_navigation(None)
        created = set(root.client.elements)
        if invalidate is None:
            while loader._props["pending"]:
                _idle(loader)
            assert set(root.client.elements) > created
        else:
            _idle(loader)
            assert set(root.client.elements) == created
        assert not loader._props["pending"]
        assert store.get(workspace.id).workspace_revision == workspace.workspace_revision
    finally:
        root.delete()


@pytest.mark.parametrize("tab", ["settings", "clinic_schedule"])
def test_saving_replaces_the_page_before_a_queued_preload_can_use_old_data(tmp_path, tab):
    from nicegui import ui

    from rbs.catalog import sample_instance
    from rbs.store import Store
    from rbs.ui.app_shell import _render_tab
    from rbs.ui.session import WorkspaceSession

    store = Store(tmp_path / "edit.sqlite")
    store.init()
    workspace = store.create("Preloading", sample_instance())
    session = WorkspaceSession(store=store, workspace_id=workspace.id, active_tab=tab)
    session._render_tab = _render_tab
    root = ui.column()
    try:
        session.panels[tab] = root
        before = set(root.client.elements)
        session.refresh_visible()
        loader = next(
            element for key, element in root.client.elements.items()
            if key not in before and isinstance(element, DeferredSections)
        )

        async def save_before_preload():
            task = asyncio.create_task(loader._preload_next(SimpleNamespace(
                args=loader._props["generation"],
            )))
            await asyncio.sleep(0)
            saved = session.persist_instance(
                workspace,
                workspace.instance.revised(use_placeholder_electives=True),
            )
            await task
            return saved

        saved = asyncio.run(save_before_preload())
        assert loader.is_deleted
        replacement = next(
            element for key, element in root.client.elements.items()
            if key not in before and isinstance(element, DeferredSections)
        )
        assert replacement is not loader
        while replacement._props["pending"]:
            _idle(replacement)
        current = store.get(workspace.id)
        assert current.instance.use_placeholder_electives
        assert current.workspace_revision == saved.workspace_revision
        assert current.workspace_revision > workspace.workspace_revision
    finally:
        root.delete()
