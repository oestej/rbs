"""Per-user datasets: isolation, eviction, and the bug this whole phase fixes."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from rbs.catalog import sample_instance
from rbs.cloud.config import ALLOWLIST, CloudConfig
from rbs.cloud.control import ControlPlane
from rbs.cloud.host import CloudHost
from rbs.cloud.sessions import SessionRegistry
from rbs.cloud.solve_pool import SolvePool
from rbs.store import StoreInvalidatedError
from rbs.ui.host import Principal, WorkspaceHost

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
ALICE = Principal("subject-alice", "alice@example.org", "cloudflare_access")
BOB = Principal("subject-bob", "bob@example.org", "cloudflare_access")


def _config(tmp_path, **overrides) -> CloudConfig:
    settings = {
        "cf_team_domain": "acme.cloudflareaccess.com",
        "cf_audience": "aud-for-this-app",
        "storage_secret": "signing-secret",
        "authorization_mode": ALLOWLIST,
        "bootstrap_subjects": (ALICE.subject, BOB.subject),
        "control_db": tmp_path / "control.sqlite",
        "data_dir": tmp_path / "data",
    }
    settings.update(overrides)
    return CloudConfig(**settings)


class _AdapterStub:
    """Stands in for the proxy: whatever principal the test says is calling."""

    def __init__(self, principal: Principal | None = None) -> None:
        self.principal = principal

    def resolve(self, request):  # noqa: ARG002 - the stub ignores the request
        return self.principal


@pytest.fixture
def cloud(tmp_path):
    config = _config(tmp_path)
    control = ControlPlane(config.control_db, config)
    control.init()
    for subject in config.bootstrap_subjects:
        control.admit(subject)
    registry = SessionRegistry(control, config)
    adapter = _AdapterStub()
    host = CloudHost(config, control, registry, adapter, SolvePool(config))
    return host, control, registry, adapter, config


def test_cloud_host_satisfies_the_shared_seam(cloud) -> None:
    host, *_ = cloud
    assert isinstance(host, WorkspaceHost)


def test_two_subjects_get_isolated_desks(cloud) -> None:
    host, _control, registry, _adapter, _config = cloud

    alice = registry.store_for(ALICE.subject)
    bob = registry.store_for(BOB.subject)

    assert alice.path != bob.path
    alice.create("Alice AY", sample_instance())

    assert [w.name for w in alice.list()] == ["Alice AY"]
    assert bob.list() == []


def test_the_same_subject_reuses_one_desk(cloud) -> None:
    _host, _control, registry, _adapter, _config = cloud

    assert registry.store_for(ALICE.subject).path == registry.store_for(ALICE.subject).path


def test_selecting_a_workspace_does_not_move_anyone_elses(cloud) -> None:
    """The singleton current_workspace_id was shared by every browser before this."""
    _host, _control, registry, _adapter, _config = cloud
    alice = registry.store_for(ALICE.subject)
    bob = registry.store_for(BOB.subject)
    alice_workspace = alice.create("Alice AY", sample_instance())
    bob_workspace = bob.create("Bob AY", sample_instance())

    alice.set_current(alice_workspace.id)
    bob.set_current(bob_workspace.id)

    assert alice.current_id() == alice_workspace.id
    assert bob.current_id() == bob_workspace.id
    assert alice.current().name == "Alice AY"
    assert bob.current().name == "Bob AY"


def test_a_new_desk_is_empty_rather_than_seeded(cloud) -> None:
    """The sample workspace belongs to the desktop build, not to a hosted user."""
    _host, _control, registry, _adapter, _config = cloud

    assert registry.store_for(ALICE.subject).list() == []


def test_data_files_are_not_named_after_anyone(cloud) -> None:
    _host, _control, registry, _adapter, config = cloud
    registry.store_for(ALICE.subject)

    names = [path.name for path in config.data_dir.glob("*.sqlite")]

    assert names and all(ALICE.subject not in name for name in names)


def test_eviction_removes_the_file_and_leaves_a_tombstone(cloud) -> None:
    host, control, registry, _adapter, _config = cloud
    store = registry.store_for(ALICE.subject)
    store.create("Alice AY", sample_instance())
    record = control.find_dataset(ALICE.subject)
    host.touch(ALICE)
    path = registry.path_for(record)
    assert path.exists()

    registry.evict(record, now=NOW)

    assert not path.exists()
    assert control.find_dataset(ALICE.subject) is None
    notice = control.eviction_notice(ALICE.subject, now=NOW)
    assert notice is not None and notice.workspace_count == 1


def test_evicted_store_handles_cannot_recreate_orphan_files(cloud) -> None:
    _host, control, registry, _adapter, _config = cloud
    store = registry.store_for(ALICE.subject)
    store.create("Alice AY", sample_instance())
    record = control.find_dataset(ALICE.subject)
    path = registry.path_for(record)

    registry.evict(record, now=NOW)

    with pytest.raises(StoreInvalidatedError, match="expired"):
        store.list()
    assert not path.exists()
    assert path not in registry.orphaned_files()


def test_dataset_resolution_is_linearized_with_eviction(cloud, monkeypatch) -> None:
    """A resolver holding the old record cannot reopen it after unlink."""
    _host, control, registry, _adapter, _config = cloud
    registry.store_for(ALICE.subject).create("Alice AY", sample_instance())
    record = control.find_dataset(ALICE.subject)
    retired_path = registry.path_for(record)
    original_dataset_for = control.dataset_for
    record_resolved = threading.Event()
    release_resolution = threading.Event()
    eviction_done = threading.Event()
    resolved: dict[str, object] = {}

    def paused_dataset_for(subject, *, now=None):
        current = original_dataset_for(subject, now=now)
        record_resolved.set()
        assert release_resolution.wait(timeout=2)
        return current

    monkeypatch.setattr(control, "dataset_for", paused_dataset_for)

    resolver = threading.Thread(
        target=lambda: resolved.setdefault("store", registry.store_for(ALICE.subject))
    )
    resolver.start()
    assert record_resolved.wait(timeout=2)

    def evict() -> None:
        registry.evict(record, now=NOW)
        eviction_done.set()

    eviction = threading.Thread(target=evict)
    eviction.start()
    assert not eviction_done.wait(timeout=0.1)
    release_resolution.set()
    resolver.join(timeout=2)
    eviction.join(timeout=2)

    stale = resolved["store"]
    with pytest.raises(StoreInvalidatedError):
        stale.list()
    assert not retired_path.exists()


def test_long_lived_subject_repository_moves_to_new_dataset_after_eviction(cloud) -> None:
    host, control, registry, _adapter, _config = cloud
    repository = host.store_for(ALICE)
    repository.create("Alice AY", sample_instance())
    record = control.find_dataset(ALICE.subject)
    retired_path = registry.path_for(record)

    registry.evict(record, now=NOW)

    assert repository.list() == []
    assert repository.path != retired_path
    assert not retired_path.exists()


def test_database_commits_update_retention_bookkeeping_automatically(cloud) -> None:
    host, control, _registry, _adapter, _config = cloud
    repository = host.store_for(ALICE)

    repository.create("Alice AY", sample_instance())

    record = control.find_dataset(ALICE.subject)
    assert record.workspace_count == 1
    assert record.size_bytes > 0


def test_the_sweep_only_reaps_desks_past_the_window(tmp_path) -> None:
    config = _config(tmp_path, retention=timedelta(days=30))
    control = ControlPlane(config.control_db, config)
    control.init()
    for subject in config.bootstrap_subjects:
        control.admit(subject)
    registry = SessionRegistry(control, config)
    registry.store_for(ALICE.subject, now=NOW)
    registry.store_for(BOB.subject, now=NOW)
    control.touch(control.find_dataset(BOB.subject).id, now=NOW + timedelta(days=25))

    evicted = registry.sweep(now=NOW + timedelta(days=31))

    assert evicted == 1
    assert control.find_dataset(ALICE.subject) is None
    assert control.find_dataset(BOB.subject) is not None


def test_a_returning_user_after_eviction_gets_a_clean_desk(cloud) -> None:
    host, control, registry, _adapter, _config = cloud
    registry.store_for(ALICE.subject).create("Alice AY", sample_instance())
    # Evicted just now, so the tombstone is still inside its own lifetime when
    # the host reads it back without an injected clock.
    registry.evict(control.find_dataset(ALICE.subject))

    assert host.eviction_notice(ALICE) is not None
    assert registry.store_for(ALICE.subject).list() == []


def test_orphaned_data_files_are_found_and_removable(cloud) -> None:
    _host, control, registry, _adapter, config = cloud
    registry.store_for(ALICE.subject)
    stray = config.data_dir / "deadbeef.sqlite"
    stray.write_bytes(b"")

    assert [path.name for path in registry.orphaned_files()] == ["deadbeef.sqlite"]
    assert registry.remove_orphans() == 1
    assert not stray.exists()
    assert control.find_dataset(ALICE.subject) is not None


def test_an_unauthenticated_request_has_no_principal(cloud) -> None:
    host, _control, _registry, adapter, _config = cloud
    adapter.principal = None

    assert host.principal(object()) is None


def test_a_subject_off_the_allowlist_is_refused(tmp_path) -> None:
    config = _config(tmp_path, bootstrap_subjects=(ALICE.subject,))
    control = ControlPlane(config.control_db, config)
    control.init()
    control.admit(ALICE.subject)
    registry = SessionRegistry(control, config)
    adapter = _AdapterStub(BOB)
    host = CloudHost(config, control, registry, adapter, SolvePool(config))

    assert host.principal(object()) is None
    assert not list(config.data_dir.glob("*.sqlite"))


def test_session_status_counts_down_from_the_last_mutation(cloud) -> None:
    host, control, registry, _adapter, config = cloud
    registry.store_for(ALICE.subject, now=NOW)
    control.touch(control.find_dataset(ALICE.subject).id, now=NOW)

    status = host.session_status(ALICE)

    assert status is not None
    assert status.expires_at == NOW + config.retention
    assert status.remaining(NOW + timedelta(days=1)) == config.retention - timedelta(days=1)
    assert status.remaining(NOW + timedelta(days=999)) == timedelta(0)
    assert status.is_expired(NOW + timedelta(days=999))


def test_a_hosted_desk_is_capped(tmp_path) -> None:
    """The cap is what stops a desk quietly becoming an archive."""
    from rbs.store import DeskFullError

    config = _config(tmp_path, desk_cap=2)
    control = ControlPlane(config.control_db, config)
    control.init()
    control.admit(ALICE.subject)
    registry = SessionRegistry(control, config)
    store = registry.store_for(ALICE.subject)
    store.create("One", sample_instance())
    store.create("Two", sample_instance())

    with pytest.raises(DeskFullError):
        store.create("Three", sample_instance())
