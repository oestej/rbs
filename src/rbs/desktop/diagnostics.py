"""Platform-neutral desktop log retention, preferences, and support export."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import platform
import re
import stat
import sys
import tempfile
import threading
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from rbs import __version__
from rbs.logging import LOG_SCHEMA_VERSION, LoggingRuntime, get_logger
from rbs.models.common import StrictModel

DIAGNOSTICS_SCHEMA_VERSION = 1
LOG_EXPORT_SCHEMA_VERSION = 1
LOG_RETENTION_DAYS = 14
LOG_RETENTION_BYTES = 50 * 1024 * 1024
VERBOSE_ROUTE = "/_rbs/desktop/diagnostics/verbose"
EXPORT_ROUTE = "/_rbs/desktop/diagnostics/export"

_LOG_FILE = re.compile(
    r"^rbs-(?P<run>[0-9a-f-]{36})-[a-z0-9_-]+-[0-9]+\.jsonl(?:\.[0-9]+)?$"
)
_ACTIVE_MARKER = re.compile(
    r"^\.active-(?P<run>[0-9a-f-]{36})-(?P<pid>[0-9]+)$"
)


class DesktopDiagnosticsSettings(StrictModel):
    schema_version: Literal[1] = DIAGNOSTICS_SCHEMA_VERSION
    verbose_logging: bool = False


class DesktopDiagnosticsSettingsFile:
    """Atomic, machine-local settings intentionally separate from app settings."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = None if path is None else Path(path).expanduser().resolve()
        self.settings = DesktopDiagnosticsSettings()
        self.error_code: str | None = None
        self._lock = threading.RLock()
        self._load()

    @property
    def verbose_logging(self) -> bool:
        with self._lock:
            return self.settings.verbose_logging

    def set_verbose(self, enabled: bool) -> None:
        replacement = DesktopDiagnosticsSettings(verbose_logging=bool(enabled))
        with self._lock:
            if self.path is not None:
                _atomic_write(
                    self.path,
                    json.dumps(replacement.model_dump(mode="json"), indent=2) + "\n",
                )
            self.settings = replacement
            self.error_code = None

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            self.error_code = "diagnostics_read_failed"
            return
        if len(raw) > 4096:
            self.error_code = "diagnostics_too_large"
            return
        try:
            self.settings = DesktopDiagnosticsSettings.model_validate_json(raw)
        except (ValidationError, ValueError):
            self.settings = DesktopDiagnosticsSettings()
            self.error_code = "diagnostics_invalid"


class DiagnosticsFileDialogs(Protocol):
    async def choose_log_export_path(self, suggested_name: str) -> str | Path | None:
        """Choose a ZIP destination, or return ``None`` on cancel."""
        ...


class NativeDiagnosticsMenu(Protocol):
    """Platform adapter seam for a toolkit's native menu configuration."""

    def install(self, native_config: Any) -> None:
        """Install diagnostics commands into a toolkit's native configuration."""
        ...


@dataclass(frozen=True, slots=True)
class RetentionResult:
    removed_count: int
    removed_bytes: int


@dataclass(frozen=True, slots=True)
class ExportResult:
    path: Path
    file_count: int
    size_bytes: int


class DesktopDiagnosticsService:
    """Commands shared by the native menu and its private HTTP bridge."""

    def __init__(
        self,
        runtime: LoggingRuntime,
        settings: DesktopDiagnosticsSettingsFile,
        log_directory: str | Path,
        dialogs: DiagnosticsFileDialogs,
    ) -> None:
        self.runtime = runtime
        self.settings = settings
        self.log_directory = Path(log_directory).expanduser().resolve()
        self.dialogs = dialogs
        self._command_lock = threading.RLock()

    @property
    def verbose_logging(self) -> bool:
        return self.settings.verbose_logging

    def toggle_verbose(self) -> bool:
        with self._command_lock:
            enabled = not self.settings.verbose_logging
            self.settings.set_verbose(enabled)
            self.runtime.set_level("DEBUG" if enabled else "INFO")
            get_logger("desktop.diagnostics").info(
                "logging.verbose_changed",
                enabled=enabled,
            )
            return enabled

    async def export_logs(self) -> ExportResult | None:
        suggested = f"rbs-logs-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        selected = await self.dialogs.choose_log_export_path(suggested)
        if selected is None or not str(selected).strip():
            get_logger("desktop.diagnostics").info("logging.export_cancelled")
            return None
        destination = Path(selected).expanduser()
        if destination.suffix.lower() != ".zip":
            destination = destination.with_suffix(".zip")
        get_logger("desktop.diagnostics").info("logging.export_started")
        result = await asyncio.to_thread(
            export_log_bundle,
            self.log_directory,
            destination.resolve(),
            run_id=self.runtime.run_id,
            runtime=self.runtime,
        )
        get_logger("desktop.diagnostics").info(
            "logging.export_completed",
            file_count=result.file_count,
            size_bytes=result.size_bytes,
        )
        return result

    def install(self, app: Any) -> None:
        from starlette.responses import JSONResponse

        if getattr(app.state, "rbs_desktop_diagnostics_installed", False):
            return

        @app.post(VERBOSE_ROUTE, include_in_schema=False)
        async def toggle_verbose() -> JSONResponse:
            try:
                enabled = self.toggle_verbose()
            except OSError:
                get_logger("desktop.diagnostics").error(
                    "logging.verbose_change_failed",
                    error_code="preference_write_failed",
                    exc_info=True,
                )
                return JSONResponse({"status": "error"}, status_code=500)
            return JSONResponse({"status": "ok", "verbose": enabled})

        @app.post(EXPORT_ROUTE, include_in_schema=False)
        async def export_logs() -> JSONResponse:
            try:
                result = await self.export_logs()
            except Exception:
                get_logger("desktop.diagnostics").error(
                    "logging.export_failed",
                    error_code="bundle_write_failed",
                    exc_info=True,
                )
                return JSONResponse({"status": "error"}, status_code=500)
            if result is None:
                return JSONResponse({"status": "cancelled"})
            return JSONResponse({"status": "ok"})

        app.state.rbs_desktop_diagnostics_installed = True


def default_diagnostics_path(*, home: Path | None = None) -> Path:
    home = Path.home() if home is None else home
    return home / "Library" / "Application Support" / "RBS Desktop" / "diagnostics.json"


def default_log_directory(*, home: Path | None = None) -> Path:
    home = Path.home() if home is None else home
    return home / "Library" / "Logs" / "RBS Desktop"


def prune_logs(
    directory: str | Path,
    *,
    now: datetime | None = None,
    retention_days: int = LOG_RETENTION_DAYS,
    max_bytes: int = LOG_RETENTION_BYTES,
) -> RetentionResult:
    root = Path(directory)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _directory_lock(root):
        return _prune_unlocked(
            root,
            now=_as_utc(now),
            retention_days=retention_days,
            max_bytes=max_bytes,
        )


def export_log_bundle(
    directory: str | Path,
    destination: str | Path,
    *,
    run_id: str,
    runtime: LoggingRuntime | None = None,
    now: datetime | None = None,
    max_bytes: int = LOG_RETENTION_BYTES,
) -> ExportResult:
    root = Path(directory)
    destination = Path(destination)
    current = _as_utc(now)
    destination.parent.mkdir(parents=True, exist_ok=True)
    snapshots: list[tuple[str, bytes, str | None, str | None]] = []
    with _directory_lock(root):
        _prune_unlocked(
            root,
            now=current,
            retention_days=LOG_RETENTION_DAYS,
            max_bytes=LOG_RETENTION_BYTES,
        )
        if runtime is not None:
            runtime.flush()
        total = 0
        for source in sorted(_log_files(root), key=_mtime, reverse=True):
            try:
                payload = _complete_jsonl(source.read_bytes())
            except OSError:
                continue
            if not payload or total + len(payload) > max_bytes:
                continue
            first, last = _timestamp_range(payload)
            snapshots.append((source.name, payload, first, last))
            total += len(payload)

    manifest_files = [
        {
            "name": name,
            "size_bytes": len(payload),
            "first_timestamp": first,
            "last_timestamp": last,
        }
        for name, payload, first, last in snapshots
    ]
    manifest = {
        "schema_version": LOG_EXPORT_SCHEMA_VERSION,
        "exported_at": current.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "app_version": __version__,
        "python_version": platform.python_version(),
        "platform": sys.platform,
        "os_version": platform.mac_ver()[0],
        "architecture": platform.machine(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "run_id": run_id,
        "files": manifest_files,
        "total_log_bytes": total,
    }

    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
            for name, payload, _first, _last in snapshots:
                archive.writestr(f"logs/{name}", payload)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return ExportResult(path=destination, file_count=len(snapshots), size_bytes=total)


def _prune_unlocked(
    root: Path,
    *,
    now: datetime,
    retention_days: int,
    max_bytes: int,
) -> RetentionResult:
    active = _active_run_ids(root)
    cutoff = now.timestamp() - timedelta(days=retention_days).total_seconds()
    removed_count = 0
    removed_bytes = 0
    files = _log_files(root)
    for path in files:
        match = _LOG_FILE.fullmatch(path.name)
        if match is None or match.group("run") in active or _mtime(path) >= cutoff:
            continue
        size = _size(path)
        try:
            path.unlink()
        except OSError:
            continue
        removed_count += 1
        removed_bytes += size

    files = _log_files(root)
    total = sum(_size(path) for path in files)
    inactive = sorted(
        (
            path
            for path in files
            if (_LOG_FILE.fullmatch(path.name) is not None)
            and _LOG_FILE.fullmatch(path.name).group("run") not in active  # type: ignore[union-attr]
        ),
        key=_mtime,
    )
    for path in inactive:
        if total <= max_bytes:
            break
        size = _size(path)
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        removed_count += 1
        removed_bytes += size
    return RetentionResult(removed_count=removed_count, removed_bytes=removed_bytes)


def _active_run_ids(root: Path) -> set[str]:
    active: set[str] = set()
    for marker in root.glob(".active-*"):
        match = _ACTIVE_MARKER.fullmatch(marker.name)
        if match is None:
            continue
        pid = int(match.group("pid"))
        if _process_is_alive(pid):
            active.add(match.group("run"))
        else:
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass
    return active


def _process_is_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _complete_jsonl(payload: bytes) -> bytes:
    end = payload.rfind(b"\n")
    if end < 0:
        return b""
    accepted: list[bytes] = []
    for line in payload[: end + 1].splitlines():
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if (
            isinstance(value, dict)
            and value.get("schema_version") == LOG_SCHEMA_VERSION
            and value.get("service") == "rbs"
        ):
            accepted.append(line)
    return b"\n".join(accepted) + (b"\n" if accepted else b"")


def _timestamp_range(payload: bytes) -> tuple[str | None, str | None]:
    timestamps = []
    for line in payload.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        timestamp = value.get("timestamp") if isinstance(value, dict) else None
        if isinstance(timestamp, str):
            timestamps.append(timestamp)
    return (
        timestamps[0] if timestamps else None,
        timestamps[-1] if timestamps else None,
    )


def _log_files(root: Path) -> list[Path]:
    return [path for path in root.iterdir() if path.is_file() and _LOG_FILE.fullmatch(path.name)]


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


@contextmanager
def _directory_lock(root: Path) -> Iterator[None]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    lock_path = root / ".diagnostics.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_write(destination: Path, payload: str) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # The completed file is still atomically visible on filesystems which
        # do not permit syncing directory handles.
        pass


__all__ = [
    "default_diagnostics_path",
    "default_log_directory",
    "export_log_bundle",
    "prune_logs",
    "DesktopDiagnosticsService",
    "DesktopDiagnosticsSettings",
    "DesktopDiagnosticsSettingsFile",
    "DiagnosticsFileDialogs",
    "ExportResult",
    "RetentionResult",
    "NativeDiagnosticsMenu",
]
