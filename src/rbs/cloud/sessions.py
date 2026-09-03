"""The data plane: one workspace database per user, and the sweep that reaps them.

Files here are named for nothing and hold everything. The mapping from a person
to their file lives in the control plane, which is what makes this directory
unattributable on its own - and what makes ``do not back this up`` a coherent
instruction rather than a hopeful one.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from rbs.cloud.config import CloudConfig
from rbs.cloud.control import ControlPlane, DatasetRecord
from rbs.logging import get_logger
from rbs.store import Store

logger = get_logger("cloud.sessions")


class SubjectRepository:
    """Stable per-subject handle that resolves the current dataset per operation.

    NiceGUI pages live longer than retention records. Keeping this proxy in a
    page session lets an evicted subject transparently move to their newly
    allocated empty dataset instead of retaining a deleted :class:`Store`.
    """

    def __init__(self, registry: SessionRegistry, subject: str) -> None:
        self._registry = registry
        self._subject = subject

    @property
    def path(self) -> Path:
        return self._registry.store_for(self._subject).path

    @property
    def max_workspaces(self) -> int | None:
        return self._registry.store_for(self._subject).max_workspaces

    def __getattr__(self, name: str):
        # Resolve again inside the callable. UI frameworks often retain a bound
        # callback for hours, including across a retention sweep.
        attribute = getattr(self._registry.store_for(self._subject), name)
        if not callable(attribute):
            return attribute

        def current_call(*args, **kwargs):
            return getattr(self._registry.store_for(self._subject), name)(*args, **kwargs)

        return current_call


class SessionRegistry:
    """Resolves a subject to the ``Store`` holding that subject's desk."""

    def __init__(self, control: ControlPlane, config: CloudConfig) -> None:
        self._control = control
        self._config = config
        self._data_dir = Path(config.data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._stores: dict[str, Store] = {}
        self._repositories: dict[str, SubjectRepository] = {}

    def path_for(self, record: DatasetRecord) -> Path:
        return self._data_dir / record.filename

    def store_for(self, subject: str, *, now: datetime | None = None) -> Store:
        """Open (or create) this subject's dataset.

        The dataset is resolved through the control plane on every call rather
        than cached against the subject, so a desk evicted between requests
        yields a fresh dataset instead of a ``Store`` pointing at a deleted file.
        """
        with self._lock:
            # Resolve the control-plane record under the same lock eviction
            # uses. Otherwise a caller can capture the retiring record, wait
            # for eviction to finish, and then reopen its deleted filename.
            record = self._control.dataset_for(subject, now=now)
            store = self._stores.get(record.id)
            if store is not None:
                return store
            store = Store(self.path_for(record), max_workspaces=self._config.desk_cap)
            # init() only. A new hosted user gets an empty desk and is asked to
            # open a file; the sample workspace belongs to the desktop build.
            store.init()
            store.add_commit_listener(
                lambda: self._record_mutation(record.id, subject, store)
            )
            self._stores[record.id] = store
            logger.info("session.dataset_opened")
            return store

    def repository_for(self, subject: str) -> SubjectRepository:
        """Return the stable repository handle suitable for a browser session."""
        with self._lock:
            repository = self._repositories.get(subject)
            if repository is None:
                repository = SubjectRepository(self, subject)
                self._repositories[subject] = repository
            return repository

    def _record_mutation(self, record_id: str, subject: str, store: Store) -> None:
        """Advance hosted retention from the database commit itself."""
        current = self._control.find_dataset(subject)
        if current is None or current.id != record_id:
            return
        size = sum(
            candidate.stat().st_size
            for candidate in (
                store.path,
                store.path.with_suffix(".sqlite-wal"),
                store.path.with_suffix(".sqlite-shm"),
            )
            if candidate.exists()
        )
        self._control.touch(
            record_id,
            workspace_count=store.workspace_count(),
            size_bytes=size,
        )

    def evict(self, record: DatasetRecord, *, now: datetime | None = None) -> None:
        """Delete one dataset's file and retire its control-plane record."""
        with self._lock:
            store = self._stores.pop(record.id, None)
            if store is not None:
                store.invalidate("workspace dataset expired and was retired")
            path = self.path_for(record)
            for candidate in (
                path,
                path.with_suffix(".sqlite-wal"),
                path.with_suffix(".sqlite-shm"),
            ):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "retention.file_remove_failed",
                        error_code="dataset_file_remove",
                        exc_info=True,
                    )
            self._control.forget(record.id, now=now)

    def sweep(self, *, now: datetime | None = None) -> int:
        """Evict every dataset idle beyond the retention window."""
        evicted = 0
        for record in self._control.evictable(now=now):
            self.evict(record, now=now)
            evicted += 1
        self._control.purge_expired_tombstones(now=now)
        if evicted:
            logger.info("retention.datasets_evicted", evicted_count=evicted)
        return evicted

    def orphaned_files(self) -> list[Path]:
        """Data files no dataset record points at.

        A crash between removing a record and removing its file would otherwise
        leave content behind with nothing left to describe or reap it.
        """
        with self._control.connect() as conn:
            known = {
                str(row["filename"])
                for row in conn.execute("SELECT filename FROM datasets").fetchall()
            }
        return [
            path
            for path in sorted(self._data_dir.glob("*.sqlite"))
            if path.name not in known
        ]

    def remove_orphans(self) -> int:
        removed = 0
        for path in self.orphaned_files():
            try:
                path.unlink()
                removed += 1
            except OSError:
                logger.warning(
                    "retention.orphan_remove_failed",
                    error_code="orphan_file_remove",
                    exc_info=True,
                )
        return removed
