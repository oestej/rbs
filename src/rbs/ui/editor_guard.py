"""Shared protection for editors that hold local changes until Save."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


def confirm_close_editor(
    *,
    subject: str,
    save_label: str,
    save_icon: str,
    on_discard: Callable[[], None],
    on_save_click: Callable[[], None],
) -> None:
    """Ask how to handle local changes before an editor is left."""
    from nicegui import ui

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-lg gap-4 p-5"):
        ui.label("Discard unsaved changes?").classes("rbs-type-dialog-title")
        ui.label(f"Changes to {subject} will be lost unless you save them.").classes(
            "rbs-type-body rbs-text-muted"
        )

        def discard() -> None:
            dialog.close()
            on_discard()

        def save_and_leave() -> None:
            # Close first so a failed save leaves its error visible in the
            # editor instead of behind the confirmation dialog.
            dialog.close()
            on_save_click()

        with ui.row().classes("w-full flex-nowrap justify-end gap-2"):
            ui.button("Keep editing", on_click=dialog.close).props("flat no-caps")
            ui.button("Discard changes", on_click=discard).props(
                "flat no-caps color=negative"
            )
            ui.button(save_label, icon=save_icon, on_click=save_and_leave).props(
                "unelevated no-caps"
            )
    dialog.open()


@dataclass
class EditorGuard:
    """Dirty-state callbacks published by an editor to its navigation owner."""

    is_dirty: Callable[[], bool] | None = None
    save: Callable[[], bool] | None = None
    subject: str = "this item"
    save_label: str = "Save changes"
    save_icon: str = "save"

    def clear(self) -> None:
        self.is_dirty = None
        self.save = None


def confirm_guarded_navigation(
    guard: EditorGuard | None,
    proceed: Callable[[], None],
) -> None:
    """Navigate immediately when clean, or resolve a dirty draft first."""
    if guard is None or guard.is_dirty is None or not guard.is_dirty():
        proceed()
        return

    def discard_and_continue() -> None:
        guard.clear()
        proceed()

    def save_and_continue() -> None:
        saver = guard.save
        if saver is not None and saver():
            proceed()

    confirm_close_editor(
        subject=guard.subject,
        save_label=guard.save_label,
        save_icon=guard.save_icon,
        on_discard=discard_and_continue,
        on_save_click=save_and_continue,
    )
