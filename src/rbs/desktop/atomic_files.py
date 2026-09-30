"""Atomic text replacement; callers retain directory and permission policy."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(
    destination: Path,
    payload: str,
    *,
    mode: int | None = None,
    sync_directory: bool = False,
) -> None:
    """Replace a file in an existing directory, cleaning up unsuccessful writes.

    With no explicit mode, keep mkstemp's private permissions. Callers that
    preserve an existing file's mode must resolve and pass it themselves.
    """
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
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, destination)
        if sync_directory:
            fsync_directory(destination.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def fsync_directory(directory: Path) -> None:
    """Best-effort directory sync on platforms/filesystems that support it."""
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # Replacement is still atomic when directory syncing is unsupported.
        pass
