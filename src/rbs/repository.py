"""Application-facing persistence contract for scheduling workspaces.

The shared UI depends on this protocol, not on SQLite. The local application
uses :class:`rbs.store.Store`; the hosted application uses a stable per-subject
proxy that may resolve to a different Store after retention eviction.
"""

from __future__ import annotations

from typing import Protocol

from rbs.models.instance import SchedulerInput
from rbs.models.rbsc import RBSCState
from rbs.models.schedule import Schedule
from rbs.models.workspace import Workspace


class WorkspaceRepository(Protocol):
    """Persistence operations needed by the workspace application."""

    def ensure_sample(self) -> Workspace: ...

    def workspace_count(self) -> int: ...

    def list(self) -> list[Workspace]: ...

    def get(self, workspace_id: int) -> Workspace: ...

    def current_id(self) -> int | None: ...

    def set_current(self, workspace_id: int | None) -> None: ...

    def create(
        self,
        name: str,
        instance: SchedulerInput,
        schedule: Schedule | None = None,
        *,
        is_sample: bool = False,
    ) -> Workspace: ...

    def save_instance(
        self,
        workspace_id: int,
        instance: SchedulerInput,
        *,
        expected_workspace_revision: int,
        preserve_schedule: bool = False,
    ) -> Workspace: ...

    def save_schedule(
        self,
        workspace_id: int,
        schedule: Schedule | None,
        *,
        expected_instance_revision: int,
        expected_workspace_revision: int,
    ) -> Workspace: ...

    def rename(
        self,
        workspace_id: int,
        name: str,
        *,
        expected_workspace_revision: int,
    ) -> Workspace: ...

    def delete(
        self,
        workspace_id: int,
        *,
        expected_workspace_revision: int,
    ) -> None: ...

    def mark_exported(
        self,
        workspace_id: int,
        *,
        expected_workspace_revision: int,
        clear_sample: bool = False,
    ) -> Workspace: ...

    def export_workspace_rbsc(
        self,
        workspace_id: int,
        *,
        expected_workspace_revision: int | None = None,
        clear_sample: bool = False,
    ) -> str: ...

    def export_rbsc(self) -> str: ...

    def inspect_rbsc(self, payload: str) -> RBSCState: ...

    def import_workspace_rbsc(self, payload: str) -> list[Workspace]: ...

    def restore_rbsc(self, payload: str) -> RBSCState: ...
