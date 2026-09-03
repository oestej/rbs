"""Application-level workspace snapshots and command errors.

These types are shared by UI, controllers, and persistence adapters. Keeping
them outside the SQLite store lets application code depend on workspace
semantics without importing a concrete storage implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rbs.models.instance import SchedulerInput
from rbs.models.schedule import Schedule


class DownloadState(StrEnum):
    """How a workspace stands relative to the file the user owns."""

    NEVER = "never"
    STALE = "stale"
    CURRENT = "current"


#: Revision assigned at creation; every mutation bumps ``workspace_revision``.
INITIAL_WORKSPACE_REVISION = 1


@dataclass
class Workspace:
    """Immutable-in-practice snapshot returned by a workspace repository."""

    id: int
    name: str
    academic_year: str
    catalog_id: int
    catalog_name: str
    instance: SchedulerInput
    schedule: Schedule | None
    stale_schedule: Schedule | None
    instance_revision: int
    workspace_revision: int
    schedule_revision: int | None
    created_at: str
    updated_at: str
    is_sample: bool = False
    exported_instance_revision: int | None = None
    exported_schedule_revision: int | None = None
    exported_workspace_revision: int | None = None
    exported_at: str | None = None

    @property
    def solution_is_out_of_date(self) -> bool:
        """Whether a previous solution exists for an older input revision."""
        return self.stale_schedule is not None

    @property
    def download_state(self) -> DownloadState:
        """Whether the user's file still matches the repository snapshot."""
        if self.exported_at is None:
            return DownloadState.NEVER
        matches = (
            self.exported_workspace_revision == self.workspace_revision
            if self.exported_workspace_revision is not None
            else self.exported_instance_revision == self.instance_revision
            and self.exported_schedule_revision == self.schedule_revision
        )
        return DownloadState.CURRENT if matches else DownloadState.STALE

    @property
    def has_unsaved_changes(self) -> bool:
        """Whether closing would discard anything the user did.

        A workspace that was never saved but also never modified holds
        nothing the user authored, so it needs no confirmation. Creation
        starts ``workspace_revision`` at ``INITIAL_WORKSPACE_REVISION``
        and every mutation bumps it, which is what distinguishes "new"
        from "changed".
        """
        if self.download_state is DownloadState.STALE:
            return True
        if self.download_state is DownloadState.CURRENT:
            return False
        return self.workspace_revision != INITIAL_WORKSPACE_REVISION

    @property
    def latest_schedule(self) -> Schedule | None:
        """Return the last solution, including one marked out of date."""
        return self.schedule or self.stale_schedule


class DeskFullError(ValueError):
    """Raised when a desk already holds as many workspaces as it may."""


class WorkspaceConflictError(ValueError):
    """Raised when a command was based on an older workspace revision."""
