"""Revisioned scheduling-workspace storage (mixin for Store)."""

from __future__ import annotations

import sqlite3

from rbs.emit import dumps
from rbs.models.catalog import ConstraintCatalog
from rbs.models.instance import SchedulerInput, SchedulingCase
from rbs.models.schedule import Schedule
from rbs.models.workspace import Workspace, WorkspaceConflictError
from rbs.solver.validation import validate_schedule, validate_schedule_or_raise
from rbs.store_schema import _WORKSPACE_SELECT, CURRENT_KEY
from rbs.store_support import _now


class StoreWorkspaceMixin:
    """Workspace CRUD and revision policy backing the Store desk."""

    def workspace_count(self) -> int:
        """How many workspaces are on this desk.

        Deliberately not ``len(self.list())``: that parses and revalidates every
        stored instance, catalog and schedule, which is far too expensive for a
        number used only for bookkeeping.
        """
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS total FROM workspaces").fetchone()
        return int(row["total"])

    def list(self) -> list[Workspace]:
        with self.connect() as conn:
            rows = conn.execute(_WORKSPACE_SELECT + " ORDER BY w.updated_at DESC").fetchall()
        return [self._row_to_workspace(row) for row in rows]

    def get(self, workspace_id: int) -> Workspace:
        with self.connect() as conn:
            row = self._workspace_row(conn, workspace_id)
        return self._row_to_workspace(row)

    def _workspace_row(
        self,
        conn: sqlite3.Connection,
        workspace_id: int,
    ) -> sqlite3.Row:
        row = conn.execute(
            _WORKSPACE_SELECT + " WHERE w.id = ?",
            (workspace_id,),
        ).fetchone()
        if row is None:
            raise KeyError(workspace_id)
        return row

    @staticmethod
    def _require_workspace_revision(
        row: sqlite3.Row,
        expected_workspace_revision: int,
    ) -> None:
        current = int(row["workspace_revision"])
        if current != expected_workspace_revision:
            raise WorkspaceConflictError(
                f"workspace changed from revision {expected_workspace_revision} "
                f"to {current}; reload it before saving"
            )

    def create(
        self,
        name: str,
        instance: SchedulerInput,
        schedule: Schedule | None = None,
        *,
        is_sample: bool = False,
    ) -> Workspace:
        if schedule is not None:
            validate_schedule_or_raise(instance, schedule)
            schedule = schedule.model_copy(
                update={"meta": schedule.meta.model_copy(update={"source_instance_revision": 1})}
            )
        now = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._guard_capacity(conn=conn)
            catalog_id = self._put_catalog(
                conn,
                f"{name} constraints",
                instance.constraint_catalog(),
                managed=True,
            )
            cursor = conn.execute(
                """
                INSERT INTO workspaces
                    (name, academic_year, catalog_id, instance_json, schedule_json,
                     instance_revision, workspace_revision, schedule_revision,
                     created_at, updated_at, is_sample)
                VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?)
                """,
                (
                    name.strip() or "Untitled",
                    instance.academic_year,
                    catalog_id,
                    dumps(instance.scheduling_case()),
                    dumps(schedule) if schedule is not None else None,
                    1 if schedule is not None else None,
                    now,
                    now,
                    int(is_sample),
                ),
            )
            workspace_id = int(cursor.lastrowid)
        self.set_current(workspace_id)
        return self.get(workspace_id)

    def save_instance(
        self,
        workspace_id: int,
        instance: SchedulerInput,
        *,
        expected_workspace_revision: int,
        preserve_schedule: bool = False,
    ) -> Workspace:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._workspace_row(conn, workspace_id)
            self._require_workspace_revision(row, expected_workspace_revision)
            workspace = self._row_to_workspace(row)
            next_instance_revision = workspace.instance_revision + 1
            schedule = workspace.schedule if preserve_schedule else None
            preserve_current_schedule = schedule is not None
            if preserve_current_schedule:
                validate_schedule_or_raise(instance, schedule)
                schedule = schedule.model_copy(
                    update={
                        "meta": schedule.meta.model_copy(
                            update={"source_instance_revision": next_instance_revision}
                        )
                    }
                )
            previous_catalog_id = workspace.catalog_id
            catalog_id = self._put_catalog(
                conn,
                f"Workspace {workspace_id} constraints",
                instance.constraint_catalog(),
                managed=True,
            )
            if preserve_current_schedule:
                cursor = conn.execute(
                    """
                    UPDATE workspaces
                    SET academic_year = ?, catalog_id = ?, instance_json = ?,
                        schedule_json = ?, schedule_revision = ?,
                        instance_revision = instance_revision + 1,
                        workspace_revision = workspace_revision + 1, updated_at = ?
                    WHERE id = ? AND workspace_revision = ?
                    """,
                    (
                        instance.academic_year,
                        catalog_id,
                        dumps(instance.scheduling_case()),
                        dumps(schedule) if schedule is not None else None,
                        next_instance_revision,
                        _now(),
                        workspace_id,
                        expected_workspace_revision,
                    ),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE workspaces
                    SET academic_year = ?, catalog_id = ?, instance_json = ?,
                        instance_revision = instance_revision + 1,
                        workspace_revision = workspace_revision + 1, updated_at = ?
                    WHERE id = ? AND workspace_revision = ?
                    """,
                    (
                        instance.academic_year,
                        catalog_id,
                        dumps(instance.scheduling_case()),
                        _now(),
                        workspace_id,
                        expected_workspace_revision,
                    ),
                )
            if cursor.rowcount != 1:
                raise WorkspaceConflictError("workspace changed before instance save")
            self._discard_unreferenced_catalog(conn, previous_catalog_id)
        return self.get(workspace_id)

    def save_schedule(
        self,
        workspace_id: int,
        schedule: Schedule | None,
        *,
        expected_instance_revision: int,
        expected_workspace_revision: int,
    ) -> Workspace:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._workspace_row(conn, workspace_id)
            self._require_workspace_revision(row, expected_workspace_revision)
            if int(row["instance_revision"]) != expected_instance_revision:
                raise WorkspaceConflictError(
                    "workspace inputs changed while the schedule was being solved"
                )
            if schedule is not None:
                workspace = self._row_to_workspace(row)
                validate_schedule_or_raise(workspace.instance, schedule)
                schedule = schedule.model_copy(
                    update={
                        "meta": schedule.meta.model_copy(
                            update={"source_instance_revision": expected_instance_revision}
                        )
                    }
                )
            cursor = conn.execute(
                """
                UPDATE workspaces
                SET schedule_json = ?, schedule_revision = ?,
                    workspace_revision = workspace_revision + 1, updated_at = ?
                WHERE id = ? AND instance_revision = ? AND workspace_revision = ?
                """,
                (
                    dumps(schedule) if schedule is not None else None,
                    expected_instance_revision if schedule is not None else None,
                    _now(),
                    workspace_id,
                    expected_instance_revision,
                    expected_workspace_revision,
                ),
            )
        if cursor.rowcount != 1:
            raise WorkspaceConflictError("workspace changed while the schedule was being solved")
        return self.get(workspace_id)

    def rename(
        self,
        workspace_id: int,
        name: str,
        *,
        expected_workspace_revision: int,
    ) -> Workspace:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._workspace_row(conn, workspace_id)
            self._require_workspace_revision(row, expected_workspace_revision)
            cursor = conn.execute(
                """
                UPDATE workspaces
                SET name = ?, workspace_revision = workspace_revision + 1, updated_at = ?
                WHERE id = ? AND workspace_revision = ?
                """,
                (
                    name.strip() or "Untitled",
                    _now(),
                    workspace_id,
                    expected_workspace_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise WorkspaceConflictError("workspace changed before rename")
        return self.get(workspace_id)

    def delete(self, workspace_id: int, *, expected_workspace_revision: int) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._workspace_row(conn, workspace_id)
            self._require_workspace_revision(row, expected_workspace_revision)
            cursor = conn.execute(
                "DELETE FROM workspaces WHERE id = ? AND workspace_revision = ?",
                (workspace_id, expected_workspace_revision),
            )
            if cursor.rowcount != 1:
                raise WorkspaceConflictError("workspace changed before deletion")
            self._discard_unreferenced_catalog(conn, int(row["catalog_id"]))
        current = self.current_id()
        if current == workspace_id:
            remaining = self.list()
            if remaining:
                self.set_current(remaining[0].id)
            else:
                self.set_current(None)

    def current_id(self) -> int | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_meta WHERE key = ?", (CURRENT_KEY,)
            ).fetchone()
        if row is None:
            return None
        return int(row["value"])

    def current(self) -> Workspace | None:
        workspace_id = self.current_id()
        if workspace_id is None:
            return None
        try:
            return self.get(workspace_id)
        except KeyError:
            return None

    def set_current(self, workspace_id: int | None) -> None:
        with self.connect() as conn:
            if workspace_id is None:
                conn.execute("DELETE FROM app_meta WHERE key = ?", (CURRENT_KEY,))
                return
            conn.execute(
                """
                INSERT INTO app_meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (CURRENT_KEY, str(workspace_id)),
            )

    def _row_to_workspace(self, row: sqlite3.Row) -> Workspace:
        case = SchedulingCase.model_validate_json(row["instance_json"])
        catalog = ConstraintCatalog.model_validate_json(row["catalog_json"])
        instance = catalog.apply(case)
        schedule = None
        stale_schedule = None
        if row["schedule_json"]:
            stored_schedule = Schedule.model_validate_json(row["schedule_json"])
            if (
                row["schedule_revision"] == row["instance_revision"]
                and validate_schedule(instance, stored_schedule).valid
            ):
                schedule = stored_schedule
            else:
                stale_schedule = stored_schedule
        keys = row.keys()
        return Workspace(
            id=row["id"],
            name=row["name"],
            academic_year=row["academic_year"],
            catalog_id=row["catalog_id"],
            catalog_name=row["catalog_name"],
            instance=instance,
            schedule=schedule,
            stale_schedule=stale_schedule,
            instance_revision=row["instance_revision"],
            workspace_revision=(
                row["workspace_revision"]
                if "workspace_revision" in keys
                else row["instance_revision"]
            ),
            schedule_revision=row["schedule_revision"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            is_sample=bool(row["is_sample"]) if "is_sample" in keys else False,
            exported_instance_revision=(
                row["exported_instance_revision"] if "exported_instance_revision" in keys else None
            ),
            exported_schedule_revision=(
                row["exported_schedule_revision"] if "exported_schedule_revision" in keys else None
            ),
            exported_workspace_revision=(
                row["exported_workspace_revision"]
                if "exported_workspace_revision" in keys
                else None
            ),
            exported_at=row["exported_at"] if "exported_at" in keys else None,
        )
