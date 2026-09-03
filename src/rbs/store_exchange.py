"""Portable workspace exchange: RBSC import/export (mixin for Store)."""

from __future__ import annotations

import json

from rbs.emit import dumps, dumps_bundle
from rbs.ingest import parse_workspace_payload
from rbs.models.rbsc import RBSCState
from rbs.models.workspace import DeskFullError, Workspace, WorkspaceConflictError
from rbs.solver.validation import validate_schedule_or_raise
from rbs.store_schema import CURRENT_KEY
from rbs.store_support import (
    _catalog_hash,
    _load_object,
    _now,
    _row_to_rbsc_catalog,
    _row_to_rbsc_workspace,
)


class StoreExchangeMixin:
    """RBSC document exchange and JSON import/export for Store."""

    def inspect_rbsc(self, payload: str) -> RBSCState:
        """Parse and fully validate a portable database-state payload."""
        state = RBSCState.model_validate(_load_object(payload))
        if CURRENT_KEY in state.app_metadata:
            raise ValueError(f"{CURRENT_KEY!r} is reserved; use current_workspace_id")
        catalogs = {record.id: record.catalog for record in state.catalogs}
        for workspace in state.workspaces:
            if (
                workspace.schedule is None
                or workspace.schedule_revision != workspace.instance_revision
            ):
                continue
            instance = catalogs[workspace.catalog_id].apply(workspace.case)
            validate_schedule_or_raise(instance, workspace.schedule)
        return state

    def export_rbsc(self) -> str:
        """Export every database-backed record as one versioned RBSC document."""
        with self.connect() as conn:
            catalog_rows = conn.execute("SELECT * FROM catalogs ORDER BY id").fetchall()
            workspace_rows = conn.execute("SELECT * FROM workspaces ORDER BY id").fetchall()
            metadata_rows = conn.execute("SELECT key, value FROM app_meta ORDER BY key").fetchall()

        metadata = {str(row["key"]): str(row["value"]) for row in metadata_rows}
        current_raw = metadata.pop(CURRENT_KEY, None)
        state = RBSCState(
            exported_at=_now(),
            current_workspace_id=int(current_raw) if current_raw is not None else None,
            app_metadata=metadata,
            catalogs=[_row_to_rbsc_catalog(row) for row in catalog_rows],
            workspaces=[_row_to_rbsc_workspace(row) for row in workspace_rows],
        )
        return json.dumps(state.model_dump(mode="json"), indent=2) + "\n"

    def restore_rbsc(self, payload: str) -> RBSCState:
        """Atomically replace the database contents from a validated RBSC file."""
        state = self.inspect_rbsc(payload)
        if self.max_workspaces is not None and len(state.workspaces) > self.max_workspaces:
            raise DeskFullError(
                f"that file holds {len(state.workspaces)} workspaces, more than "
                f"the {self.max_workspaces} this desk allows"
            )
        self.init()
        with self.connect() as conn:
            conn.execute("DELETE FROM workspaces")
            conn.execute("DELETE FROM catalogs")
            conn.execute("DELETE FROM app_meta")
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('catalogs', 'workspaces')")

            for record in state.catalogs:
                conn.execute(
                    """
                    INSERT INTO catalogs
                        (id, name, schema_version, content_hash, catalog_json,
                         managed, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        record.name,
                        record.catalog.schema_version,
                        _catalog_hash(record.catalog),
                        dumps(record.catalog),
                        int(record.managed),
                        record.created_at,
                        record.updated_at,
                    ),
                )

            for workspace in state.workspaces:
                conn.execute(
                    """
                    INSERT INTO workspaces
                        (id, name, academic_year, catalog_id, instance_json,
                         schedule_json, instance_revision, workspace_revision,
                         schedule_revision, created_at, updated_at, is_sample,
                         exported_instance_revision, exported_schedule_revision,
                         exported_workspace_revision, exported_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace.id,
                        workspace.name,
                        workspace.academic_year,
                        workspace.catalog_id,
                        dumps(workspace.case),
                        dumps(workspace.schedule) if workspace.schedule is not None else None,
                        workspace.instance_revision,
                        workspace.workspace_revision,
                        workspace.schedule_revision,
                        workspace.created_at,
                        workspace.updated_at,
                        int(workspace.is_sample),
                        workspace.exported_instance_revision,
                        workspace.exported_schedule_revision,
                        workspace.exported_workspace_revision,
                        workspace.exported_at,
                    ),
                )

            for key, value in sorted(state.app_metadata.items()):
                conn.execute(
                    "INSERT INTO app_meta (key, value) VALUES (?, ?)",
                    (key, value),
                )
            if state.current_workspace_id is not None:
                conn.execute(
                    "INSERT INTO app_meta (key, value) VALUES (?, ?)",
                    (CURRENT_KEY, str(state.current_workspace_id)),
                )
        return state

    def import_json(self, name: str, payload: str) -> Workspace:
        data = _load_object(payload)
        instance, schedule = parse_workspace_payload(data)
        return self.create(name, instance, schedule)

    def export_instance(self, workspace_id: int) -> str:
        return dumps(self.get(workspace_id).instance)

    def export_catalog(self, catalog_id: int) -> str:
        return dumps(self.get_catalog(catalog_id).catalog)

    def export_schedule(self, workspace_id: int) -> str:
        workspace = self.get(workspace_id)
        if workspace.schedule is None:
            raise ValueError("workspace has no schedule yet")
        return dumps(workspace.schedule)

    def mark_exported(
        self,
        workspace_id: int,
        *,
        expected_workspace_revision: int,
        clear_sample: bool = False,
    ) -> Workspace:
        """Record that the user now holds a file matching this exact state."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._workspace_row(conn, workspace_id)
            self._require_workspace_revision(row, expected_workspace_revision)
            cursor = conn.execute(
                """
                UPDATE workspaces
                SET exported_instance_revision = ?, exported_schedule_revision = ?,
                    exported_workspace_revision = ?, exported_at = ?,
                    is_sample = CASE WHEN ? THEN 0 ELSE is_sample END
                WHERE id = ? AND workspace_revision = ?
                """,
                (
                    int(row["instance_revision"]),
                    row["schedule_revision"],
                    expected_workspace_revision,
                    _now(),
                    int(clear_sample),
                    workspace_id,
                    expected_workspace_revision,
                ),
            )
        if cursor.rowcount != 1:
            raise WorkspaceConflictError("workspace changed while its file was being saved")
        return self.get(workspace_id)

    def export_workspace_rbsc(
        self,
        workspace_id: int,
        *,
        expected_workspace_revision: int | None = None,
        clear_sample: bool = False,
    ) -> str:
        """Export one workspace, plus the catalog it needs, as an RBSC document.

        One workspace to a file. Exporting the whole database made sense while a
        database was one user's whole world, but once workspaces are opened and
        closed individually it leaves nobody able to say which file holds which
        workspace at which revision.
        """
        with self.connect() as conn:
            conn.execute("BEGIN")
            workspace_row = conn.execute(
                "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
            if workspace_row is None:
                raise KeyError(workspace_id)
            if (
                expected_workspace_revision is not None
                and int(workspace_row["workspace_revision"]) != expected_workspace_revision
            ):
                raise WorkspaceConflictError("workspace changed before its file could be prepared")
            catalog_row = conn.execute(
                "SELECT * FROM catalogs WHERE id = ?", (workspace_row["catalog_id"],)
            ).fetchone()
        if catalog_row is None or workspace_row is None:
            raise KeyError(workspace_id)
        state = RBSCState(
            exported_at=_now(),
            current_workspace_id=workspace_id,
            catalogs=[_row_to_rbsc_catalog(catalog_row)],
            workspaces=[
                _row_to_rbsc_workspace(
                    workspace_row,
                    clear_sample=clear_sample,
                )
            ],
        )
        return json.dumps(state.model_dump(mode="json"), indent=2) + "\n"

    def import_workspace_rbsc(self, payload: str) -> list[Workspace]:
        """Merge the workspaces in an RBSC file onto this desk.

        Additive, unlike :meth:`restore_rbsc`, which replaces the database
        wholesale. New ids are allocated and catalog references remapped, so
        importing the same file twice yields two independent workspaces rather
        than a collision.

        What is imported is by definition identical to a file the user holds, so
        it starts life marked as downloaded.
        """
        state = self.inspect_rbsc(payload)
        if not state.workspaces:
            raise ValueError("this file contains no workspaces")
        catalogs = {record.id: record.catalog for record in state.catalogs}
        created: list[int] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._guard_capacity(len(state.workspaces), conn=conn)
            for workspace in state.workspaces:
                catalog = catalogs[workspace.catalog_id]
                catalog_id = self._put_catalog(
                    conn,
                    f"{workspace.name} constraints",
                    catalog,
                    managed=True,
                )
                exported_at = workspace.exported_at or state.exported_at
                cursor = conn.execute(
                    """
                    INSERT INTO workspaces
                        (name, academic_year, catalog_id, instance_json, schedule_json,
                         instance_revision, workspace_revision, schedule_revision,
                         created_at, updated_at, is_sample, exported_instance_revision,
                         exported_schedule_revision, exported_workspace_revision, exported_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace.name,
                        workspace.academic_year,
                        catalog_id,
                        dumps(workspace.case),
                        dumps(workspace.schedule) if workspace.schedule is not None else None,
                        workspace.instance_revision,
                        workspace.workspace_revision,
                        workspace.schedule_revision,
                        workspace.created_at,
                        _now(),
                        int(workspace.is_sample),
                        workspace.instance_revision,
                        workspace.schedule_revision,
                        workspace.workspace_revision,
                        exported_at,
                    ),
                )
                created.append(int(cursor.lastrowid))
        if created:
            self.set_current(created[0])
        return [self.get(workspace_id) for workspace_id in created]

    def export_bundle(self, workspace_id: int) -> str:
        workspace = self.get(workspace_id)
        return dumps_bundle(workspace.instance, workspace.schedule)
