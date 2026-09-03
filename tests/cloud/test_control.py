"""The control plane: authorization, dataset mapping, and the retention clock."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from rbs.cloud.config import ALLOWLIST, TRUST_PROXY, CloudConfig
from rbs.cloud.control import ControlPlane, UnknownSubjectError
from rbs.ui.host import Principal

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
BOOTSTRAP = Principal("bootstrap-subject", "admin@example.org", "cloudflare_access")
STRANGER = Principal("stranger-subject", "someone@example.org", "cloudflare_access")


def _config(mode: str = ALLOWLIST, **overrides) -> CloudConfig:
    settings = {
        "cf_team_domain": "acme.cloudflareaccess.com",
        "cf_audience": "aud-for-this-app",
        "storage_secret": "signing-secret",
        "authorization_mode": mode,
        "bootstrap_subjects": ("bootstrap-subject",) if mode == ALLOWLIST else (),
    }
    settings.update(overrides)
    return CloudConfig(**settings)


def _plane(tmp_path, config: CloudConfig | None = None) -> ControlPlane:
    plane = ControlPlane(tmp_path / "control.sqlite", config or _config())
    plane.init()
    return plane


def _admitted(tmp_path, config: CloudConfig | None = None) -> ControlPlane:
    """A plane with the bootstrap subject already through the door."""
    plane = _plane(tmp_path, config)
    plane.authorize(BOOTSTRAP, now=NOW)
    return plane


def test_allowlist_admits_bootstrap_subjects_and_refuses_everyone_else(tmp_path) -> None:
    plane = _plane(tmp_path)

    assert plane.authorize(BOOTSTRAP, now=NOW) is True
    assert plane.authorize(STRANGER, now=NOW) is False


def test_refusing_a_stranger_creates_no_record_of_them(tmp_path) -> None:
    plane = _plane(tmp_path)

    plane.authorize(STRANGER, now=NOW)

    with plane.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0


def test_trust_proxy_admits_anyone_the_proxy_vouched_for(tmp_path) -> None:
    plane = _plane(tmp_path, _config(TRUST_PROXY))

    assert plane.authorize(STRANGER, now=NOW) is True
    assert plane.find_dataset(STRANGER.subject) is None  # not created until asked for


def test_an_admitted_subject_can_be_revoked(tmp_path) -> None:
    plane = _plane(tmp_path, _config(TRUST_PROXY))
    plane.authorize(STRANGER, now=NOW)

    plane.revoke(STRANGER.subject)

    # trust_proxy keeps admitting; the allowlist is what a revoke bites on.
    strict = ControlPlane(plane.path, _config(ALLOWLIST))
    assert strict.authorize(STRANGER, now=NOW) is False


def test_configuration_outranks_a_revoked_bootstrap_subject(tmp_path) -> None:
    """Otherwise revoking the last administrator would be unrecoverable."""
    plane = _plane(tmp_path)
    plane.authorize(BOOTSTRAP, now=NOW)

    plane.revoke(BOOTSTRAP.subject)

    assert plane.authorize(BOOTSTRAP, now=NOW) is True


def test_admit_then_authorize_lets_a_stranger_in(tmp_path) -> None:
    plane = _plane(tmp_path)

    plane.admit(STRANGER.subject)

    assert plane.authorize(STRANGER, now=NOW) is True


def test_dataset_is_created_once_and_reused(tmp_path) -> None:
    plane = _admitted(tmp_path)

    first = plane.dataset_for(BOOTSTRAP.subject, now=NOW)
    second = plane.dataset_for(BOOTSTRAP.subject, now=NOW + timedelta(days=1))

    assert first == second
    assert plane.find_dataset(BOOTSTRAP.subject) == first


def test_dataset_filenames_are_opaque_and_not_derived_from_the_subject(tmp_path) -> None:
    """The data directory must not be attributable to people without this database."""
    plane = _admitted(tmp_path)

    record = plane.dataset_for(BOOTSTRAP.subject, now=NOW)

    assert re.fullmatch(r"[0-9a-f]{32}\.sqlite", record.filename)
    assert BOOTSTRAP.subject not in record.filename
    assert BOOTSTRAP.subject not in record.id


def test_touch_advances_the_retention_clock(tmp_path) -> None:
    plane = _admitted(tmp_path)
    record = plane.dataset_for(BOOTSTRAP.subject, now=NOW)

    plane.touch(record.id, now=NOW + timedelta(days=3), workspace_count=4)

    refreshed = plane.find_dataset(BOOTSTRAP.subject)
    assert refreshed.last_mutation_at == NOW + timedelta(days=3)
    assert refreshed.workspace_count == 4


def test_evictable_respects_the_configured_window(tmp_path) -> None:
    plane = _admitted(tmp_path, _config(retention=timedelta(days=30)))
    record = plane.dataset_for(BOOTSTRAP.subject, now=NOW)

    assert plane.evictable(now=NOW + timedelta(days=29)) == []
    assert [item.id for item in plane.evictable(now=NOW + timedelta(days=31))] == [record.id]


def test_a_touch_inside_the_window_defers_eviction(tmp_path) -> None:
    plane = _admitted(tmp_path, _config(retention=timedelta(days=30)))
    record = plane.dataset_for(BOOTSTRAP.subject, now=NOW)

    plane.touch(record.id, now=NOW + timedelta(days=29))

    assert plane.evictable(now=NOW + timedelta(days=31)) == []
    assert plane.evictable(now=NOW + timedelta(days=60)) != []


def test_forget_drops_the_dataset_and_leaves_a_tombstone(tmp_path) -> None:
    plane = _admitted(tmp_path)
    record = plane.dataset_for(BOOTSTRAP.subject, now=NOW)
    plane.touch(record.id, now=NOW, workspace_count=3)

    forgotten = plane.forget(record.id, now=NOW + timedelta(days=91))

    assert forgotten.id == record.id
    assert plane.find_dataset(BOOTSTRAP.subject) is None
    notice = plane.eviction_notice(BOOTSTRAP.subject, now=NOW + timedelta(days=92))
    assert notice is not None
    assert notice.workspace_count == 3
    assert notice.evicted_at == NOW + timedelta(days=91)


def test_a_returning_user_gets_a_fresh_dataset_after_eviction(tmp_path) -> None:
    plane = _admitted(tmp_path)
    original = plane.dataset_for(BOOTSTRAP.subject, now=NOW)
    plane.forget(original.id, now=NOW + timedelta(days=91))

    replacement = plane.dataset_for(BOOTSTRAP.subject, now=NOW + timedelta(days=92))

    assert replacement.id != original.id
    assert replacement.filename != original.filename


def test_tombstones_expire_and_can_be_dismissed(tmp_path) -> None:
    plane = _admitted(tmp_path, _config(retention=timedelta(days=30)))
    record = plane.dataset_for(BOOTSTRAP.subject, now=NOW)
    plane.forget(record.id, now=NOW)

    assert plane.eviction_notice(BOOTSTRAP.subject, now=NOW + timedelta(days=29)) is not None
    assert plane.eviction_notice(BOOTSTRAP.subject, now=NOW + timedelta(days=31)) is None
    assert plane.purge_expired_tombstones(now=NOW + timedelta(days=31)) == 1


def test_showing_a_notice_can_clear_it(tmp_path) -> None:
    plane = _admitted(tmp_path)
    record = plane.dataset_for(BOOTSTRAP.subject, now=NOW)
    plane.forget(record.id, now=NOW)

    plane.clear_eviction_notices(BOOTSTRAP.subject)

    assert plane.eviction_notice(BOOTSTRAP.subject, now=NOW) is None


def test_the_control_plane_holds_no_schedule_content(tmp_path) -> None:
    """The claim the whole design rests on, asserted against the schema."""
    plane = _admitted(tmp_path)
    plane.dataset_for(BOOTSTRAP.subject, now=NOW)

    with plane.connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        columns = {
            column["name"]
            for table in tables
            for column in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }

    assert tables <= {"users", "datasets", "tombstones", "sqlite_sequence"}
    forbidden = {"instance_json", "schedule_json", "catalog_json", "case", "residents"}
    assert not (columns & forbidden)


@pytest.mark.parametrize("mode", [ALLOWLIST, TRUST_PROXY])
def test_authorize_records_when_a_known_caller_was_last_seen(tmp_path, mode) -> None:
    plane = _plane(tmp_path, _config(mode))
    principal = BOOTSTRAP if mode == ALLOWLIST else STRANGER
    plane.authorize(principal, now=NOW)

    plane.authorize(principal, now=NOW + timedelta(days=5))

    with plane.connect() as conn:
        row = conn.execute(
            "SELECT last_seen_at FROM users WHERE subject = ?", (principal.subject,)
        ).fetchone()
    assert row["last_seen_at"].startswith("2026-03-06")


def test_a_dataset_cannot_exist_without_an_admitted_user(tmp_path) -> None:
    plane = _plane(tmp_path)

    with pytest.raises(UnknownSubjectError):
        plane.dataset_for(STRANGER.subject, now=NOW)
