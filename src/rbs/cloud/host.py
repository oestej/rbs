"""The hosted packaging's :class:`~rbs.ui.host.WorkspaceHost`.

Wires the four hosted concerns together behind the seam the shared UI sees:
who is calling (identity), whether they may (control plane), which database is
theirs (session registry), and where their solve runs (solve pool).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from rbs.cloud.config import CloudConfig
from rbs.cloud.control import ControlPlane
from rbs.cloud.identity import IdentityAdapter
from rbs.cloud.sessions import SessionRegistry
from rbs.cloud.solve_pool import SolvePool
from rbs.logging import get_logger
from rbs.models.instance import SchedulerInput
from rbs.models.schedule import Schedule
from rbs.product import CLOUD_PRODUCT
from rbs.repository import WorkspaceRepository
from rbs.ui.host import EvictionNotice, Principal, SessionStatus

logger = get_logger("cloud.host")


class CloudHost:
    product = CLOUD_PRODUCT
    # Opening a file already merges a whole-database export, so replace would
    # add nothing but a way to destroy every workspace at once - including ones
    # that have never been saved, which the close gate exists to prevent.
    allows_database_restore = False
    document_io = None

    def __init__(
        self,
        config: CloudConfig,
        control: ControlPlane,
        registry: SessionRegistry,
        identity: IdentityAdapter,
        solve_pool: SolvePool,
    ) -> None:
        self._config = config
        self._control = control
        self._registry = registry
        self._identity = identity
        self._pool = solve_pool
        self._sweeper: asyncio.Task | None = None

    # ---- WorkspaceHost --------------------------------------------------

    def principal(self, request) -> Principal | None:
        """Who is calling, and may they use RBS.

        Two separate questions with two separate owners: the proxy answers the
        first, the control-plane allowlist answers the second.
        """
        principal = self._identity.resolve(request)
        if principal is None:
            logger.info("authorization.denied", reason="identity_missing")
            return None
        if not self._control.authorize(principal):
            logger.info("authorization.denied", reason="not_allowlisted")
            return None
        return principal

    def store_for(self, principal: Principal) -> WorkspaceRepository:
        return self._registry.repository_for(principal.subject)

    @property
    def upload_max_bytes(self) -> int:
        return self._config.upload_max_bytes

    def session_status(self, principal: Principal) -> SessionStatus | None:
        record = self._control.find_dataset(principal.subject)
        if record is None:
            return None
        return SessionStatus(
            last_mutation_at=record.last_mutation_at,
            expires_at=record.expires_at(self._config.retention),
            workspace_count=record.workspace_count,
            warn_within=self._config.retention_warning,
        )

    def eviction_notice(self, principal: Principal) -> EvictionNotice | None:
        return self._control.eviction_notice(principal.subject)

    def acknowledge_eviction(self, principal: Principal) -> None:
        self._control.clear_eviction_notices(principal.subject)

    def touch(self, principal: Principal) -> None:
        record = self._control.find_dataset(principal.subject)
        if record is None:
            return
        count = None
        try:
            count = self._registry.store_for(principal.subject).workspace_count()
        except Exception:  # noqa: BLE001 - bookkeeping must never break a save
            logger.debug(
                "retention.workspace_count_failed",
                error_code="workspace_count",
            )
        self._control.touch(record.id, workspace_count=count)

    async def solve(
        self,
        principal: Principal,  # noqa: ARG002 - the pool is shared, not per-caller
        instance: SchedulerInput,
        *,
        reference_schedule: Schedule | None = None,
    ) -> Schedule:
        return await self._pool.solve(instance, reference_schedule=reference_schedule)

    # ---- lifecycle ------------------------------------------------------

    @property
    def queue_depth(self) -> int:
        return self._pool.waiting

    def sweep_once(self, *, now: datetime | None = None) -> int:
        return self._registry.sweep(now=now)

    def start_sweeper(self, *, interval_seconds: float = 3600.0) -> None:
        if self._sweeper is not None:
            return

        async def _loop() -> None:
            while True:
                try:
                    await asyncio.to_thread(self._registry.sweep, now=datetime.now(UTC))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("retention.sweep_failed")
                await asyncio.sleep(interval_seconds)

        self._sweeper = asyncio.create_task(_loop())

    async def shutdown(self) -> None:
        """Drain in-flight work before the process goes away."""
        if self._sweeper is not None:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except asyncio.CancelledError:
                pass
            self._sweeper = None
        await self._pool.shutdown()
        logger.info("solver.pool_stopped")
