"""Discovery and placement of native desktop recovery drafts."""

from __future__ import annotations

import os

import pytest

from rbs.desktop import recovery
from rbs.desktop.recovery import (
    allocate_recovery_path,
    default_recovery_directory,
    recoverable_drafts,
)


def test_default_recovery_directory_is_platform_specific(tmp_path) -> None:
    assert default_recovery_directory(platform="darwin", home=tmp_path) == (
        tmp_path / "Library" / "Application Support" / "RBS Desktop" / "Recovery"
    )
    assert default_recovery_directory(
        platform="win32",
        environ={"LOCALAPPDATA": str(tmp_path / "Local")},
        home=tmp_path,
    ) == tmp_path / "Local" / "RBS Desktop" / "Recovery"
    assert default_recovery_directory(
        platform="linux",
        environ={"XDG_STATE_HOME": str(tmp_path / "state")},
        home=tmp_path,
    ) == tmp_path / "state" / "rbs-desktop" / "recovery"


def test_allocate_recovery_path_creates_a_private_directory(tmp_path) -> None:
    root = tmp_path / "private"

    path = allocate_recovery_path(root, pid=42, token="d" * 32)

    assert path == root / f"checkpoint-42-{'d' * 32}.sqlite"
    assert root.is_dir()
    if os.name != "nt":
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


def test_windows_liveness_probe_never_uses_os_kill(monkeypatch) -> None:
    probed: list[int] = []
    monkeypatch.setattr(
        recovery,
        "_is_windows_process_running",
        lambda pid: probed.append(pid) or True,
    )
    monkeypatch.setattr(
        recovery.os,
        "kill",
        lambda *_args: pytest.fail("os.kill must not be called on Windows"),
    )

    assert recovery._is_process_running(123, platform="win32")
    assert probed == [123]


class _FakeKernel32:
    def __init__(self, *, handle: int, exit_code: int = 259) -> None:
        self.handle = handle
        self.exit_code = exit_code
        self.closed: list[int] = []

    def OpenProcess(self, _access, _inherit, _pid):
        return self.handle

    def GetExitCodeProcess(self, _handle, result):
        result._obj.value = self.exit_code
        return True

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [(259, True), (0, False)],
)
def test_windows_liveness_probe_reads_process_exit_code(exit_code, expected) -> None:
    kernel32 = _FakeKernel32(handle=42, exit_code=exit_code)

    assert recovery._is_windows_process_running(123, kernel32=kernel32) is expected
    assert kernel32.closed == [42]


@pytest.mark.parametrize(
    ("error", "expected"),
    [(87, False), (5, True)],
)
def test_windows_liveness_probe_treats_only_missing_pid_as_stale(error, expected) -> None:
    kernel32 = _FakeKernel32(handle=0)

    assert (
        recovery._is_windows_process_running(
            123,
            kernel32=kernel32,
            last_error=lambda: error,
        )
        is expected
    )


@pytest.mark.parametrize("token", ["short", "G" * 32, "x" * 32])
def test_recovery_path_rejects_ambiguous_tokens(tmp_path, token) -> None:
    with pytest.raises(ValueError, match="32 lowercase hexadecimal"):
        allocate_recovery_path(tmp_path, pid=1, token=token)
