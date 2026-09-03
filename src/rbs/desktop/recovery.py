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
from collections.abc import Callable
from pathlib import Path

_CHECKPOINT_NAME = re.compile(
    r"^checkpoint-(?P<pid>[1-9][0-9]*)-[a-f0-9]{32}\.sqlite$"
)
_LEGACY_DRAFT_NAME = re.compile(
    r"^draft-(?P<pid>[1-9][0-9]*)-[a-f0-9]{32}\.rbsc$"
)


def default_recovery_directory(*, home: Path | None = None) -> Path:
    """Return the per-user application-state directory for recoverable drafts."""
    home = Path.home() if home is None else home
    return home / "Library" / "Application Support" / "RBS Desktop" / "Recovery"


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
    os.chmod(root, 0o700)


def _is_process_running(pid: int) -> bool:
    # ``os.kill(pid, 0)`` is the conventional non-destructive probe: it performs
    # every permission check without delivering a signal.
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
