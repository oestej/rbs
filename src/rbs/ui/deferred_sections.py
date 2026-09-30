"""Keep section editors mounted and prepare unopened sections during browser idle time."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping

from nicegui.element import Element
from nicegui.elements.tabs import Tabs
from nicegui.events import GenericEventArguments, ValueChangeEventArguments

from rbs.logging import get_logger


class DeferredSections(Element, component="static/deferred_sections.js"):
    """One render per section, with at most one background render per idle request.

    The browser owns interaction/visibility checks. Deleting this element with its
    page cancels its browser queue; the server also rejects late or stale requests.
    Builders and save callbacks retain the snapshot that created the page.
    """

    def __init__(
        self,
        panels: Mapping[str, Element],
        render: Callable[[str], None],
        *,
        can_preload: Callable[[], bool] | None = None,
        is_active: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__()
        self._panels = dict(panels)
        self._render = render
        self._can_preload = can_preload or (lambda: True)
        self._is_active = is_active or (lambda: True)
        self._remaining = dict.fromkeys(panels)
        self._stopped = False
        self._props["pending"] = bool(self._remaining)
        self._props["generation"] = 0
        self.on("idle", self._preload_next)

    def load(self, name: str) -> None:
        """Load a requested section immediately, preserving every existing editor."""
        if self.is_deleted or name not in self._remaining:
            return
        panel = self._panels[name]
        if panel.is_deleted:
            self.cancel()
            return
        try:
            with panel:
                self._render(name)
        except Exception:
            # A retry must not append a second editor to a partially built one.
            panel.clear()
            raise
        del self._remaining[name]
        self._props["pending"] = bool(self._remaining) and not self._stopped
        self._props["generation"] += 1

    def cancel(self) -> None:
        """Stop speculative work; explicit section selection remains available."""
        self._stopped = True
        self._props["pending"] = False

    async def _preload_next(self, event: GenericEventArguments) -> None:
        # Give queued navigation/save callbacks a chance to replace this page first.
        await asyncio.sleep(0)
        if self.is_deleted or self._stopped or event.args != self._props["generation"]:
            return
        if not self._can_preload():
            self.cancel()
            return
        if not self._is_active():
            # A page switch can overtake a browser idle request. Acknowledge it
            # without doing work; returning to the page can resume the queue.
            self._props["generation"] += 1
            return
        name = next(iter(self._remaining), None)
        if name is None:
            return
        try:
            self.load(name)
        except Exception as exc:
            # Background work should not interrupt an unrelated editor. Opening
            # the failed section explicitly retries through the usual UI path.
            self.cancel()
            get_logger("ui").error(
                "ui.section_preload_failed",
                error_code=type(exc).__name__,
                exc_info=True,
            )


def defer_sections(
    tabs: Tabs,
    panels: Mapping[str, Element],
    render: Callable[[str], None],
    *,
    active_section: str,
    on_section_change: Callable[[ValueChangeEventArguments], None] | None = None,
    can_preload: Callable[[], bool] | None = None,
    is_active: Callable[[], bool] | None = None,
) -> DeferredSections:
    """Render the selected section now and the others on selection or idle time."""
    loader = DeferredSections(panels, render, can_preload=can_preload, is_active=is_active)
    loader.load(active_section if active_section in panels else next(iter(panels)))

    def select_section(event: ValueChangeEventArguments) -> None:
        name = getattr(event.value, "name", event.value)
        loader.load(name)
        if on_section_change is not None:
            on_section_change(event)

    tabs.on_value_change(select_section)
    return loader
