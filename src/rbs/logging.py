"""One privacy-preserving logging contract for every RBS runtime.

The application has several process boundaries (the hosted server, the native
webview and the standalone solver), so "one logger" cannot mean one Python
handler.  It means one schema and one processor chain installed independently
in each process, with an explicit sink selected by the entry point.
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import math
import os
import re
import stat
import sys
import threading
import traceback
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Literal

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars, merge_contextvars

from rbs import __version__

LOG_SCHEMA_VERSION = 1
LOG_LEVEL_ENV = "RBS_LOG_LEVEL"
RUN_ID_ENV = "RBS_RUN_ID"
PARENT_RUNTIME_ENV = "RBS_PARENT_RUNTIME"
DEFAULT_LOG_LEVEL = "INFO"
LOG_CHUNK_BYTES = 5 * 1024 * 1024

RuntimeName = Literal["cloud", "local", "desktop", "cli", "solver", "native"]
DestinationName = Literal["stdout", "stderr", "desktop"]

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
_EVENT_CODE = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9_]*)+$")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_SAFE_COMPONENT = re.compile(r"[^a-z0-9_-]+")
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_CAPABILITY = re.compile(r"(?i)(_rbs_capability|token|secret)=([^\s&#]+)")
_URL = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_POSIX_PATH = re.compile(r"(?<![\w:])/(?:[^\s\"']+)")
_WINDOWS_PATH = re.compile(r"(?i)\b[A-Z]:\\[^\s\"']+")
_DOCUMENT_FILE = re.compile(
    r"(?i)(?<![\w.-])[\w .-]+\.(?:rbsc|sqlite|json|csv|pdf)(?![\w.-])"
)

_APPLICATION_FIELDS = frozenset(
    {
        "age_days",
        "asset",
        "column",
        "count",
        "duration_ms",
        "enabled",
        "engine",
        "error_code",
        "error_name",
        "evicted_count",
        "exit_code",
        "file_count",
        "kind",
        "line",
        "num_workers",
        "outcome",
        "queue_depth",
        "reason",
        "removed_count",
        "setting",
        "size_bytes",
        "source",
        "status_code",
        "stderr_bytes",
        "stderr_sha256",
        "time_limit_seconds",
    }
)
_CONTEXT_FIELDS = frozenset({"request_id", "session_id", "operation_id", "solve_id"})


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Configuration selected once by an executable's composition root."""

    runtime: RuntimeName
    component: str
    destination: DestinationName
    level: str | int | None = None
    run_id: str | None = None
    log_directory: Path | None = None
    stream: IO[str] | None = None


class LoggingRuntime:
    """Mutable controls and lifecycle for an installed logging pipeline."""

    def __init__(
        self,
        *,
        config: LoggingConfig,
        handler: logging.Handler,
        run_id: str,
        level: int,
        previous_handlers: tuple[logging.Handler, ...],
        previous_level: int,
        previous_structlog_config: dict[str, Any],
        active_marker: Path | None = None,
    ) -> None:
        self.config = config
        self.handler = handler
        self.run_id = run_id
        self.level = level
        self.previous_handlers = previous_handlers
        self.previous_level = previous_level
        self.previous_structlog_config = previous_structlog_config
        self.active_marker = active_marker
        self._closed = False
        self._lock = threading.RLock()

    def set_level(self, level: str | int) -> int:
        """Apply a validated level immediately and return its numeric value."""
        resolved, _invalid = resolve_log_level(level)
        with self._lock:
            self.level = resolved
            self.handler.setLevel(resolved)
            logging.getLogger().setLevel(resolved)
        return resolved

    def flush(self) -> None:
        with self._lock:
            self.handler.acquire()
            try:
                self.handler.flush()
            finally:
                self.handler.release()

    def close(self) -> None:
        global _runtime
        with self._lock:
            if self._closed:
                return
            self._closed = True
            root = logging.getLogger()
            if self.handler in root.handlers:
                root.removeHandler(self.handler)
            self.handler.close()
            for previous in self.previous_handlers:
                if previous not in root.handlers:
                    root.addHandler(previous)
            root.setLevel(self.previous_level)
            if self.active_marker is not None:
                try:
                    self.active_marker.unlink(missing_ok=True)
                except OSError:
                    pass
            with _configuration_lock:
                if _runtime is self:
                    _runtime = None
                    clear_contextvars()
                    structlog.configure(**self.previous_structlog_config)

    def child_environment(self) -> dict[str, str]:
        """Return the inherited environment with safe correlation data added."""
        environment = dict(os.environ)
        environment[RUN_ID_ENV] = self.run_id
        environment[PARENT_RUNTIME_ENV] = self.config.runtime
        environment[LOG_LEVEL_ENV] = logging.getLevelName(self.level)
        return environment


_configuration_lock = threading.RLock()
_runtime: LoggingRuntime | None = None


def _drop_unconfigured_log(
    _logger: Any,
    _method_name: str,
    _event_dict: dict[str, Any],
) -> None:
    """Keep library use quiet until an executable selects a log destination."""
    raise structlog.DropEvent


def _configure_silent_structlog() -> None:
    """Prevent structlog's development renderer from bypassing RBS logging."""
    structlog.configure(
        processors=(_drop_unconfigured_log,),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


def resolve_log_level(value: str | int | None) -> tuple[int, bool]:
    """Resolve a level, falling back safely instead of disabling diagnostics."""
    if isinstance(value, int):
        if value in _LEVELS.values():
            return value, False
        return logging.INFO, True
    raw = os.environ.get(LOG_LEVEL_ENV, DEFAULT_LOG_LEVEL) if value is None else value
    resolved = _LEVELS.get(str(raw).strip().upper())
    return (resolved, False) if resolved is not None else (logging.INFO, True)


def configure_logging(config: LoggingConfig) -> LoggingRuntime:
    """Install RBS's processor chain and sole root handler for this process.

    Repeated calls replace the previously installed RBS handler instead of
    stacking duplicates. This also makes entry points safe to exercise more
    than once in an embedded process or test suite.
    """
    global _runtime
    with _configuration_lock:
        if _runtime is not None:
            _runtime.close()

        level, invalid_level = resolve_log_level(config.level)
        run_id = _valid_run_id(config.run_id or os.environ.get(RUN_ID_ENV))
        root = logging.getLogger()
        previous_handlers = tuple(root.handlers)
        previous_level = root.level
        previous_structlog_config = dict(structlog.get_config())

        active_marker: Path | None = None
        if config.destination == "desktop":
            if config.log_directory is None:
                raise ValueError("desktop logging requires log_directory")
            handler, active_marker = _desktop_handler(
                Path(config.log_directory), run_id, config.component
            )
        else:
            stream = config.stream
            if stream is None:
                stream = sys.stdout if config.destination == "stdout" else sys.stderr
            handler = logging.StreamHandler(stream)

        for existing in previous_handlers:
            root.removeHandler(existing)

        handler.setLevel(level)
        formatter = structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=(
                structlog.stdlib.add_log_level,
                structlog.stdlib.add_logger_name,
            ),
            processors=(
                _normalizer(config.runtime, config.component, run_id),
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(sort_keys=True, ensure_ascii=False),
            ),
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)
        root.setLevel(level)

        # Uvicorn installs non-propagating handlers by default. Remove any that
        # already exist so every dependency enters the root ProcessorFormatter.
        for name in (
            "uvicorn",
            "uvicorn.error",
            "uvicorn.access",
            "nicegui",
            "pywebview",
        ):
            dependency = logging.getLogger(name)
            dependency.handlers.clear()
            dependency.propagate = True
        logging.getLogger("uvicorn.access").disabled = True
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.captureWarnings(True)

        structlog.configure(
            processors=(
                merge_contextvars,
                structlog.stdlib.filter_by_level,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ),
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=False,
        )
        clear_contextvars()
        runtime = LoggingRuntime(
            config=config,
            handler=handler,
            run_id=run_id,
            level=level,
            previous_handlers=previous_handlers,
            previous_level=previous_level,
            previous_structlog_config=previous_structlog_config,
            active_marker=active_marker,
        )
        _runtime = runtime

        logger = get_logger("logging")
        if invalid_level:
            logger.warning("logging.level_invalid", setting=LOG_LEVEL_ENV)
        return runtime


def current_runtime() -> LoggingRuntime | None:
    return _runtime


def get_logger(component: str = "application") -> structlog.stdlib.BoundLogger:
    safe = _SAFE_COMPONENT.sub("_", component.strip().lower()).strip("_") or "application"
    return structlog.get_logger(f"rbs.{safe}")


@contextmanager
def log_context(**values: str | None):
    """Bind validated opaque IDs for one operation and restore prior context."""
    accepted = {
        key: value.lower()
        for key, value in values.items()
        if key in _CONTEXT_FIELDS and isinstance(value, str) and _UUID.fullmatch(value)
    }
    tokens = bind_contextvars(**accepted)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


class LoggingContextMiddleware:
    """Bind a fresh request ID without producing generic access logs."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        clear_contextvars()
        request_id = str(uuid.uuid4())
        bind_contextvars(request_id=request_id)
        try:
            await self.app(scope, receive, send)
        except Exception:
            get_logger("http").exception("http.unhandled_error")
            raise
        finally:
            clear_contextvars()


def install_asgi_logging(app: Any) -> None:
    """Install request context exactly once on a FastAPI/NiceGUI app."""
    if getattr(app.state, "rbs_logging_context_installed", False):
        return
    app.add_middleware(LoggingContextMiddleware)
    app.state.rbs_logging_context_installed = True


def relay_solver_stderr(stderr: str | bytes, *, exit_code: int | None = None) -> None:
    """Validate child JSONL records and relay only their privacy-safe shape."""
    if not stderr:
        return
    raw = stderr if isinstance(stderr, bytes) else stderr.encode("utf-8", errors="replace")
    logger = get_logger("solver.transport")
    malformed = False
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if not _valid_relay_record(record):
                raise ValueError
        except (json.JSONDecodeError, TypeError, ValueError):
            malformed = True
            continue
        fields = {
            key: record[key]
            for key in _APPLICATION_FIELDS
            if key in record and _safe_scalar(record[key])
        }
        for key in _CONTEXT_FIELDS:
            value = record.get(key)
            if isinstance(value, str) and _UUID.fullmatch(value):
                fields[key] = value
        level = str(record.get("level", "info")).lower()
        method = getattr(logger, level, logger.info)
        method(str(record["event"]), **fields)
    if malformed:
        logger.warning(
            "solver.stderr_rejected",
            stderr_bytes=len(raw),
            stderr_sha256=hashlib.sha256(raw).hexdigest(),
            exit_code=exit_code,
        )


def _valid_relay_record(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    return (
        record.get("schema_version") == LOG_SCHEMA_VERSION
        and record.get("service") == "rbs"
        and record.get("runtime") == "solver"
        and isinstance(record.get("event"), str)
        and bool(_EVENT_CODE.fullmatch(record["event"]))
        and record["event"].startswith("solver.")
        and str(record.get("level", "")).lower()
        in {"debug", "info", "warning", "error", "critical"}
    )


def _normalizer(runtime: str, component: str, run_id: str):
    def normalize(_logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        from_structlog = bool(event_dict.get("_from_structlog"))
        raw_event = str(event_dict.get("event", ""))
        logger_name = str(event_dict.get("logger", "unknown"))
        event_code = raw_event if _EVENT_CODE.fullmatch(raw_event) else (
            "application.log" if logger_name.startswith("rbs") else "dependency.log"
        )
        level = str(event_dict.get("level", method_name)).lower()
        if level == "warn":
            level = "warning"
        if level not in {"debug", "info", "warning", "error", "critical"}:
            level = "info"

        normalized: dict[str, Any] = {
            "schema_version": LOG_SCHEMA_VERSION,
            "timestamp": _utc_timestamp(),
            "level": level,
            "event": event_code,
            "logger": _bounded(logger_name, 100),
            "service": "rbs",
            "runtime": runtime,
            "component": component,
            "version": __version__,
            "run_id": run_id,
            "process_id": os.getpid(),
            # ProcessorFormatter removes these after this processor. Keeping
            # them here avoids leaking implementation metadata while honoring
            # its structlog/stdlib contract.
            "_record": event_dict.get("_record"),
            "_from_structlog": event_dict.get("_from_structlog", False),
        }
        for key in _CONTEXT_FIELDS:
            value = event_dict.get(key)
            if isinstance(value, str) and _UUID.fullmatch(value):
                normalized[key] = value.lower()
        if from_structlog and event_code != "application.log":
            for key in _APPLICATION_FIELDS:
                value = event_dict.get(key)
                if key in event_dict and _safe_scalar(value):
                    normalized[key] = _safe_value(value)
        elif raw_event and not logger_name.startswith("rbs"):
            normalized["message"] = sanitize_text(raw_event)

        exception = _safe_exception(event_dict.get("exc_info"))
        if exception is not None:
            normalized["exception"] = exception
        return normalized

    return normalize


def sanitize_text(value: object, *, limit: int = 512) -> str:
    """Defense-in-depth redaction for messages owned by dependencies."""
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = _BEARER.sub("Bearer [redacted]", text)
    text = _JWT.sub("[redacted-token]", text)
    text = _CAPABILITY.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    text = _EMAIL.sub("[redacted-email]", text)
    text = _URL.sub("[redacted-url]", text)
    text = _WINDOWS_PATH.sub("[redacted-path]", text)
    text = _POSIX_PATH.sub("[redacted-path]", text)
    text = _DOCUMENT_FILE.sub("[redacted-file]", text)
    return _bounded(text, limit)


def _safe_exception(exc_info: object) -> dict[str, Any] | None:
    if not exc_info:
        return None
    if exc_info is True:
        info = sys.exc_info()
    elif isinstance(exc_info, tuple) and len(exc_info) == 3:
        info = exc_info
    elif isinstance(exc_info, BaseException):
        info = (type(exc_info), exc_info, exc_info.__traceback__)
    else:
        return None
    exc_type, _value, tb = info
    if exc_type is None:
        return None
    frames = []
    if tb is not None:
        for frame in traceback.extract_tb(tb)[-12:]:
            frames.append(
                {
                    "file": Path(frame.filename).name,
                    "function": _bounded(frame.name, 100),
                    "line": frame.lineno,
                }
            )
    return {"type": getattr(exc_type, "__name__", "Exception"), "frames": frames}


def _desktop_handler(
    directory: Path,
    run_id: str,
    component: str,
) -> tuple[logging.Handler, Path]:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    safe_component = _SAFE_COMPONENT.sub("-", component.lower()).strip("-") or "application"
    path = directory / f"rbs-{run_id}-{safe_component}-{os.getpid()}.jsonl"
    handler = _PrivateRotatingFileHandler(
        path,
        maxBytes=LOG_CHUNK_BYTES,
        backupCount=12,
        encoding="utf-8",
    )
    marker = directory / f".active-{run_id}-{os.getpid()}"
    marker.touch(mode=0o600, exist_ok=True)
    os.chmod(marker, 0o600)
    return handler, marker


class _PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        os.chmod(self.baseFilename, stat.S_IRUSR | stat.S_IWUSR)
        return stream

    def doRollover(self) -> None:
        super().doRollover()
        for candidate in Path(self.baseFilename).parent.glob(
            f"{Path(self.baseFilename).name}.*"
        ):
            os.chmod(candidate, stat.S_IRUSR | stat.S_IWUSR)


def _valid_run_id(value: str | None) -> str:
    if value and _UUID.fullmatch(value):
        return value.lower()
    return str(uuid.uuid4())


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_scalar(value: object) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    return value is None or isinstance(value, (bool, int, str))


def _safe_value(value: object) -> object:
    return sanitize_text(value, limit=128) if isinstance(value, str) else value


def _bounded(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[: limit - 1]}…"


_configure_silent_structlog()


__all__ = [
    "configure_logging",
    "current_runtime",
    "get_logger",
    "install_asgi_logging",
    "log_context",
    "relay_solver_stderr",
    "resolve_log_level",
    "sanitize_text",
    "LoggingConfig",
    "LoggingContextMiddleware",
    "LoggingRuntime",
    "LOG_LEVEL_ENV",
    "LOG_SCHEMA_VERSION",
]
