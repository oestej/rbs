"""Native desktop lifecycle for one self-contained RBS document.

The desktop package treats an ``.rbsc`` file as a document, not as an export
from a durable SQLite desk.  The caller supplies an ephemeral :class:`Store`
used by the existing editors while this controller owns the file path and the
saved/dirty boundary.

The dialog protocol is intentionally tiny.  A packaging adapter can implement
it with whichever native window toolkit is bundled without pulling that toolkit
into the shared application or document model.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import unicodedata
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from rbs.desktop.settings import DesktopSettingsFile
from rbs.models.instance import SchedulerInput
from rbs.models.rbsc import portable_case_payload, portable_catalog_payload
from rbs.models.workspace import Workspace
from rbs.store import Store
from rbs.workspaces import WorkspaceController

MAX_RBSC_BYTES = 32 * 1024 * 1024


class NativeFileDialogs(Protocol):
    """The native open/save pickers supplied by the desktop packaging."""

    async def choose_open_path(self) -> str | Path | None:
        """Choose an existing ``.rbsc`` file, or return ``None`` on cancel."""
        ...

    async def choose_save_path(self, suggested_name: str) -> str | Path | None:
        """Choose a destination, or return ``None`` on cancel."""
        ...

    async def choose_settings_open_path(self) -> str | Path | None:
        """Choose an existing settings JSON file, or return ``None`` on cancel."""
        ...

    async def choose_settings_save_path(
        self,
        suggested_name: str,
    ) -> str | Path | None:
        """Choose a settings JSON destination, or return ``None`` on cancel."""
        ...


class DocumentCardinalityError(ValueError):
    """Raised when a desktop document does not contain exactly one workspace."""


class NoDocumentError(ValueError):
    """Raised when Save is requested without a workspace to save."""


class SampleWorkspaceSaveError(ValueError):
    """Raised when Save is used before sample data has been copied with Save As."""


class ExternalDocumentChangeError(RuntimeError):
    """Raised rather than overwriting a document changed outside this process."""


class DesktopDocumentController:
    """Own one desktop document backed by a caller-supplied ephemeral store.

    ``Store`` remains useful as an editing repository, but its SQLite path is an
    implementation detail of the running app.  ``path`` is the identity of the
    user's document and is changed only by a successful load or Save As.
    """

    application_name = "RBS Desktop"

    def __init__(
        self,
        store: Store,
        dialogs: NativeFileDialogs,
        *,
        max_file_bytes: int = MAX_RBSC_BYTES,
        atomic_writer: Callable[[Path, str], None] | None = None,
        recovery_path: Path | None = None,
        lock_directory: Path | None = None,
        application_settings: DesktopSettingsFile | None = None,
    ) -> None:
        if max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive")
        self.store = store
        self.dialogs = dialogs
        self.max_file_bytes = max_file_bytes
        self.path: Path | None = None
        self.generation = 0
        self._saved_fingerprint: str | None = None
        self._disk_fingerprint: str | None = None
        self._atomic_writer = atomic_writer or _atomic_write_text
        self.recovery_path = (
            None if recovery_path is None else Path(recovery_path).expanduser().resolve()
        )
        self.lock_directory = (
            None if lock_directory is None else Path(lock_directory).expanduser().resolve()
        )
        self.recovery_error: str | None = None
        self.application_settings = application_settings
        self.recovered_from: Path | None = None
        self._checkpoint_fingerprint: str | None = None
        self._recovery_suspended = 0
        self._recovery_lock = threading.RLock()
        self._select_only_workspace_if_present()
        self._unsubscribe_recovery = self.store.add_commit_listener(self._on_store_commit)

    @property
    def workspace(self) -> Workspace | None:
        """Return the document workspace, enforcing the one-document invariant."""
        workspaces = self.store.list()
        if not workspaces:
            return None
        if len(workspaces) != 1:
            raise DocumentCardinalityError(
                f"a desktop document must contain exactly one workspace; found {len(workspaces)}"
            )
        return workspaces[0]

    @property
    def dirty(self) -> bool:
        """Whether meaningful document content differs from the saved baseline.

        Database ids, timestamps, current-workspace selection, and download/export
        markers are deliberately absent from the fingerprint.  The workspace
        name, scheduling input, solution and their revision relationship are in
        it, so edits such as a rename cannot accidentally remain "clean".
        """
        workspace = self.workspace
        if workspace is None:
            return False
        if workspace.is_sample:
            return False
        if self._saved_fingerprint is None:
            return True
        return _semantic_fingerprint(workspace) != self._saved_fingerprint

    @property
    def settings_error(self) -> str | None:
        return (
            None
            if self.application_settings is None
            else self.application_settings.error
        )

    @property
    def supports_application_settings(self) -> bool:
        """Whether this controller owns an importable application settings file."""
        return self.application_settings is not None

    def sync_application_settings(self, instance: SchedulerInput) -> bool:
        """Persist settings edits independently of the workspace document."""
        if self.application_settings is None:
            return True
        return self.application_settings.capture(instance)

    async def save_settings(self) -> Path | None:
        """Export the current application settings through a native save picker."""
        settings = self._require_application_settings()
        selected = await self.dialogs.choose_settings_save_path("settings.json")
        if selected is None or str(selected).strip() == "":
            return None
        destination = _with_json_suffix(Path(selected).expanduser()).resolve()
        # Normal UI edits already synchronize automatically. Capturing again
        # makes this explicit export dependable even for callers which edited
        # the ephemeral store directly.
        settings.capture(self._require_workspace().instance)
        return settings.export_to(destination)

    async def load_settings(self) -> Workspace | None:
        """Import settings, install them, and apply them to the open document."""
        settings = self._require_application_settings()
        selected = await self.dialogs.choose_settings_open_path()
        if selected is None or str(selected).strip() == "":
            return None

        imported = settings.read_export(selected)
        workspace = self._require_workspace()
        # Validate the overlay before replacing the canonical settings file.
        revised = imported.applied_to(workspace.instance)
        previous = settings.settings
        settings.replace(imported)
        try:
            if revised != workspace.instance:
                workspace = WorkspaceController(self.store).save_instance(
                    workspace,
                    revised,
                    preserve_schedule=workspace.schedule is not None,
                )
        except BaseException:
            # Keep the automatic application file aligned with the open UI if
            # the store's optimistic-concurrency boundary rejects the update.
            try:
                settings.replace(previous)
            except OSError:
                pass
            raise
        return workspace

    def load(self, path: str | Path) -> Workspace:
        """Validate and load one ``.rbsc`` document, replacing the ephemeral store.

        Parsing and cardinality checks happen before :meth:`Store.restore_rbsc`,
        so an invalid database export leaves the open document untouched.
        """
        source = Path(path).expanduser().resolve()
        if source.suffix.lower() != ".rbsc":
            raise ValueError("RBS workspace documents must use the .rbsc extension")
        payload, disk_fingerprint = self._read_payload(source)
        workspace = self._replace_workspace_from_rbsc(payload)
        self.path = source
        self._saved_fingerprint = _semantic_fingerprint(workspace)
        self._disk_fingerprint = disk_fingerprint
        self.recovered_from = None
        self.generation += 1
        self.checkpoint()
        return workspace

    def _replace_workspace_from_rbsc(self, payload: str) -> Workspace:
        """Validate and atomically replace the ephemeral workspace state."""
        state = self.store.inspect_rbsc(payload)
        if len(state.workspaces) != 1:
            raise DocumentCardinalityError(
                "a desktop .rbsc document must contain exactly one workspace; "
                f"found {len(state.workspaces)}"
            )

        backup = self.store.export_rbsc()
        try:
            with self._suspend_recovery():
                try:
                    self.store.restore_rbsc(payload)
                    workspace = self.workspace
                    assert workspace is not None  # established by validated state
                    workspace = self._apply_application_settings(workspace)
                    self.store.set_current(workspace.id)
                    # Opening establishes both the controller's clean baseline
                    # and the legacy Store/UI's equivalent marker.
                    workspace = WorkspaceController(self.store).mark_exported(workspace)
                except BaseException:
                    self.store.restore_rbsc(backup)
                    raise
        except BaseException:
            self.checkpoint()
            raise
        return workspace

    async def open(self) -> Workspace | None:
        """Ask for a document and load it; cancellation changes nothing."""
        selected = await self.dialogs.choose_open_path()
        if selected is None or str(selected).strip() == "":
            return None
        return self.load(selected)

    async def save(self) -> Path | None:
        """Save to the bound path, asking for one when this document is untitled."""
        workspace = self._require_workspace()
        if workspace.is_sample:
            raise SampleWorkspaceSaveError(
                "Sample Data can only be saved with Save As."
            )
        if self.path is None:
            return await self.save_as()
        return self._save_to(self.path, rebind=False)

    async def save_as(self) -> Path | None:
        """Choose a new path and bind it only after an atomic write succeeds."""
        workspace = self._require_workspace()
        selected = await self.dialogs.choose_save_path(
            _suggested_filename(workspace.name, workspace.academic_year)
        )
        if selected is None or str(selected).strip() == "":
            return None
        destination = _with_rbsc_suffix(Path(selected).expanduser()).resolve()
        return self._save_to(
            destination,
            rebind=True,
            clear_sample=workspace.is_sample,
        )

    def mark_new(self) -> Workspace:
        """Adopt the store's sole workspace as a new, untitled dirty document."""
        workspace = self._require_workspace()
        with self._suspend_recovery():
            workspace = self._apply_application_settings(workspace)
            self.store.set_current(workspace.id)
            self.path = None
            self._saved_fingerprint = None
            self._disk_fingerprint = None
            self.recovered_from = None
            self.generation += 1
        self.checkpoint()
        return workspace

    def restore_recovery(self, path: str | Path) -> Workspace:
        """Adopt a stale recovery checkpoint as an untitled, dirty document.

        Recovery never silently rebinds to the workspace's previous user-facing
        path.  The user must choose Save/Save As, which prevents a recovered
        process from overwriting a file that may have changed in the meantime.
        The stale source is removed only after this process has written its own
        atomic checkpoint.
        """
        source = Path(path).expanduser().resolve()
        # Keep loading from clearing a clean checkpoint when the source and this
        # controller's recovery path happen to be the same file. Rebind the
        # restored state as dirty first, then write its checkpoint.
        with self._suspend_recovery():
            if source.suffix.lower() == ".sqlite":
                checkpoint = Store(source)
                checkpoint.init()
                workspace = self._replace_workspace_from_rbsc(
                    checkpoint.export_rbsc()
                )
            else:
                workspace = self.load(source)
            self.path = None
            self._saved_fingerprint = None
            self._disk_fingerprint = None
            self.generation += 1
            self.recovered_from = source
        replacement = self.recovery_path
        checkpointed = self.checkpoint()
        if (
            replacement is not None
            and replacement != source
            and checkpointed
            and replacement.is_file()
        ):
            _remove_recovery_checkpoint(source)
        return workspace

    def checkpoint(self) -> bool:
        """Synchronize or clear this process's private recovery snapshot.

        Checkpoint failure must not make the editor unusable; callers can show
        ``recovery_error`` and urge an immediate explicit Save instead.
        """
        with self._recovery_lock:
            if self.recovery_path is None or self._recovery_suspended:
                self.recovery_error = None
                return True
            try:
                workspace = self.workspace
                if workspace is None:
                    _remove_recovery_checkpoint(self.recovery_path)
                    self._checkpoint_fingerprint = None
                else:
                    fingerprint = _semantic_fingerprint(workspace)
                    if (
                        fingerprint == self._checkpoint_fingerprint
                        and self.recovery_path.is_file()
                    ):
                        self.recovery_error = None
                        return True
                    if self.recovery_path.suffix.lower() == ".sqlite":
                        _atomic_sqlite_checkpoint(
                            self.store.path,
                            self.recovery_path,
                        )
                    else:
                        # Read old recovery drafts and preserve compatibility
                        # with callers that still supply an .rbsc target.
                        payload = self.store.export_workspace_rbsc(workspace.id)
                        _atomic_write_text(self.recovery_path, payload)
                    self._checkpoint_fingerprint = fingerprint
            except Exception as exc:
                self.recovery_error = str(exc)
                return False
            self.recovery_error = None
            return True

    def clear_recovery_checkpoint(self) -> bool:
        """Remove this process's checkpoint after an orderly application exit."""
        if self.recovery_path is None:
            return True
        with self._recovery_lock:
            try:
                _remove_recovery_checkpoint(self.recovery_path)
            except OSError as exc:
                self.recovery_error = str(exc)
                return False
            self._checkpoint_fingerprint = None
            self.recovery_error = None
            return True

    def new(self, *, sample: bool = False) -> Workspace:
        """Replace the current document with a blank or demonstration workspace."""
        from rbs.catalog import current_blank_instance, current_sample_instance

        backup = self.store.export_rbsc()
        try:
            with self._suspend_recovery():
                try:
                    for workspace in self.store.list():
                        WorkspaceController(self.store).delete(workspace)
                    instance = (
                        current_sample_instance() if sample else current_blank_instance()
                    )
                    name = f"Sample {instance.academic_year}" if sample else "Untitled"
                    workspace = self.store.create(
                        name,
                        instance,
                        is_sample=sample,
                    )
                    # Re-import the single-workspace payload to discard catalogs
                    # left behind by the old ephemeral document.
                    payload = self.store.export_workspace_rbsc(workspace.id)
                    self.store.restore_rbsc(payload)
                except BaseException:
                    self.store.restore_rbsc(backup)
                    raise
        except BaseException:
            self.checkpoint()
            raise
        return self.mark_new()

    def close(self) -> None:
        """Close the ephemeral document without modifying its bound file."""
        workspace = self.workspace
        if workspace is None:
            return
        with self._suspend_recovery():
            WorkspaceController(self.store).delete(workspace)
            self.path = None
            self._saved_fingerprint = None
            self._disk_fingerprint = None
            self.recovered_from = None
            self.generation += 1
        self.checkpoint()

    def _on_store_commit(self) -> None:
        """Checkpoint every committed mutation through the Store boundary."""
        if not self._recovery_suspended:
            self.checkpoint()

    @contextmanager
    def _suspend_recovery(self):
        """Keep a good draft while a multi-transaction replacement is in flight."""
        with self._recovery_lock:
            self._recovery_suspended += 1
        try:
            yield
        finally:
            with self._recovery_lock:
                self._recovery_suspended -= 1

    def _save_to(
        self,
        destination: Path,
        *,
        rebind: bool,
        clear_sample: bool = False,
    ) -> Path:
        # All RBS Desktop processes serialize saves for this destination. The
        # disk fingerprint is rechecked only after the lock is held, closing
        # the race where two windows both validated the same old file before
        # either replaced it.
        with _exclusive_document_lock(destination, self.lock_directory):
            if self.path == destination:
                self._ensure_bound_file_is_unchanged()

            workspace = self._require_workspace()
            payload = self.store.export_workspace_rbsc(
                workspace.id,
                expected_workspace_revision=workspace.workspace_revision,
                clear_sample=clear_sample,
            )
            self._atomic_writer(destination, payload)

            if rebind:
                self.path = destination
            self._disk_fingerprint = _payload_fingerprint(payload)

            # This marker keeps compatibility with the current Store/UI, but is
            # not part of desktop dirty tracking and is written only after the
            # file write succeeds.
            try:
                workspace = WorkspaceController(self.store).mark_exported(
                    workspace,
                    clear_sample=clear_sample,
                )
            except KeyError:
                pass
            self._saved_fingerprint = _semantic_fingerprint(workspace)
            self.checkpoint()
        return destination

    def _read_payload(self, path: Path) -> tuple[str, str]:
        size = path.stat().st_size
        if size > self.max_file_bytes:
            raise ValueError(
                f"that file is larger than the {self.max_file_bytes // (1024 * 1024)} MB limit"
            )
        raw = path.read_bytes()
        if len(raw) > self.max_file_bytes:
            raise ValueError(
                f"that file is larger than the {self.max_file_bytes // (1024 * 1024)} MB limit"
            )
        try:
            payload = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("that file is not a UTF-8 RBS document") from exc
        return payload, hashlib.sha256(raw).hexdigest()

    def _ensure_bound_file_is_unchanged(self) -> None:
        assert self.path is not None
        if self._disk_fingerprint is None:
            return
        try:
            size = self.path.stat().st_size
            current = (
                None
                if size > self.max_file_bytes
                else hashlib.sha256(self.path.read_bytes()).hexdigest()
            )
        except FileNotFoundError:
            current = None
        if current != self._disk_fingerprint:
            raise ExternalDocumentChangeError(
                "The file changed outside RBS Desktop. Use Save as… to keep this "
                "draft, or reopen the file to use the version on disk."
            )

    def _select_only_workspace_if_present(self) -> None:
        workspace = self.workspace
        if workspace is not None:
            self.store.set_current(workspace.id)

    def _require_workspace(self) -> Workspace:
        workspace = self.workspace
        if workspace is None:
            raise NoDocumentError("there is no RBS document to save")
        return workspace

    def _require_application_settings(self) -> DesktopSettingsFile:
        if self.application_settings is None:
            raise RuntimeError("application settings are unavailable")
        return self.application_settings

    def _apply_application_settings(self, workspace: Workspace) -> Workspace:
        if self.application_settings is None:
            return workspace
        revised = self.application_settings.apply(workspace.instance)
        if revised == workspace.instance:
            return workspace
        return WorkspaceController(self.store).save_instance(
            workspace,
            revised,
            preserve_schedule=workspace.schedule is not None,
        )


def _semantic_fingerprint(workspace: Workspace) -> str:
    """Hash content that would change the meaning of a workspace document."""
    latest_schedule = workspace.latest_schedule
    schedule = latest_schedule.model_dump(mode="json") if latest_schedule is not None else None
    if schedule is not None:
        schedule["meta"].pop("source_instance_revision", None)
    semantic = {
        "name": workspace.name,
        "academic_year": workspace.academic_year,
        "is_sample": workspace.is_sample,
        "case": portable_case_payload(workspace.instance.scheduling_case()),
        "catalog": portable_catalog_payload(
            workspace.instance.constraint_catalog()
        ),
        "schedule": schedule,
        "schedule_is_current": workspace.schedule is not None,
    }
    canonical = json.dumps(
        semantic,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _payload_fingerprint(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _suggested_filename(name: str, academic_year: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", f"{name} {academic_year}").strip("-")
    return f"{slug or 'workspace'}.rbsc"


def _with_rbsc_suffix(path: Path) -> Path:
    if path.suffix.lower() == ".rbsc":
        return path
    return path.with_suffix(".rbsc")


def _with_json_suffix(path: Path) -> Path:
    if path.suffix.lower() == ".json":
        return path
    return path.with_suffix(".json")


def _atomic_write_text(destination: Path, payload: str) -> None:
    """Replace ``destination`` atomically without risking the previous file."""
    parent = destination.parent
    if not parent.is_dir():
        raise FileNotFoundError(f"destination folder does not exist: {parent}")

    existing_mode = None
    try:
        existing_mode = destination.stat().st_mode
    except FileNotFoundError:
        pass

    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if existing_mode is not None:
            os.chmod(temporary, existing_mode)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_sqlite_checkpoint(source: Path, destination: Path) -> None:
    """Replace a recovery checkpoint with one consistent SQLite snapshot."""
    parent = destination.parent
    if not parent.is_dir():
        raise FileNotFoundError(f"recovery folder does not exist: {parent}")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    source_connection = None
    checkpoint_connection = None
    try:
        source_connection = sqlite3.connect(source, timeout=30.0)
        checkpoint_connection = sqlite3.connect(temporary, timeout=30.0)
        source_connection.backup(checkpoint_connection)
        result = checkpoint_connection.execute("PRAGMA quick_check").fetchone()
        if result is None or result[0] != "ok":
            raise sqlite3.DatabaseError("recovery checkpoint failed its integrity check")
        checkpoint_connection.commit()
        checkpoint_connection.close()
        checkpoint_connection = None
        source_connection.close()
        source_connection = None
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        _remove_sqlite_sidecars(destination)
        os.replace(temporary, destination)
    except BaseException:
        _remove_sqlite_files(temporary)
        raise
    finally:
        if checkpoint_connection is not None:
            checkpoint_connection.close()
        if source_connection is not None:
            source_connection.close()


def _remove_recovery_checkpoint(path: Path) -> None:
    """Remove one recovery file and any SQLite sidecars it acquired."""
    if path.suffix.lower() != ".sqlite":
        path.unlink(missing_ok=True)
        return
    _remove_sqlite_files(path)


def _remove_sqlite_files(path: Path) -> None:
    _remove_sqlite_sidecars(path)
    path.unlink(missing_ok=True)


def _remove_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


@contextmanager
def _exclusive_document_lock(destination: Path, lock_directory: Path | None):
    """Hold a stable, cross-process lock for one document destination."""
    lock_path = _document_lock_path(destination, lock_directory)
    if lock_directory is not None:
        lock_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(lock_directory, 0o700)

    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        if locked:
            os.lseek(descriptor, 0, os.SEEK_SET)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _document_lock_path(destination: Path, lock_directory: Path | None) -> Path:
    """Return one lock identity for all spellings of the same local path."""
    if lock_directory is None:
        return destination.with_name(f".{destination.name}.rbs-lock")

    # Default APFS is case- and Unicode-normalization-insensitive while
    # Path.resolve preserves the caller's spelling. Sharing a lock for two
    # genuinely distinct files on a case-sensitive Mac volume is a safe,
    # conservative over-serialization.
    identity = unicodedata.normalize("NFC", str(destination.resolve())).casefold()
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return lock_directory / f"{digest}.lock"
