"""The hosted request path end to end: identity, isolation, and the save route."""

from __future__ import annotations

import json

import pytest

from rbs.catalog import sample_instance
from rbs.cloud.config import ALLOWLIST, CloudConfig
from rbs.cloud.control import ControlPlane
from rbs.cloud.host import CloudHost
from rbs.cloud.sessions import SessionRegistry
from rbs.cloud.solve_pool import SolvePool
from rbs.ui.app import export_workspace_response
from rbs.ui.host import Principal
from rbs.ui.workspaces.file_handle import PAYLOAD_HEADER, PAYLOAD_MARKER

ALICE = Principal("subject-alice", "alice@example.org", "cloudflare_access")
BOB = Principal("subject-bob", "bob@example.org", "cloudflare_access")


class _AdapterStub:
    def __init__(self, principal: Principal | None = None) -> None:
        self.principal = principal

    def resolve(self, request):  # noqa: ARG002 - the stub ignores the request
        return self.principal


@pytest.fixture
def cloud(tmp_path):
    config = CloudConfig(
        cf_team_domain="acme.cloudflareaccess.com",
        cf_audience="aud-for-this-app",
        storage_secret="signing-secret",
        authorization_mode=ALLOWLIST,
        bootstrap_subjects=(ALICE.subject, BOB.subject),
        control_db=tmp_path / "control.sqlite",
        data_dir=tmp_path / "data",
    )
    control = ControlPlane(config.control_db, config)
    control.init()
    for subject in config.bootstrap_subjects:
        control.admit(subject)
    adapter = _AdapterStub()
    host = CloudHost(config, control, SessionRegistry(control, config), adapter, SolvePool(config))
    return host, adapter


def test_an_unauthenticated_request_gets_no_workspace(cloud) -> None:
    host, adapter = cloud
    adapter.principal = None

    response = export_workspace_response(host, 1, object())

    assert response.status_code == 403


def test_an_authenticated_request_receives_its_own_workspace(cloud) -> None:
    host, adapter = cloud
    adapter.principal = ALICE
    workspace = host.store_for(ALICE).create("Alice AY", sample_instance())

    response = export_workspace_response(host, workspace.id, object())

    assert response.status_code == 200
    assert response.headers[PAYLOAD_HEADER] == PAYLOAD_MARKER
    assert "Alice-AY" in response.headers["content-disposition"]
    payload = json.loads(response.body)
    assert [item["name"] for item in payload["workspaces"]] == ["Alice AY"]


def test_sample_data_can_only_be_exported_as_a_regular_copy(cloud) -> None:
    host, adapter = cloud
    adapter.principal = ALICE
    store = host.store_for(ALICE)
    workspace = store.create("Sample 2026-2027", sample_instance(), is_sample=True)

    assert export_workspace_response(host, workspace.id, object()).status_code == 409

    response = export_workspace_response(
        host,
        workspace.id,
        object(),
        save_as=True,
    )
    payload = json.loads(response.body)
    assert response.status_code == 200
    assert payload["workspaces"][0]["is_sample"] is False
    assert store.get(workspace.id).is_sample


def test_one_user_cannot_download_anothers_workspace(cloud) -> None:
    """Ids are per-desk, so Bob's id 1 must resolve on Bob's desk, not Alice's."""
    host, adapter = cloud
    adapter.principal = ALICE
    alice_workspace = host.store_for(ALICE).create("Alice AY", sample_instance())

    adapter.principal = BOB
    response = export_workspace_response(host, alice_workspace.id, object())

    assert response.status_code == 404


def test_a_download_is_not_cached_by_anything_in_between(cloud) -> None:
    host, adapter = cloud
    adapter.principal = ALICE
    workspace = host.store_for(ALICE).create("Alice AY", sample_instance())

    response = export_workspace_response(host, workspace.id, object())

    assert response.headers["cache-control"] == "no-store"


def test_a_missing_workspace_is_a_404_not_a_crash(cloud) -> None:
    host, adapter = cloud
    adapter.principal = ALICE
    host.store_for(ALICE)

    assert export_workspace_response(host, 9999, object()).status_code == 404


def test_saving_marks_the_workspace_downloaded(cloud) -> None:
    from rbs.store import DownloadState

    host, adapter = cloud
    adapter.principal = ALICE
    store = host.store_for(ALICE)
    workspace = store.create("Alice AY", sample_instance())

    export_workspace_response(host, workspace.id, object())
    # The route serves the bytes; the UI marks the state once the browser
    # confirms it wrote them, which is what mark_exported represents.
    store.mark_exported(
        workspace.id,
        expected_workspace_revision=workspace.workspace_revision,
    )

    assert store.get(workspace.id).download_state is DownloadState.CURRENT


def test_a_mutation_advances_the_retention_clock(cloud) -> None:
    host, adapter = cloud
    adapter.principal = ALICE
    store = host.store_for(ALICE)
    store.create("Alice AY", sample_instance())
    before = host.session_status(ALICE)

    host.touch(ALICE)

    after = host.session_status(ALICE)
    assert after.last_mutation_at >= before.last_mutation_at
    assert after.workspace_count == 1


def test_cloud_upload_limit_is_exposed_to_the_shared_ui(cloud) -> None:
    from types import SimpleNamespace

    from rbs.ui.workspaces.io import _upload_limit, _within_limit

    host, _adapter = cloud
    state = SimpleNamespace(workspace_host=host)

    assert _upload_limit(state) == host._config.upload_max_bytes  # noqa: SLF001
    assert _within_limit("1234", max_bytes=4) == "1234"
    with pytest.raises(ValueError, match="larger"):
        _within_limit("12345", max_bytes=4)


def test_the_solve_ceiling_is_not_a_user_preference(cloud) -> None:
    from rbs.cloud.solve_pool import clamp_solver_settings

    host, _adapter = cloud
    config = host._config  # noqa: SLF001 - asserting the deployment's own limit
    instance = sample_instance()
    greedy = instance.model_copy(
        update={
            "solver": instance.solver.model_copy(
                update={"time_limit_seconds": 9999.0, "num_workers": 256}
            )
        }
    )

    bounded = clamp_solver_settings(greedy, config)

    assert bounded.solver.time_limit_seconds == config.solve_ceiling_seconds
    assert bounded.solver.num_workers == config.solve_workers


def test_settings_inside_the_limits_are_left_alone(cloud) -> None:
    from rbs.cloud.solve_pool import clamp_solver_settings

    host, _adapter = cloud
    instance = sample_instance()
    modest = instance.model_copy(
        update={
            "solver": instance.solver.model_copy(
                update={"time_limit_seconds": 30.0, "num_workers": 2}
            )
        }
    )

    assert clamp_solver_settings(modest, host._config) is modest  # noqa: SLF001
