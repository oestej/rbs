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
