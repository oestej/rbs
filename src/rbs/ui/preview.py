"""Preview packagings for ``rbs ui``: render desktop/cloud chrome locally.

Both previews keep the local single-user database and solver; only the host
chrome changes. ``--desktop`` shows the native-document UI with inert file
actions, and ``--cloud`` shows the hosted product chrome without identity,
retention, or solve-pool infrastructure — that stack statically cannot be
imported here (see ``tests/test_packaging.py``), and cloud auth needs a real
proxy JWT, so a pretend mode is the only honest local option.
"""

from __future__ import annotations

from rbs.models.workspace import Workspace
from rbs.product import CLOUD_PRODUCT
from rbs.repository import WorkspaceRepository
from rbs.ui.host import DocumentIO, LocalHost, WorkspaceHost

UI_MODES = ("local", "desktop", "cloud")


class PreviewUnavailableError(ValueError):
    """A native file action attempted in UI preview mode."""


class PreviewDocumentIO(DocumentIO):
    """Render-only stand-in for the desktop build's native document ownership.

    Presence alone is what switches the shared UI into desktop chrome (the
    "RBS Desktop" title, the document tab, the settings save/load row, no
    database restore). There are no native dialogs behind it, so every file
    action fails loudly instead of pretending to work.
    """

    def __init__(self, store: WorkspaceRepository) -> None:
        self._store = store

    @property
    def application_name(self) -> str:
        return "RBS Desktop"

    @property
    def generation(self) -> int:
        return 0

    @property
    def path(self):  # noqa: ANN204 - matches the protocol's optional path
        return None

    @property
    def recovery_error(self) -> str | None:
        return None

    @property
    def settings_error(self) -> str | None:
        return None

    @property
    def recovered_from(self):  # noqa: ANN204 - matches the protocol's optional path
        return None

    @property
    def workspace(self) -> Workspace | None:
        """The desk's first workspace, or nothing when the desk is empty."""
        try:
            workspaces = self._store.list()
        except Exception:  # noqa: BLE001 - preview chrome must not break
            return None
        return workspaces[0] if workspaces else None

    @property
    def dirty(self) -> bool:
        """Always unsaved: the preview never writes a file to match against."""
        return self.workspace is not None

    @property
    def supports_application_settings(self) -> bool:
        return True

    def _unavailable(self) -> PreviewUnavailableError:
        return PreviewUnavailableError(
            "Document files are not available in UI preview mode"
        )

    async def open(self):
        raise self._unavailable()

    async def save(self):
        raise self._unavailable()

    async def save_as(self):
        raise self._unavailable()

    async def save_settings(self):
        raise self._unavailable()

    async def load_settings(self):
        raise self._unavailable()

    def new(self, *, sample: bool = False):  # noqa: ARG002 - signature parity
        raise self._unavailable()

    def close(self) -> None:
        raise self._unavailable()

    def clear_recovery_checkpoint(self) -> bool:
        return False

    def sync_application_settings(self, instance) -> bool:  # noqa: ANN001, ARG002
        return False


class CloudPreviewHost(LocalHost):
    """Single-user local stack behind hosted product chrome.

    Same database, caller, and solver as the browser default; only the
    packaging answers change to match ``CloudHost``: the cloud product, no
    whole-database replace, and no native document ownership. Retention and
    eviction stay absent rather than invented.
    """

    product = CLOUD_PRODUCT
    allows_database_restore = False


def mode_from_flags(*, desktop: bool = False, cloud: bool = False) -> str:
    """Map the ``rbs ui`` packaging flags onto one UI mode."""
    if desktop and cloud:
        raise ValueError("choose at most one of --desktop and --cloud")
    if desktop:
        return "desktop"
    if cloud:
        return "cloud"
    return "local"


def build_host(store: WorkspaceRepository, mode: str = "local") -> WorkspaceHost:
    """Compose the host for one ``rbs ui`` launch mode."""
    if mode == "local":
        return LocalHost(store)
    if mode == "desktop":
        return LocalHost(store, document_io=PreviewDocumentIO(store))
    if mode == "cloud":
        return CloudPreviewHost(store)
    raise ValueError(f"unknown UI mode {mode!r}; expected one of {', '.join(UI_MODES)}")
