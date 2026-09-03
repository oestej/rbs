"""Preview packaging composition for ``rbs ui --desktop`` and ``--cloud``."""

import asyncio

import pytest

from rbs.catalog import sample_instance
from rbs.product import ProductTarget
from rbs.store import Store
from rbs.ui.host import LocalHost
from rbs.ui.preview import (
    CloudPreviewHost,
    PreviewDocumentIO,
    PreviewUnavailableError,
    build_host,
    mode_from_flags,
)


def _created(before: set[int]) -> list:
    from nicegui import ui

    return [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]


def test_mode_from_flags() -> None:
    assert mode_from_flags() == "local"
    assert mode_from_flags(desktop=True) == "desktop"
    assert mode_from_flags(cloud=True) == "cloud"
    with pytest.raises(ValueError, match="--desktop and --cloud"):
        mode_from_flags(desktop=True, cloud=True)


def test_local_preview_is_the_plain_browser_host(tmp_path) -> None:
    store = Store(tmp_path / "local.sqlite")
    store.init()

    host = build_host(store, "local")

    assert type(host) is LocalHost
    assert host.document_io is None
    assert host.allows_database_restore
    assert host.product.target is ProductTarget.LOCAL


def test_desktop_preview_adds_native_chrome_to_the_local_stack(tmp_path) -> None:
    store = Store(tmp_path / "desktop.sqlite")
    store.init()

    host = build_host(store, "desktop")

    assert isinstance(host, LocalHost)
    assert isinstance(host.document_io, PreviewDocumentIO)
    assert not host.allows_database_restore
    assert host.product.target is ProductTarget.LOCAL


def test_cloud_preview_is_single_user_behind_hosted_chrome(tmp_path) -> None:
    store = Store(tmp_path / "cloud.sqlite")
    store.init()

    host = build_host(store, "cloud")

    assert isinstance(host, CloudPreviewHost)
    assert host.document_io is None
    assert not host.allows_database_restore
    assert host.product.target is ProductTarget.CLOUD
    assert host.session_status(host.principal(None)) is None
    assert host.eviction_notice(host.principal(None)) is None


def test_unknown_preview_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown UI mode"):
        build_host(None, "native")


def test_preview_document_is_clean_on_an_empty_desk(tmp_path) -> None:
    store = Store(tmp_path / "empty.sqlite")
    store.init()
    documents = PreviewDocumentIO(store)

    assert documents.workspace is None
    assert not documents.dirty
    assert documents.path is None
    assert documents.recovery_error is None
    assert documents.settings_error is None


def test_preview_document_tracks_the_desk_without_writing(tmp_path) -> None:
    store = Store(tmp_path / "desk.sqlite")
    store.init()
    workspace = store.create("Preview year", sample_instance())
    documents = PreviewDocumentIO(store)

    assert documents.workspace is not None
    assert documents.workspace.id == workspace.id
    assert documents.dirty
    assert documents.application_name == "RBS Desktop"
    assert documents.supports_application_settings
    assert not documents.sync_application_settings(workspace.instance)
    assert not documents.clear_recovery_checkpoint()


def test_preview_document_actions_fail_loudly(tmp_path) -> None:
    store = Store(tmp_path / "actions.sqlite")
    store.init()
    store.create("Preview year", sample_instance())
    documents = PreviewDocumentIO(store)

    with pytest.raises(PreviewUnavailableError, match="preview mode"):
        documents.new()
    with pytest.raises(PreviewUnavailableError, match="preview mode"):
        documents.close()
    for action in (
        documents.open,
        documents.save,
        documents.save_as,
        documents.save_settings,
        documents.load_settings,
    ):
        with pytest.raises(PreviewUnavailableError, match="preview mode"):
            asyncio.run(action())


def test_desktop_preview_renders_the_document_tab(tmp_path) -> None:
    from nicegui import ui

    from rbs.ui.session import WorkspaceSession
    from rbs.ui.workspaces.io import _workspace_tab

    store = Store(tmp_path / "render.sqlite")
    store.init()
    workspace = store.create("Preview year", sample_instance())
    host = build_host(store, "desktop")
    session = WorkspaceSession(
        store=store,
        workspace_host=host,
        principal=host.principal(None),
        workspace_id=workspace.id,
    )
    before = set(ui.context.client.elements)

    _workspace_tab(store, workspace, session, lambda: None)

    labels = {
        str(getattr(element, "_text", "") or "") for element in _created(before)
    }
    assert "Desktop document" in labels
    assert "Replace database" not in labels


def test_cloud_preview_hides_database_replace_without_native_chrome(
    tmp_path,
) -> None:
    from nicegui import ui

    from rbs.ui.session import WorkspaceSession
    from rbs.ui.workspaces.io import _workspace_tab

    store = Store(tmp_path / "render.sqlite")
    store.init()
    workspace = store.create("Preview year", sample_instance())
    host = build_host(store, "cloud")
    session = WorkspaceSession(
        store=store,
        workspace_host=host,
        principal=host.principal(None),
        workspace_id=workspace.id,
    )
    before = set(ui.context.client.elements)

    _workspace_tab(store, workspace, session, lambda: None)

    labels = {
        str(getattr(element, "_text", "") or "") for element in _created(before)
    }
    assert "Desktop document" not in labels
    assert "All workspaces" in labels
    assert "Replace database" not in labels
