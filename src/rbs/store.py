"""SQLite constraint catalogs and revisioned scheduling workspaces."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from rbs.catalog import bootstrap_catalog, current_sample_instance
from rbs.logging import get_logger
from rbs.models.workspace import (
    DeskFullError,
    Workspace,
)
from rbs.models.workspace import (
    DownloadState as DownloadState,
)
from rbs.models.workspace import (
    WorkspaceConflictError as WorkspaceConflictError,
)
from rbs.store_catalogs import StoreCatalogMixin
from rbs.store_exchange import StoreExchangeMixin
from rbs.store_schema import (
    CURRENT_KEY,
    SCHEMA,
)
from rbs.store_support import (
    CatalogRecord,
    StoreInvalidatedError,
    catalog_hash,
    load_object,
    now_iso,
    schedule_has_rotation,
)
from rbs.store_workspaces import StoreWorkspaceMixin

__all__ = [
    "CURRENT_KEY",
    "SCHEMA",
    "CatalogRecord",
    "Store",
    "StoreCatalogMixin",
    "StoreExchangeMixin",
    "StoreInvalidatedError",
    "StoreWorkspaceMixin",
    "WorkspaceConflictError",
    "catalog_hash",
    "load_object",
    "now_iso",
    "schedule_has_rotation",
]

logger = get_logger("store")


class Store(StoreCatalogMixin, StoreWorkspaceMixin, StoreExchangeMixin):
    """SQLite constraint catalogs and revisioned scheduling workspaces.

    Storage behavior lives in the ``store_*`` mixins (catalogs, workspaces,
    exchange); this class keeps connection lifecycle, database
    initialization, and the public constructor.
    """
    def __init__(
        self,
        path: str | Path = "rbs.sqlite",
        *,
        max_workspaces: int | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A desk is what you are working on, not an archive. Capping it is what
        # stops one becoming the other by accumulation.
        self.max_workspaces = max_workspaces
        self._commit_listeners: list[Callable[[], None]] = []
        self._listener_lock = threading.RLock()
        self._lifecycle = threading.Condition(threading.RLock())
        self._active_connections = 0
        self._invalidated_reason: str | None = None

    @property
    def is_valid(self) -> bool:
        """Whether this handle may still open its backing SQLite dataset."""
        with self._lifecycle:
            return self._invalidated_reason is None

    def invalidate(self, reason: str = "workspace dataset was retired") -> None:
        """Prevent new connections and wait for in-flight transactions to finish.

        SQLite creates a missing database file on connect. Hosted retention must
        therefore invalidate every live handle *before* unlinking its dataset, or
        a forgotten browser tab can recreate the retired filename as an orphan.
        """
        with self._lifecycle:
            self._invalidated_reason = reason
            while self._active_connections:
                self._lifecycle.wait()

    def _begin_use(self) -> None:
        with self._lifecycle:
            if self._invalidated_reason is not None:
                raise StoreInvalidatedError(self._invalidated_reason)
            self._active_connections += 1

    def _end_use(self) -> None:
        with self._lifecycle:
            self._active_connections -= 1
            if not self._active_connections:
                self._lifecycle.notify_all()

    def add_commit_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Run ``listener`` after each successful connection that changed data.

        Listeners observe already-committed state and execute after the SQLite
        connection closes, so they may safely export through this Store. A
        listener failure is logged rather than making a completed database edit
        appear to have failed.
        """
        with self._listener_lock:
            self._commit_listeners.append(listener)

        def unsubscribe() -> None:
            with self._listener_lock:
                try:
                    self._commit_listeners.remove(listener)
                except ValueError:
                    pass

        return unsubscribe

    def _guard_capacity(
        self,
        adding: int = 1,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        if self.max_workspaces is None:
            return
        if conn is None:
            current = self.workspace_count()
        else:
            row = conn.execute("SELECT COUNT(*) AS total FROM workspaces").fetchone()
            current = int(row["total"])
        if current + adding > self.max_workspaces:
            raise DeskFullError(
                f"this desk holds {current} of at most {self.max_workspaces} "
                "workspaces; close one you have downloaded before opening another"
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self._begin_use()
        try:
            conn = sqlite3.connect(self.path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            # workspaces.catalog_id is a declared foreign key; SQLite ignores it unless
            # enforcement is switched on per connection.
            conn.execute("PRAGMA foreign_keys = ON")
            # One user can have several tabs open against the same database, and a
            # solve can hold a write open for a while.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            initial_changes = conn.total_changes
            changed = False
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
                changed = conn.total_changes > initial_changes
            finally:
                conn.close()
            if changed:
                with self._listener_lock:
                    listeners = tuple(self._commit_listeners)
                for listener in listeners:
                    try:
                        listener()
                    except Exception:
                        logger.exception("database.commit_listener_failed")
        finally:
            self._end_use()

    def init(self) -> None:
        """Create current-schema tables and seed the bundled catalog.

        Only the current schema is supported: databases or documents written
        by older builds fail validation with a descriptive error instead of
        being upgraded in place.
        """
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._put_catalog(
                conn,
                "Bundled default",
                bootstrap_catalog(),
                managed=False,
            )

    def ensure_sample(self) -> Workspace:
        self.init()
        existing = self.list()
        if existing:
            current = self.current()
            return current if current is not None else self.get(existing[0].id)
        instance = current_sample_instance()
        workspace = self.create(
            f"Sample {instance.academic_year}",
            instance,
            is_sample=True,
        )
        self.set_current(workspace.id)
        return workspace
