"""SQLite constraint-catalog storage (mixin for Store)."""

from __future__ import annotations

import sqlite3

from rbs.emit import dumps
from rbs.models.catalog import ConstraintCatalog
from rbs.models.workspace import Workspace, WorkspaceConflictError
from rbs.store_support import CatalogRecord, _catalog_hash, _load_object, _now


class StoreCatalogMixin:
    """Catalog CRUD backing the Store workspace desk."""

    def _put_catalog(
        self,
        conn: sqlite3.Connection,
        name: str,
        catalog: ConstraintCatalog,
        *,
        managed: bool,
    ) -> int:
        payload = dumps(catalog)
        digest = _catalog_hash(catalog)
        existing = conn.execute(
            "SELECT id FROM catalogs WHERE content_hash = ?", (digest,)
        ).fetchone()
        if existing is not None:
            if not managed:
                conn.execute(
                    "UPDATE catalogs SET managed = 0 WHERE id = ?",
                    (existing["id"],),
                )
            return int(existing["id"])
        now = _now()
        cursor = conn.execute(
            """
            INSERT INTO catalogs
                (name, schema_version, content_hash, catalog_json, managed,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name.strip() or "Untitled constraints",
                catalog.schema_version,
                digest,
                payload,
                int(managed),
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def save_catalog(self, name: str, catalog: ConstraintCatalog) -> CatalogRecord:
        with self.connect() as conn:
            catalog_id = self._put_catalog(conn, name, catalog, managed=False)
        return self.get_catalog(catalog_id)

    def _discard_unreferenced_catalog(
        self,
        conn: sqlite3.Connection,
        catalog_id: int,
    ) -> None:
        """Collect an internal immutable catalog after its last workspace leaves."""
        conn.execute(
            """
            DELETE FROM catalogs
            WHERE id = ? AND managed = 1
              AND NOT EXISTS (
                  SELECT 1 FROM workspaces WHERE catalog_id = catalogs.id
              )
            """,
            (catalog_id,),
        )

    def import_catalog_json(self, name: str, payload: str) -> CatalogRecord:
        catalog = ConstraintCatalog.model_validate(_load_object(payload))
        return self.save_catalog(name, catalog)

    def list_catalogs(self) -> list[CatalogRecord]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM catalogs ORDER BY updated_at DESC, id").fetchall()
        return [self._row_to_catalog(row) for row in rows]

    def get_catalog(self, catalog_id: int) -> CatalogRecord:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM catalogs WHERE id = ?", (catalog_id,)).fetchone()
        if row is None:
            raise KeyError(catalog_id)
        return self._row_to_catalog(row)

    def set_catalog(
        self,
        workspace_id: int,
        catalog_id: int,
        *,
        expected_workspace_revision: int,
    ) -> Workspace:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._workspace_row(conn, workspace_id)
            self._require_workspace_revision(row, expected_workspace_revision)
            catalog_row = conn.execute(
                "SELECT * FROM catalogs WHERE id = ?",
                (catalog_id,),
            ).fetchone()
            if catalog_row is None:
                raise KeyError(catalog_id)
            workspace = self._row_to_workspace(row)
            catalog = self._row_to_catalog(catalog_row).catalog
            catalog.apply(workspace.instance.scheduling_case())
            previous_catalog_id = int(row["catalog_id"])
            cursor = conn.execute(
                """
                UPDATE workspaces
                SET catalog_id = ?, instance_revision = instance_revision + 1,
                    workspace_revision = workspace_revision + 1, updated_at = ?
                WHERE id = ? AND workspace_revision = ?
                """,
                (
                    catalog_id,
                    _now(),
                    workspace_id,
                    expected_workspace_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise WorkspaceConflictError("workspace changed before catalog update")
            self._discard_unreferenced_catalog(conn, previous_catalog_id)
        return self.get(workspace_id)

    def _row_to_catalog(self, row: sqlite3.Row) -> CatalogRecord:
        return CatalogRecord(
            id=row["id"],
            name=row["name"],
            catalog=ConstraintCatalog.model_validate_json(row["catalog_json"]),
            content_hash=row["content_hash"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            managed=bool(row["managed"]) if "managed" in row.keys() else False,
        )
