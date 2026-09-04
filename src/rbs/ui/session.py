"""Workspace UI session: navigation state and tab-local refresh."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

from rbs.catalog import current_blank_instance, current_sample_instance
from rbs.models.color_scheme import ColorScheme
from rbs.models.instance import SchedulerInput
from rbs.models.schedule import Schedule
from rbs.models.workspace import Workspace
from rbs.product import ProductConfig
from rbs.repository import WorkspaceRepository
from rbs.ui.host import LocalHost, Principal, WorkspaceHost
from rbs.ui.locks import refresh_locks_through_today
from rbs.workspaces import WorkspaceController

TAB_NAMES = (
    "block_schedule",
    "clinic_schedule",
    "residents",
    "rotations",
    "clinic",
    "settings",
)


@runtime_checkable
class UiElement(Protocol):
    """The NiceGUI element surface a workspace session holds onto.

    Sessions only need ``clear``/``classes`` plus context-manager mounting;
    anything richer (``props``, ``client``, tab handles) lives behind the
    concrete element in the rendering modules, not the session state.
    """

    def clear(self) -> None: ...

    def classes(self, *args: object, **kwargs: object) -> UiElement: ...

    def __enter__(self) -> UiElement: ...

    def __exit__(self, *args: object) -> object: ...


RenderTab = Callable[["WorkspaceSession", str], None]
MountShell = Callable[["WorkspaceSession"], None]
RefreshStatus = Callable[["WorkspaceSession"], None]


@dataclass
class WorkspaceSession:
    """Mutable UI session for one browser page.

    Chrome (header tabs and tab panels) is mounted once per workspace identity.
    Saving an instance or finishing a solve refreshes the visible tab instead of
    tearing down the page.
    """

    store: WorkspaceRepository
    workspace_host: WorkspaceHost | None = field(default=None, repr=False)
    principal: Principal | None = field(default=None, repr=False)
    workspace_id: int | None = None
    resident_id: str | None = None
    rotation_id: str | None = None
    show_past_block_weeks: bool = False
    show_past_clinic_weeks: bool = False
    clinic_site: str = "all"
    clinic_section: str = "clinic_sites"
    rotation_section: str = "rotation_summary"
    settings_section: str = "settings_general"
    resident_block_schedule_editing: bool = False
    resident_schedule_editing: bool = False
    resident_schedule_section: str = "resident_block_schedule"
    active_tab: str = "block_schedule"
    solving: bool = False
    theme: UiElement | None = field(default=None, repr=False)
    chrome_scheme: ColorScheme | None = field(default=None, repr=False)
    header: UiElement | None = field(default=None, repr=False)
    body: UiElement | None = field(default=None, repr=False)
    navigation: UiElement | None = field(default=None, repr=False)
    download_chip: UiElement | None = field(default=None, repr=False)
    workspace_dialog: UiElement | None = field(default=None, repr=False)
    solve_chip: UiElement | None = field(default=None, repr=False)
    panels: dict[str, UiElement] = field(default_factory=dict, repr=False)
    stale_panels: set[str] = field(default_factory=set, repr=False)
    _render_tab: RenderTab | None = field(default=None, repr=False)
    _mount: MountShell | None = field(default=None, repr=False)
    _refresh_status: RefreshStatus | None = field(default=None, repr=False)
    _recovery_error: str | None = field(default=None, repr=False)
    _leave_guard_baseline: tuple[int, int] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # A session built without a host is the desktop case: one database, one
        # assumed caller. Filling it in here keeps every call site downstream
        # able to treat both fields as present.
        if self.workspace_host is None:
            self.workspace_host = LocalHost(self.store)
        if self.principal is None:
            self.principal = self.workspace_host.principal(None)

    @property
    def product(self) -> ProductConfig:
        """Product decisions inherited from this session's packaging host."""
        assert self.workspace_host is not None
        return self.workspace_host.product

    def workspace(self) -> Workspace | None:
        if self.workspace_id is None:
            return None
        try:
            workspace = self.store.get(self.workspace_id)
        except KeyError:
            return None
        if workspace.instance.lock_through_today and workspace.schedule is not None:
            refreshed = refresh_locks_through_today(
                workspace.instance,
                workspace.schedule,
                date.today(),
            )
            if refreshed != workspace.instance:
                workspace = WorkspaceController(self.store).save_instance(
                    workspace,
                    refreshed,
                    preserve_schedule=workspace.schedule is not None,
                )
        return workspace

    def reset_navigation(self, workspace_id: int | None) -> None:
        self.workspace_id = workspace_id
        self._leave_guard_baseline = None
        self.resident_id = None
        self.rotation_id = None
        self.show_past_block_weeks = False
        self.show_past_clinic_weeks = False
        self.clinic_site = "all"
        self.clinic_section = "clinic_sites"
        self.rotation_section = "rotation_summary"
        self.settings_section = "settings_general"
        self.resident_block_schedule_editing = False
        self.resident_schedule_editing = False
        self.resident_schedule_section = "resident_block_schedule"
        self.active_tab = "block_schedule"
        self.solving = False

    def persist_instance(
        self,
        instance: SchedulerInput,
        *,
        preserve_schedule: bool = False,
    ) -> None:
        if self.workspace_id is None:
            return
        workspace = self.workspace()
        if workspace is None:
            return
        saved = WorkspaceController(self.store).save_instance(
            workspace,
            instance,
            preserve_schedule=preserve_schedule,
        )
        documents = getattr(self.workspace_host, "document_io", None)
        sync_settings = getattr(documents, "sync_application_settings", None)
        if sync_settings is not None:
            sync_settings(saved.instance)
        self.touch()
        self.mark_stale()
        self.refresh_visible()

    def persist_schedule(self, schedule: Schedule, *, refresh: bool = True) -> None:
        workspace = self.workspace()
        if workspace is None:
            return
        WorkspaceController(self.store).save_schedule(workspace, schedule)
        self.touch()
        self.mark_stale()
        if refresh:
            self.refresh_visible()

    def touch(self) -> None:
        """Record that this caller changed something.

        Mutation accounting happens at the repository commit boundary. This
        method only refreshes status chrome after the command completes.
        """
        if self._refresh_status is not None:
            self._refresh_status(self)

    def mark_stale(self, *tabs: str) -> None:
        self.stale_panels.update(tabs or TAB_NAMES)

    def refresh_visible(self) -> None:
        self.refresh_panel(self.active_tab)

    def refresh_panel(self, name: str) -> None:
        panel = self.panels.get(name)
        if panel is None or self._render_tab is None:
            self.stale_panels.discard(name)
            return
        panel.clear()
        with panel:
            self._render_tab(self, name)
        self.stale_panels.discard(name)

    def on_tab_change(self, value) -> None:
        value = getattr(value, "value", value)
        name = getattr(value, "name", value)
        if name not in TAB_NAMES:
            return
        self.active_tab = name
        if name in self.stale_panels:
            self.refresh_panel(name)

    def rebuild(self) -> None:
        """Remount chrome after a workspace identity change."""
        if self._mount is None:
            return
        self.panels.clear()
        self.navigation = None
        self.stale_panels.clear()
        self._mount(self)

    def create_sample(self) -> None:
        instance = current_sample_instance()
        workspace = self.store.create(
            f"Sample {instance.academic_year}",
            instance,
            is_sample=True,
        )
        self.reset_navigation(workspace.id)
        self.rebuild()

    def create_blank(self) -> None:
        instance = current_blank_instance()
        workspace = self.store.create("Untitled", instance)
        self.reset_navigation(workspace.id)
        self.rebuild()

    def delete_current(self) -> None:
        workspace = self.workspace()
        if workspace is None:
            return
        WorkspaceController(self.store).delete(workspace)
        remaining = self.store.list()
        self.reset_navigation(remaining[0].id if remaining else None)
        self.rebuild()

    def switch_workspace(self, workspace_id: int) -> None:
        if workspace_id == self.workspace_id:
            return
        self.reset_navigation(workspace_id)
        self.store.set_current(workspace_id)
        self.rebuild()
