"""Private crash/quit checkpoints for the native desktop packaging.

The native webview cannot synchronously ask the Python document controller to
save while its close button is being handled. RBS therefore keeps an atomic
SQLite checkpoint for each *dirty* desktop process. A later process may recover
checkpoints whose owner PID is no longer running; live windows are never treated
as recovery sources. Legacy ``.rbsc`` drafts remain discoverable for upgrades.
"""

from __future__ import annotations

import errno
import os
import re
import secrets
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

_CHECKPOINT_NAME = re.compile(
    r"^checkpoint-(?P<pid>[1-9][0-9]*)-[a-f0-9]{32}\.sqlite$"
)
_LEGACY_DRAFT_NAME = re.compile(
    r"^draft-(?P<pid>[1-9][0-9]*)-[a-f0-9]{32}\.rbsc$"
)


def default_recovery_directory(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return a per-user application-state directory for recoverable drafts."""
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home

    if platform == "darwin":
        return home / "Library" / "Application Support" / "RBS Desktop" / "Recovery"
    if platform == "win32":
        local_app_data = environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else home / "AppData" / "Local"
        return base / "RBS Desktop" / "Recovery"
    state_home = environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else home / ".local" / "state"
    return base / "rbs-desktop" / "recovery"


def allocate_recovery_path(
    root: Path,
    *,
    pid: int | None = None,
    token: str | None = None,
) -> Path:
    """Allocate a unique checkpoint path without creating it yet."""
    _prepare_private_directory(root)
    owner = os.getpid() if pid is None else pid
    if owner < 1:
        raise ValueError("recovery owner PID must be positive")
    identifier = secrets.token_hex(16) if token is None else token
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise ValueError("recovery token must be 32 lowercase hexadecimal characters")
    return root / f"checkpoint-{owner}-{identifier}.sqlite"


def recoverable_drafts(
    root: Path,
    *,
    is_process_running: Callable[[int], bool] | None = None,
) -> tuple[Path, ...]:
    """Return newest-first checkpoints whose desktop process has exited."""
    if not root.is_dir():
        return ()
    running = _is_process_running if is_process_running is None else is_process_running
    candidates: list[tuple[int, Path]] = []
    paths = (*root.glob("checkpoint-*.sqlite"), *root.glob("draft-*.rbsc"))
    for path in paths:
        match = _CHECKPOINT_NAME.fullmatch(path.name) or _LEGACY_DRAFT_NAME.fullmatch(
            path.name
        )
        if match is None or running(int(match.group("pid"))):
            continue
        try:
            modified = path.stat().st_mtime_ns
        except FileNotFoundError:
            continue
        candidates.append((modified, path))
    candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    return tuple(path for _modified, path in candidates)


def _prepare_private_directory(root: Path) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(root, 0o700)


def _is_process_running(pid: int, *, platform: str | None = None) -> bool:
    # ``os.kill(pid, 0)`` is the conventional non-destructive POSIX probe, but
    # Windows implements every signal other than CTRL_C/CTRL_BREAK with
    # TerminateProcess -- including signal 0. Never use it on Windows.
    platform = sys.platform if platform is None else platform
    if platform == "win32":
        return _is_windows_process_running(pid)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return True
    return True


def _is_windows_process_running(
    pid: int,
    *,
    kernel32: Any | None = None,
    last_error: Callable[[], int] | None = None,
) -> bool:
    """Probe a Windows PID without sending a signal or changing its state."""
    import ctypes
    from ctypes import wintypes

    if kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        last_error = ctypes.get_last_error
    elif last_error is None:
        def no_last_error() -> int:
            return 0

        last_error = no_last_error

    process_query_limited_information = 0x1000
    error_invalid_parameter = 87
    still_active = 259
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # ERROR_INVALID_PARAMETER is how OpenProcess reports a PID that no
        # longer exists. Access-denied and unknown failures are conservatively
        # treated as live so another window's draft is never stolen.
        return last_error() != error_invalid_parameter

    exit_code = wintypes.DWORD()
    try:
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)
