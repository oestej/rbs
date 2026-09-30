"""Shared replacement mechanics and the distinct desktop caller policies."""

import os
import stat

import pytest

from rbs.desktop import atomic_files, diagnostics, documents, settings

WRITERS = [documents._atomic_write_text, settings._atomic_write_text, diagnostics._atomic_write]


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_failed_write_preserves_original_and_removes_temporary(
    tmp_path, monkeypatch, writer, failure,
):
    destination = tmp_path / "record.json"
    destination.write_text("original", encoding="utf-8")

    def fail(*_args):
        raise OSError("injected failure")

    monkeypatch.setattr(os, failure, fail)
    with pytest.raises(OSError, match="injected failure"):
        writer(destination, "replacement")

    assert destination.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("writer", WRITERS)
def test_new_files_are_private_and_utf8(tmp_path, writer):
    destination = tmp_path / "record.json"
    writer(destination, "résidents\n")
    assert destination.read_bytes() == "résidents\n".encode()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


@pytest.mark.parametrize("writer", WRITERS)
def test_existing_permissions_follow_caller_policy(tmp_path, writer):
    parent = tmp_path / "existing"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    destination = parent / "record.json"
    destination.write_text("original")
    destination.chmod(0o640)

    writer(destination, "replacement")

    private = writer is diagnostics._atomic_write
    assert stat.S_IMODE(parent.stat().st_mode) == (0o700 if private else 0o755)
    assert stat.S_IMODE(destination.stat().st_mode) == (0o600 if private else 0o640)


@pytest.mark.parametrize("writer", WRITERS)
def test_missing_parent_follows_caller_policy(tmp_path, writer):
    destination = tmp_path / "missing" / "record.json"
    if writer is documents._atomic_write_text:
        with pytest.raises(FileNotFoundError, match="destination folder does not exist"):
            writer(destination, "content")
        assert not destination.parent.exists()
    else:
        writer(destination, "content")
        assert destination.read_text() == "content"
        assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("writer", WRITERS)
def test_only_diagnostics_requests_directory_sync(tmp_path, monkeypatch, writer):
    synced = []
    monkeypatch.setattr(atomic_files, "fsync_directory", synced.append)
    writer(tmp_path / "record.json", "content")
    assert synced == ([tmp_path] if writer is diagnostics._atomic_write else [])


def test_directory_sync_is_best_effort(tmp_path, monkeypatch):
    def fail(*_args):
        raise OSError("directory sync unsupported")

    monkeypatch.setattr(os, "open", fail)
    atomic_files.fsync_directory(tmp_path)
