"""SQLite schema and stable query fragments for workspace storage."""

from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalogs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    catalog_json TEXT NOT NULL,
    managed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    academic_year TEXT NOT NULL,
    catalog_id INTEGER REFERENCES catalogs(id),
    instance_json TEXT NOT NULL,
    schedule_json TEXT,
    instance_revision INTEGER NOT NULL DEFAULT 1,
    workspace_revision INTEGER NOT NULL DEFAULT 1,
    schedule_revision INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    is_sample INTEGER NOT NULL DEFAULT 0,
    exported_instance_revision INTEGER,
    exported_schedule_revision INTEGER,
    exported_workspace_revision INTEGER,
    exported_at TEXT
);

CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

CURRENT_KEY = "current_workspace_id"
CLINIC_CLOSURES_V1_KEY = "clinic_closure_days_v1"

_WORKSPACE_SELECT = """
SELECT w.*, c.name AS catalog_name, c.catalog_json AS catalog_json
FROM workspaces AS w
JOIN catalogs AS c ON c.id = w.catalog_id
"""
