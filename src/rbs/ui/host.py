"""Host seam between the shared workspace UI and its packaging.

``rbs.ui`` renders one workspace application. What differs between the local
desktop build and the hosted build is *whose* database it renders, whether that
database expires, where a solve runs, and which product packaging the shared UI
is presenting. Those decisions are the whole of :class:`WorkspaceHost`.

The dependency direction is deliberate and enforced by ``tests/test_packaging``:
``rbs.cloud`` imports this module, never the reverse. Keeping :class:`Principal`
here rather than beside the identity adapters is what lets the shared UI talk
about callers without importing the cloud package.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple, Protocol, runtime_checkable

from rbs.models.instance import SchedulerInput, SolverProblem
from rbs.models.schedule import Schedule
from rbs.models.workspace import Workspace
from rbs.product import LOCAL_PRODUCT, ProductConfig
from rbs.repository import WorkspaceRepository
from rbs.solver.client import SolverProcessClient

LOCAL_SUBJECT = "local"
LOCAL_PROVIDER = "local"
DEFAULT_UPLOAD_MAX_BYTES = 32 * 1024 * 1024


class Principal(NamedTuple):
    """An already-authenticated caller.

    RBS never authenticates anyone. In the hosted build an identity adapter
    reads a principal a proxy has already vouched for; locally there is exactly
    one caller and it is assumed.
    """

    subject: str
    display: str | None = None
    provider: str = LOCAL_PROVIDER


@dataclass(frozen=True, slots=True)
class SessionStatus:
    """Retention state for one hosted dataset.

    Hosts that never expire anything return ``None`` instead of this, which is
    how the local build renders no retention chrome at all rather than having
    to render a disabled version of it.
    """

    last_mutation_at: datetime
    expires_at: datetime
    workspace_count: int
    warn_within: timedelta | None = None

    def remaining(self, now: datetime) -> timedelta:
        """Time left before eviction, floored at zero."""
        return max(self.expires_at - now, timedelta(0))

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def should_warn(self, now: datetime) -> bool:
        """Whether the desk is close enough to eviction to say so."""
        if self.warn_within is None:
            return False
        return self.remaining(now) <= self.warn_within


@dataclass(frozen=True, slots=True)
class EvictionNotice:
    """What a returning user is told in place of a dataset that was reaped.

    Carries a count and a date, never schedule content, so that an evicted
    desk reads as ``your 3 workspaces were closed on 12 March`` instead of
    silently appearing empty.
    """

    evicted_at: datetime
    workspace_count: int


@runtime_checkable
class DocumentIO(Protocol):
    """Optional file ownership supplied by a native document packaging.

    Browser and cloud packagings leave this capability absent and retain their
    upload/download flow. A native host supplies direct operating-system file
    access without teaching the shared UI about pywebview or platform APIs.
    """

    application_name: str
    generation: int
    path: Path | None
    recovery_error: str | None
    settings_error: str | None
    recovered_from: Path | None

    @property
    def workspace(self) -> Workspace | None: ...

    @property
    def dirty(self) -> bool: ...

    @property
    def supports_application_settings(self) -> bool: ...

    async def open(self): ...

    async def save(self) -> Path | None: ...

    async def save_as(self) -> Path | None: ...

    async def save_settings(self) -> Path | None: ...

    async def load_settings(self): ...

    def new(self): ...

    def close(self) -> None: ...

    def clear_recovery_checkpoint(self) -> bool:
        """Remove native crash-recovery state after an orderly app exit."""
        ...

    def sync_application_settings(self, instance: SchedulerInput) -> bool: ...


@runtime_checkable
class WorkspaceHost(Protocol):
    """Everything the shared UI needs that differs between packagings."""

    product: ProductConfig
    #: Whether this packaging offers whole-database replace.
    #:
    #: Restoring deletes every workspace and catalog outright, which is the one
    #: path that would sidestep the per-workspace close gate and the desk cap.
    #: On a desktop build that is just "restore my own backup" and is expected;
    #: hosted, it buys nothing - opening a file already merges a whole-database
    #: export - while being the only way to destroy unsaved work in bulk.
    allows_database_restore: bool
    document_io: DocumentIO | None
    upload_max_bytes: int

    def principal(self, request) -> Principal | None:
        """Resolve the caller, or ``None`` when the request is not authorized."""
        ...

    def store_for(self, principal: Principal) -> WorkspaceRepository:
        """Return the workspace database this caller edits."""
        ...

    def session_status(self, principal: Principal) -> SessionStatus | None:
        """Retention state, or ``None`` where nothing expires."""
        ...

    def eviction_notice(self, principal: Principal) -> EvictionNotice | None:
        """A tombstone to show a returning user, if their desk was reaped."""
        ...

    def touch(self, principal: Principal) -> None:
        """Record caller activity. Only mutations should call this."""
        ...

    async def solve(
        self,
        principal: Principal,
        instance: SchedulerInput,
        *,
        reference_schedule: Schedule | None = None,
    ) -> Schedule:
        """Run one solve, wherever this packaging runs solves."""
        ...


class LocalHost:
    """Single-user desktop packaging backed by the standalone solver process."""

    product = LOCAL_PRODUCT
    # Your machine, your file, and no gate for it to be inconsistent with.
    allows_database_restore = True
    upload_max_bytes = DEFAULT_UPLOAD_MAX_BYTES
    document_io: DocumentIO | None

    def __init__(
        self,
        store: WorkspaceRepository,
        *,
        display: str | None = None,
        solver: SolverProcessClient | None = None,
        document_io: DocumentIO | None = None,
    ) -> None:
        self._store = store
        self._principal = Principal(LOCAL_SUBJECT, display, LOCAL_PROVIDER)
        self._solver = solver or SolverProcessClient()
        self.document_io = document_io
        self.allows_database_restore = type(self).allows_database_restore and document_io is None

    def bootstrap(self) -> None:
        """Seed the sample workspace, as the desktop build has always done."""
        self._store.ensure_sample()

    def principal(self, request) -> Principal | None:  # noqa: ARG002 - no auth locally
        return self._principal

    def store_for(self, principal: Principal) -> WorkspaceRepository:  # noqa: ARG002 - one store
        return self._store

    def session_status(self, principal: Principal) -> SessionStatus | None:  # noqa: ARG002
        return None

    def eviction_notice(self, principal: Principal) -> EvictionNotice | None:  # noqa: ARG002
        return None

    def touch(self, principal: Principal) -> None:
        """No retention clock to advance."""

    async def solve(
        self,
        principal: Principal,  # noqa: ARG002 - no queue to place the caller in
        instance: SchedulerInput,
        *,
        reference_schedule: Schedule | None = None,
    ) -> Schedule:
        return await self._solver.solve_async(
            SolverProblem.from_instance(instance),
            options=instance.solver,
            reference_solution=reference_schedule,
        )
