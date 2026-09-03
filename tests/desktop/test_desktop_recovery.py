"""Discovery and placement of native desktop recovery drafts."""

from __future__ import annotations

import errno
import os

import pytest

from rbs.desktop import recovery
from rbs.desktop.recovery import (
    allocate_recovery_path,
    default_recovery_directory,
    recoverable_drafts,
)


def test_default_recovery_directory_uses_the_application_support_tree(tmp_path) -> None:
    assert default_recovery_directory(home=tmp_path) == (
        tmp_path / "Library" / "Application Support" / "RBS Desktop" / "Recovery"
    )


def test_allocate_recovery_path_creates_a_private_directory(tmp_path) -> None:
    root = tmp_path / "private"

    path = allocate_recovery_path(root, pid=42, token="d" * 32)

    assert path == root / f"checkpoint-42-{'d' * 32}.sqlite"
    assert root.is_dir()
    assert root.stat().st_mode & 0o777 == 0o700


def test_recovery_discovery_skips_live_processes_and_orders_stale_drafts(tmp_path) -> None:
    live = allocate_recovery_path(tmp_path, pid=111, token="a" * 32)
    older = allocate_recovery_path(tmp_path, pid=222, token="b" * 32)
    newer = allocate_recovery_path(tmp_path, pid=333, token="c" * 32)
    for path in (live, older, newer):
        path.write_text("{}", encoding="utf-8")
    os.utime(older, ns=(1, 1))
    os.utime(newer, ns=(2, 2))

    found = recoverable_drafts(
        tmp_path,
        is_process_running=lambda pid: pid == 111,
    )

    assert found == (newer, older)


def test_recovery_discovery_includes_legacy_rbsc_drafts(tmp_path) -> None:
    legacy = tmp_path / f"draft-222-{'d' * 32}.rbsc"
    legacy.write_text("{}", encoding="utf-8")

    assert recoverable_drafts(
        tmp_path,
        is_process_running=lambda _pid: False,
    ) == (legacy,)


def test_liveness_probe_sees_the_running_test_process() -> None:
    assert recovery._is_process_running(os.getpid())


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ProcessLookupError(), False),
        (PermissionError(), True),
        (OSError(errno.ESRCH, "no such process"), False),
        (OSError(errno.EPERM, "operation not permitted"), True),
    ],
)
def test_liveness_probe_reads_liveness_out_of_the_probe_error(
    monkeypatch, error, expected
) -> None:
    def refuse(*_args):
        raise error

    monkeypatch.setattr(recovery.os, "kill", refuse)

    assert recovery._is_process_running(123) is expected


@pytest.mark.parametrize("token", ["short", "G" * 32, "x" * 32])
def test_recovery_path_rejects_ambiguous_tokens(tmp_path, token) -> None:
    with pytest.raises(ValueError, match="32 lowercase hexadecimal"):
        allocate_recovery_path(tmp_path, pid=1, token=token)
