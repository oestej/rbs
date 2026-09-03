"""Pure presentation policy for workspace save-state indicators."""

from __future__ import annotations

from datetime import UTC, datetime

from rbs.models.workspace import DownloadState, Workspace

# Explicit CSS tones keep header-pill contrast independent of framework colors.
PILL_ALERT = "alert"
PILL_WARN = "warn"
PILL_MUTED = "muted"
PILL_OK = "ok"

DOWNLOAD_LABELS = {
    DownloadState.NEVER: ("Never downloaded", PILL_ALERT),
    DownloadState.STALE: ("Changes since download", PILL_WARN),
    DownloadState.CURRENT: ("Downloaded", PILL_OK),
}


def download_summary(workspace: Workspace) -> tuple[str, str]:
    """Describe whether the user's workspace file matches repository state."""
    label, colour = DOWNLOAD_LABELS[workspace.download_state]
    if workspace.download_state is DownloadState.CURRENT and workspace.exported_at:
        label = f"Downloaded {_relative_age(workspace.exported_at)}"
    return label, colour


def _relative_age(timestamp: str) -> str:
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return "recently"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    seconds = (datetime.now(UTC) - moment).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(seconds // 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


def pill_classes(tone: str) -> str:
    return f"rbs-pill rbs-pill--{tone}"
