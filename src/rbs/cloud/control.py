"""The control plane: who may use RBS, and which dataset is theirs.

This database is small, durable, and **the one to back up**. It holds no
schedule content whatsoever - only opaque subjects, the mapping from a subject
to its dataset file, the activity clock that drives eviction, and tombstones for
desks that have been reaped. That separation is what lets RBS be authoritative
for access control while staying out of system-of-record status for schedules.

Authentication is the proxy's job (see :mod:`rbs.cloud.identity`). Authorization
is ours, and it lives here: an Access policy may admit an entire organization
when RBS should admit only a handful of coordinators, and a local allowlist can
revoke someone without a change to the proxy's configuration.

The public method surface is deliberately narrow so this can become Postgres
later without touching a caller.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rbs.cloud.config import ALLOWLIST, TRUST_PROXY, CloudConfig
from rbs.ui.host import EvictionNotice, Principal

ACTIVE = "active"
REVOKED = "revoked"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    subject TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    display_hint TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL REFERENCES users(subject) ON DELETE CASCADE,
    filename TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_mutation_at TEXT NOT NULL,
    workspace_count INTEGER NOT NULL DEFAULT 0,
    size_bytes INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS datasets_by_subject ON datasets(subject);
CREATE INDEX IF NOT EXISTS datasets_by_idle ON datasets(last_mutation_at);

CREATE TABLE IF NOT EXISTS tombstones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    evicted_at TEXT NOT NULL,
    workspace_count INTEGER NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS tombstones_by_subject ON tombstones(subject);
"""


class UnknownSubjectError(KeyError):
    """Raised when a dataset is requested for a subject nobody admitted."""


@dataclass(frozen=True, slots=True)
class DatasetRecord:
    id: str
    subject: str
    filename: str
    created_at: datetime
    last_mutation_at: datetime
    workspace_count: int
    size_bytes: int

    def expires_at(self, retention: timedelta) -> datetime:
        return self.last_mutation_at + retention


class ControlPlane:
    """Authorization and the subject-to-dataset mapping."""

    def __init__(self, path: str | Path, config: CloudConfig) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._config = config

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    # ---- authorization -------------------------------------------------

    def authorize(self, principal: Principal, *, now: datetime | None = None) -> bool:
        """Decide whether this caller may use RBS, recording that they called.

        Bootstrap subjects are always admitted. Configuration outranks database
        state deliberately: without that, revoking the last administrator would
        be unrecoverable from inside the application.
        """
        moment = now or _now()
        bootstrap = principal.subject in self._config.bootstrap_subjects
        with self.connect() as conn:
            row = conn.execute(
                "SELECT subject, status FROM users WHERE subject = ?",
                (principal.subject,),
            ).fetchone()

            if row is None:
                admit = bootstrap or self._config.authorization_mode == TRUST_PROXY
                if not admit:
                    return False
                conn.execute(
                    """
                    INSERT INTO users
                        (subject, provider, display_hint, status, created_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        principal.subject,
                        principal.provider,
                        principal.display,
                        ACTIVE,
                        _stamp(moment),
                        _stamp(moment),
                    ),
                )
                return True

            admit = bootstrap or row["status"] == ACTIVE
            if self._config.authorization_mode == ALLOWLIST and not admit:
                return False
            conn.execute(
                "UPDATE users SET last_seen_at = ?, display_hint = ? WHERE subject = ?",
                (_stamp(moment), principal.display, principal.subject),
            )
            return True

    def admit(self, subject: str, *, provider: str = "cloudflare_access") -> None:
        """Add or reinstate a subject on the allowlist."""
        moment = _stamp(_now())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO users
                    (subject, provider, display_hint, status, created_at, last_seen_at)
                VALUES (?, ?, NULL, ?, ?, ?)
                ON CONFLICT(subject) DO UPDATE SET status = excluded.status
                """,
                (subject, provider, ACTIVE, moment, moment),
            )

    def revoke(self, subject: str) -> None:
        """Withdraw access without deleting the desk it maps to."""
        with self.connect() as conn:
            conn.execute("UPDATE users SET status = ? WHERE subject = ?", (REVOKED, subject))

    # ---- dataset mapping -----------------------------------------------

    def dataset_for(self, subject: str, *, now: datetime | None = None) -> DatasetRecord:
        """Return this subject's dataset, creating one on first use.

        Filenames are random, never derived from the subject, so the data
        directory cannot be attributed to people without this database.
        """
        moment = now or _now()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM datasets WHERE subject = ? ORDER BY created_at LIMIT 1",
                (subject,),
            ).fetchone()
            if row is not None:
                return _to_dataset(row)
            known = conn.execute(
                "SELECT 1 FROM users WHERE subject = ?", (subject,)
            ).fetchone()
            if known is None:
                # The foreign key would catch this, but only as an opaque
                # constraint error. A desk belongs to an admitted user, and
                # authorize() is what admits them.
                raise UnknownSubjectError(
                    f"no admitted user for subject {subject!r}; authorize() first"
                )
            record_id = uuid.uuid4().hex
            filename = f"{uuid.uuid4().hex}.sqlite"
            conn.execute(
                """
                INSERT INTO datasets
                    (id, subject, filename, created_at, last_mutation_at,
                     workspace_count, size_bytes)
                VALUES (?, ?, ?, ?, ?, 0, 0)
                """,
                (record_id, subject, filename, _stamp(moment), _stamp(moment)),
            )
            row = conn.execute("SELECT * FROM datasets WHERE id = ?", (record_id,)).fetchone()
        return _to_dataset(row)

    def find_dataset(self, subject: str) -> DatasetRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM datasets WHERE subject = ? ORDER BY created_at LIMIT 1",
                (subject,),
            ).fetchone()
        return _to_dataset(row) if row is not None else None

    def touch(
        self,
        dataset_id: str,
        *,
        now: datetime | None = None,
        workspace_count: int | None = None,
        size_bytes: int | None = None,
    ) -> None:
        """Advance the retention clock. Only mutations should reach here.

        The clock lives in this table rather than on the file because a WAL
        checkpoint moves the file's mtime without anyone having changed a
        schedule, and because websocket traffic must not count as activity - a
        forgotten open tab has to keep ageing towards eviction.
        """
        moment = _stamp(now or _now())
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE datasets
                SET last_mutation_at = ?,
                    workspace_count = COALESCE(?, workspace_count),
                    size_bytes = COALESCE(?, size_bytes)
                WHERE id = ?
                """,
                (moment, workspace_count, size_bytes, dataset_id),
            )

    def evictable(self, *, now: datetime | None = None) -> list[DatasetRecord]:
        """Datasets idle for longer than the configured retention window."""
        cutoff = (now or _now()) - self._config.retention
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM datasets WHERE last_mutation_at < ? ORDER BY last_mutation_at",
                (_stamp(cutoff),),
            ).fetchall()
        return [_to_dataset(row) for row in rows]

    def forget(self, dataset_id: str, *, now: datetime | None = None) -> DatasetRecord | None:
        """Drop a dataset's record and leave a tombstone in its place.

        The file itself belongs to the data plane and is removed by the session
        registry; this is the control-plane half of an eviction.
        """
        moment = now or _now()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
            if row is None:
                return None
            record = _to_dataset(row)
            conn.execute(
                """
                INSERT INTO tombstones (subject, evicted_at, workspace_count, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    record.subject,
                    _stamp(moment),
                    record.workspace_count,
                    _stamp(moment + self._config.retention),
                ),
            )
            conn.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
        return record

    def eviction_notice(
        self, subject: str, *, now: datetime | None = None
    ) -> EvictionNotice | None:
        """The most recent unexpired tombstone for this subject, if any."""
        moment = _stamp(now or _now())
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT evicted_at, workspace_count FROM tombstones
                WHERE subject = ? AND expires_at > ?
                ORDER BY evicted_at DESC LIMIT 1
                """,
                (subject, moment),
            ).fetchone()
        if row is None:
            return None
        return EvictionNotice(
            evicted_at=_parse(row["evicted_at"]),
            workspace_count=int(row["workspace_count"]),
        )

    def clear_eviction_notices(self, subject: str) -> None:
        """Drop this subject's tombstones once they have been shown."""
        with self.connect() as conn:
            conn.execute("DELETE FROM tombstones WHERE subject = ?", (subject,))

    def purge_expired_tombstones(self, *, now: datetime | None = None) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM tombstones WHERE expires_at <= ?", (_stamp(now or _now()),)
            )
        return cursor.rowcount


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _to_dataset(row: sqlite3.Row) -> DatasetRecord:
    return DatasetRecord(
        id=str(row["id"]),
        subject=str(row["subject"]),
        filename=str(row["filename"]),
        created_at=_parse(row["created_at"]),
        last_mutation_at=_parse(row["last_mutation_at"]),
        workspace_count=int(row["workspace_count"]),
        size_bytes=int(row["size_bytes"]),
    )
