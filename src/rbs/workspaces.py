"""Workspace commands with optimistic-concurrency policy in one place."""

from __future__ import annotations

from dataclasses import dataclass

from rbs.models.instance import SchedulerInput
from rbs.models.schedule import Schedule
from rbs.models.workspace import Workspace
from rbs.repository import WorkspaceRepository


@dataclass(frozen=True, slots=True)
class WorkspaceController:
    """Apply commands against the exact workspace snapshot the caller saw."""

    repository: WorkspaceRepository

    def save_instance(
        self,
        workspace: Workspace,
        instance: SchedulerInput,
        *,
        preserve_schedule: bool = False,
    ) -> Workspace:
        return self.repository.save_instance(
            workspace.id,
            instance,
            expected_workspace_revision=workspace.workspace_revision,
            preserve_schedule=preserve_schedule,
        )

    def save_schedule(self, workspace: Workspace, schedule: Schedule | None) -> Workspace:
        return self.repository.save_schedule(
            workspace.id,
            schedule,
            expected_instance_revision=workspace.instance_revision,
            expected_workspace_revision=workspace.workspace_revision,
        )

    def rename(self, workspace: Workspace, name: str) -> Workspace:
        return self.repository.rename(
            workspace.id,
            name,
            expected_workspace_revision=workspace.workspace_revision,
        )

    def rename_live(self, workspace_id: int, name: str) -> Workspace:
        """Rename against the revision stored right now, not a held snapshot.

        Renaming only writes the name column, so it cannot clobber scheduling
        data. Reading the revision at call time keeps a stale dialog — or a
        double submit — from failing with a revision conflict.
        """
        current = self.repository.get(workspace_id)
        normalized = name.strip() or "Untitled"
        if normalized == current.name:
            return current
        return self.rename(current, name)

    def delete(self, workspace: Workspace) -> None:
        self.repository.delete(
            workspace.id,
            expected_workspace_revision=workspace.workspace_revision,
        )

    def mark_exported(
        self,
        workspace: Workspace,
        *,
        clear_sample: bool = False,
    ) -> Workspace:
        return self.repository.mark_exported(
            workspace.id,
            expected_workspace_revision=workspace.workspace_revision,
            clear_sample=clear_sample,
        )
