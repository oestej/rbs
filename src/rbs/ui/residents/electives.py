"""Resident Elective preference stack editor."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

from pydantic import ValidationError

from rbs.models.instance import SchedulerInput
from rbs.models.resident import ElectivePreferenceRequest, Resident
from rbs.models.schedule import Schedule

SaveResidentSchedule = Callable[[SchedulerInput, str, bool], None]

_PREFERENCE_DRAG_START_JS = """
(event) => {
  const rank = event.currentTarget.dataset.preferenceRank;
  if (rank === undefined) {
    event.preventDefault();
    return;
  }
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('application/x-rbs-elective-preference', rank);
  event.dataTransfer.setData('text/plain', rank);
  event.currentTarget.closest('.rbs-elective-preference-row')?.classList.add('is-dragging');
}
"""
_PREFERENCE_DRAG_END_JS = """
() => {
  document.querySelectorAll(
    '.rbs-elective-preference-row.is-dragging, '
    + '.rbs-elective-preference-row.is-drag-over-before, '
    + '.rbs-elective-preference-row.is-drag-over-after'
  ).forEach((row) => row.classList.remove(
    'is-dragging', 'is-drag-over-before', 'is-drag-over-after'
  ));
}
"""
_PREFERENCE_DRAG_OVER_JS = """
(event) => {
  const types = Array.from(event.dataTransfer.types || []);
  if (!types.includes('application/x-rbs-elective-preference')) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = 'move';
  const rect = event.currentTarget.getBoundingClientRect();
  const before = event.clientY < rect.top + rect.height / 2;
  event.currentTarget.classList.toggle('is-drag-over-before', before);
  event.currentTarget.classList.toggle('is-drag-over-after', !before);
}
"""
_PREFERENCE_DRAG_LEAVE_JS = """
(event) => {
  if (!event.currentTarget.contains(event.relatedTarget)) {
    event.currentTarget.classList.remove('is-drag-over-before', 'is-drag-over-after');
  }
}
"""
_PREFERENCE_DROP_JS = """
(event) => {
  event.preventDefault();
  const row = event.currentTarget;
  const source = Number(event.dataTransfer.getData(
    'application/x-rbs-elective-preference'
  ));
  const target = Number(row.dataset.preferenceRank);
  const rect = row.getBoundingClientRect();
  const position = event.clientY < rect.top + rect.height / 2 ? 'before' : 'after';
  row.classList.remove('is-drag-over-before', 'is-drag-over-after');
  if (Number.isInteger(source) && Number.isInteger(target)) {
    emit({source, target, position});
  }
}
"""


def replace_elective_preferences(
    instance: SchedulerInput,
    resident_id: str,
    preferences: list[ElectivePreferenceRequest],
) -> SchedulerInput:
    """Save one resident's ordered preference stack and revalidate it."""
    resident = instance.residents_by_id.get(resident_id)
    if resident is None:
        raise ValueError(f"unknown resident {resident_id!r}")
    replacement = resident.model_copy(update={"elective_preferences": preferences})
    return instance.revised(
        residents=[replacement if item.id == resident_id else item for item in instance.residents]
    )


def elective_preference_options(
    instance: SchedulerInput,
    resident: Resident,
) -> dict[str, str]:
    """Return service/shape choices currently compatible with this resident."""
    inventory = instance.direct_elective_block_counts_for_pgy(resident.pgy)
    options: dict[str, str] = {}
    for option in instance.electives.rotation_options:
        rotation = instance.rotation(option.rotation_id)
        for duration in inventory:
            if option.allows(resident.pgy, duration) and rotation.allows_duration(
                duration, pgy=resident.pgy
            ):
                options[f"{rotation.id}|{duration}"] = (
                    f"{rotation.code} · {rotation.name} · {duration} weeks"
                )
    return options


def render_elective_preferences(
    instance: SchedulerInput,
    schedule: Schedule | None,
    resident: Resident,
    *,
    on_schedule_save: SaveResidentSchedule | None,
    schedule_is_current: bool,
) -> None:
    """Render one resident's stack-ranked Elective request editor."""
    from nicegui import ui

    inventory = instance.direct_elective_block_counts_for_pgy(resident.pgy)
    options = elective_preference_options(instance, resident)
    preferences = list(resident.elective_preferences)

    with ui.column().classes(
        "rbs-resident-schedule-content rbs-elective-preferences w-full min-w-0 gap-4 p-5"
    ):
        with ui.column().classes("gap-1"):
            ui.label("Elective preferences").classes("rbs-type-dialog-title")
            ui.label(
                "Stack-rank the services this resident wants. Each row requests one "
                "block. A service can be repeated only when its Elective policy allows "
                "it. Unranked services are excluded from matching."
            ).classes("rbs-type-body rbs-text-muted")

        if not inventory:
            _empty_state(
                "No direct Elective blocks",
                "This training level has no direct Elective inventory to rank.",
            )
            return

        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
            ui.label("Available inventory").classes("rbs-type-control-label")
            for duration, count in inventory.items():
                ui.badge(
                    f"{count} × {duration}-week block{'s' if count != 1 else ''}",
                    color="primary",
                ).props("outline")

        fallback_assignments = [
            assignment
            for assignment in (schedule.assignments if schedule is not None else [])
            if assignment.resident_id == resident.id and assignment.elective_fallback
        ]
        if fallback_assignments:
            weeks = ", ".join(
                f"{assignment.start_week}–{assignment.end_week}"
                if assignment.start_week != assignment.end_week
                else str(assignment.start_week)
                for assignment in sorted(
                    fallback_assignments,
                    key=lambda assignment: assignment.start_week,
                )
            )
            with ui.row().classes(
                "rbs-elective-fallback-summary w-full items-start gap-3 rounded p-3"
            ):
                ui.icon("info").props("size=20px")
                with ui.column().classes("gap-0"):
                    ui.label(
                        f"{len(fallback_assignments)} Clinic (Elective fallback) "
                        f"block{'s' if len(fallback_assignments) != 1 else ''}"
                    ).classes("rbs-font-semibold")
                    ui.label(
                        f"{'Current' if schedule_is_current else 'Last solved'} schedule · "
                        f"week{'s' if ',' in weeks or '–' in weeks else ''} {weeks}"
                    ).classes("rbs-type-caption rbs-text-muted")

        if not options:
            _empty_state(
                "No eligible services",
                "Configure services under Rotations › Electives before adding requests.",
            )
            return

        stack = ui.column().classes("w-full gap-2")

        def move(source: int, target: int, position: str = "before") -> None:
            if source == target or not (0 <= source < len(preferences)):
                return
            item = preferences.pop(source)
            if source < target:
                target -= 1
            insert_at = target + (1 if position == "after" else 0)
            preferences.insert(max(0, min(insert_at, len(preferences))), item)
            render_stack()

        def handle_drop(event) -> None:
            payload = event.args if isinstance(event.args, dict) else {}
            try:
                move(
                    int(payload.get("source")),
                    int(payload.get("target")),
                    str(payload.get("position")),
                )
            except (TypeError, ValueError):
                ui.notify("Unable to reorder that preference", type="negative")

        def render_stack() -> None:
            stack.clear()
            with stack:
                ui.label("Ranked requests").classes("rbs-type-control-label")
                if not preferences:
                    with ui.row().classes(
                        "rbs-elective-preference-empty w-full items-start gap-3 rounded p-4"
                    ):
                        ui.icon("low_priority").props("size=22px")
                        with ui.column().classes("gap-0"):
                            ui.label("No services ranked").classes("rbs-font-semibold")
                            ui.label(
                                "All direct Elective blocks will use Clinic "
                                "(Elective fallback) on the next solve."
                            ).classes("rbs-type-body rbs-text-muted")
                    return
                for rank, request in enumerate(preferences):
                    rotation = instance.rotation(request.rotation_id)
                    row = (
                        ui.card()
                        .props(f"flat bordered data-preference-rank={rank}")
                        .classes("rbs-elective-preference-row w-full px-3 py-2")
                    )
                    row.on("dragover", js_handler=_PREFERENCE_DRAG_OVER_JS)
                    row.on("dragleave", js_handler=_PREFERENCE_DRAG_LEAVE_JS)
                    row.on("drop", handle_drop, js_handler=_PREFERENCE_DROP_JS)
                    with row:
                        with ui.row().classes("w-full items-center gap-2 no-wrap"):
                            handle = (
                                ui.button(icon="drag_indicator")
                                .props(
                                    "flat round dense draggable=true "
                                    f"data-preference-rank={rank} "
                                    f"aria-label='Drag preference {rank + 1} to reorder'"
                                )
                                .classes("rbs-elective-preference-drag-handle")
                            )
                            handle.on("dragstart", js_handler=_PREFERENCE_DRAG_START_JS)
                            handle.on("dragend", js_handler=_PREFERENCE_DRAG_END_JS)
                            ui.badge(str(rank + 1), color="primary").props("outline")
                            with ui.column().classes("min-w-0 flex-1 gap-0"):
                                ui.label(f"{rotation.code} · {rotation.name}").classes(
                                    "rbs-font-semibold"
                                )
                                ui.label(f"{request.duration_weeks}-week block").classes(
                                    "rbs-type-caption rbs-text-muted"
                                )
                            up = ui.button(
                                icon="arrow_upward",
                                on_click=lambda _event=None, index=rank: move(
                                    index,
                                    index - 1,
                                ),
                            ).props(f"flat round dense aria-label='Move preference {rank + 1} up'")
                            up.set_enabled(rank > 0)
                            down = ui.button(
                                icon="arrow_downward",
                                on_click=lambda _event=None, index=rank: move(
                                    index,
                                    index + 1,
                                    "after",
                                ),
                            ).props(
                                f"flat round dense aria-label='Move preference {rank + 1} down'"
                            )
                            down.set_enabled(rank < len(preferences) - 1)
                            ui.button(
                                icon="delete_outline",
                                on_click=lambda _event=None, index=rank: (
                                    preferences.pop(index),
                                    render_stack(),
                                ),
                            ).props(
                                f"flat round dense color=negative "
                                f"aria-label='Remove preference {rank + 1}'"
                            )

        render_stack()

        selected = (
            ui.select(
                options,
                label="Add a service and block length",
            )
            .props("outlined options-dense")
            .classes("w-full max-w-xl")
        )

        def add_request() -> None:
            value = str(selected.value or "")
            if "|" not in value:
                ui.notify("Choose a service to add", type="warning")
                return
            rotation_id, duration_text = value.rsplit("|", 1)
            duration = int(duration_text)
            limit = inventory[duration]
            option = instance.electives.option_for(rotation_id)
            if option is None:
                ui.notify("That service is no longer eligible", type="warning")
                return
            if not option.repeatable and any(
                item.rotation_id == rotation_id for item in preferences
            ):
                ui.notify(
                    "That service can be requested only once per resident",
                    type="warning",
                )
                return
            used = Counter((item.rotation_id, item.duration_weeks) for item in preferences)
            if used[rotation_id, duration] >= limit:
                ui.notify(
                    f"That service can be requested at most {limit} time"
                    f"{'s' if limit != 1 else ''} for {duration}-week inventory",
                    type="warning",
                )
                return
            preferences.append(
                ElectivePreferenceRequest(
                    rotation_id=rotation_id,
                    duration_weeks=duration,
                )
            )
            selected.value = None
            render_stack()

        with ui.row().classes("items-center gap-2 flex-wrap"):
            ui.button("Add request", icon="add", on_click=add_request).props("outline no-caps")

            def save() -> None:
                if on_schedule_save is None:
                    return
                try:
                    updated = replace_elective_preferences(
                        instance,
                        resident.id,
                        preferences,
                    )
                    ui.notify("Elective preferences saved; solve required", type="positive")
                    on_schedule_save(updated, resident.id, False)
                except (ValidationError, ValueError) as exc:
                    ui.notify(str(exc), type="negative", multi_line=True)

            save_button = ui.button(
                "Save preferences",
                icon="save",
                on_click=save,
            ).props("unelevated no-caps")
            save_button.set_enabled(on_schedule_save is not None)


def _empty_state(title: str, description: str) -> None:
    from nicegui import ui

    with ui.column().classes(
        "rbs-elective-preference-empty w-full items-center gap-1 rounded p-6 text-center"
    ):
        ui.icon("playlist_remove").props("size=32px").classes("rbs-text-subtle")
        ui.label(title).classes("rbs-font-semibold")
        ui.label(description).classes("rbs-type-body rbs-text-muted")


__all__ = [
    "elective_preference_options",
    "render_elective_preferences",
    "replace_elective_preferences",
]
