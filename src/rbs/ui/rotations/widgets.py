"""Reusable rotation-rule widgets shared by Rotation and Clinic editors."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial

from rbs.models.clinic import ALL_CLINIC_SITES
from rbs.models.color_scheme import contrasting_text_color
from rbs.models.enums import RotationKind, Session, Weekday
from rbs.models.instance import SchedulerInput
from rbs.ui.drafts import Draft
from rbs.ui.editor_common import (
    _CLINIC_WEEK,
    _DEFAULT_BLOCK_DURATION_WEEKS,
    _SESSION_OPTIONS,
    _WEEKDAY_OPTIONS,
    _default_block_duration,
)


def rotation_code_style(color: str) -> str:
    """Return accessible scoped colors for a rotation-code avatar."""
    return (
        f"--rbs-rotation-code-color:{color};"
        f"--rbs-rotation-code-foreground:{contrasting_text_color(color)}"
    )


def rotation_color_palette(
    draft: Draft,
    palette: Mapping[str, str],
    *,
    on_change: Callable[[str], None] | None = None,
    label: str = "Block schedule color",
    compact: bool = False,
) -> None:
    """Render the schedule-color chooser from the workspace's saved scheme."""
    from nicegui import ui

    options = list(palette)
    current = str(draft.get("color") or "").upper()
    if current and current not in options:
        options.append(current)
    container = ui.column().classes(
        "rbs-rotation-color-palette" + (" is-compact" if compact else "") + " w-full gap-2"
    )

    def choose(color: str) -> None:
        draft["color"] = color
        if on_change is not None:
            on_change(color)
        else:
            render()

    def render() -> None:
        selected = str(draft.get("color") or "").upper()
        container.clear()

        def color_choices() -> None:
            with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                for color in options:
                    is_selected = color == selected
                    button = (
                        ui.button(
                            icon="check" if is_selected else None,
                            color=None,
                            on_click=partial(choose, color),
                        )
                        .props(
                            "round unelevated "
                            f"aria-label='Select {color} schedule color' "
                            f"aria-pressed={'true' if is_selected else 'false'}"
                        )
                        .classes(
                            "rbs-rotation-color-choice" + (" is-selected" if is_selected else "")
                        )
                        .style(
                            f"--rbs-rotation-choice-color:{color};"
                            f"--rbs-rotation-choice-foreground:"
                            f"{contrasting_text_color(color)}"
                        )
                    )
                    with button:
                        ui.tooltip(color)

        with container:
            if compact:
                with ui.row().classes(
                    "rbs-rotation-color-compact w-full items-center gap-3 rounded p-3"
                ):
                    ui.element("span").classes("rbs-rotation-color-swatch-large").style(
                        f"--rbs-rotation-choice-color:{selected}"
                    )
                    with ui.column().classes("min-w-0 flex-1 gap-0"):
                        ui.label(label).classes("rbs-type-control-label")
                        ui.label(selected).classes("rbs-type-caption rbs-text-muted")
                    with ui.button("Change", icon="palette").props("outline dense no-caps"):
                        with ui.menu().classes("rbs-rotation-color-menu p-3"):
                            color_choices()
            else:
                ui.label(label).classes("rbs-type-control-label")
                color_choices()

    render()


def clinic_week_editor(
    rule: Draft,
    *,
    academic_half_day: tuple[Weekday | None, Session | None] | None,
    site_options: dict[str, str],
    default_site_ids: list[str],
) -> None:
    from nicegui import ui

    container = ui.element("div").classes("rbs-clinic-week-grid w-full min-w-0 max-w-full")

    def render() -> None:
        container.clear()
        slots = clinic_slots_by_time(rule)
        with container:
            for weekday in _CLINIC_WEEK:
                with ui.card().props("flat bordered").classes("rbs-clinic-day min-w-0 gap-2 p-3"):
                    ui.label(_WEEKDAY_OPTIONS[weekday.value]).classes(
                        "w-full text-center rbs-type-control-label"
                    )
                    for session in Session:
                        key = (weekday.value, session.value)
                        slot = slots.get(key)
                        is_academic = academic_half_day == (weekday, session)
                        classes = (
                            "rbs-clinic-half-day rbs-clinic-half-day-summary w-full "
                            "gap-1 rounded p-2"
                        )
                        if slot is not None:
                            classes += " is-enabled"
                        if is_academic:
                            classes += " rbs-academic-half-day"
                        with ui.column().classes(classes):
                            with ui.row().classes(
                                "w-full items-center justify-between gap-1 flex-nowrap"
                            ):
                                enabled = ui.checkbox(
                                    _SESSION_OPTIONS[session.value],
                                    value=slot is not None,
                                ).props("dense")
                                enabled.set_enabled(not is_academic)
                                if slot is not None and not is_academic:
                                    ui.button(
                                        icon="edit",
                                        on_click=partial(
                                            _open_clinic_slot_dialog,
                                            rule,
                                            weekday,
                                            session,
                                            render,
                                            site_options,
                                            default_site_ids,
                                        ),
                                    ).props(
                                        "flat round dense size=sm "
                                        f"aria-label='Edit {_WEEKDAY_OPTIONS[weekday.value]} "
                                        f"{_SESSION_OPTIONS[session.value]} clinic slot'"
                                    )
                            if is_academic:
                                ui.label("Academic Day").classes(
                                    "rbs-academic-day-label w-full text-center rbs-type-caption "
                                    "rbs-font-semibold"
                                )
                            elif slot is None:
                                ui.label("Not allowed").classes("rbs-type-caption rbs-text-subtle")
                            else:
                                selected_sites = display_site_ids(
                                    list(slot.get("sites") or []),
                                    default_site_ids,
                                )
                                site_names = [
                                    site_options.get(site_id, site_id) for site_id in selected_sites
                                ]
                                ui.label(" · ".join(site_names)).classes(
                                    "rbs-clinic-slot-site-summary rbs-type-caption"
                                )
                                if slot.get("preferred"):
                                    ui.label("★ Preferred").classes(
                                        "rbs-type-caption rbs-font-semibold rbs-text-primary"
                                    )
                        if not is_academic:
                            enabled.on_value_change(
                                partial(
                                    _set_clinic_half_day_enabled,
                                    rule,
                                    weekday,
                                    session,
                                    default_site_ids,
                                    render,
                                )
                            )

    render()


def _set_clinic_half_day_enabled(
    rule: Draft,
    weekday: Weekday,
    session: Session,
    default_site_ids: list[str],
    refresh: Callable[[], None],
    event,
) -> None:
    key = (weekday.value, session.value)
    slots = clinic_slots_by_time(rule)
    if event.value and key not in slots:
        rule.setdefault("slots", []).append(
            {
                "weekday": weekday.value,
                "session": session.value,
                "sites": list(default_site_ids),
                "preferred": False,
            }
        )
        _sort_clinic_slots(rule)
    elif not event.value:
        rule["slots"] = [
            slot
            for slot in rule.get("slots", [])
            if (slot.get("weekday"), slot.get("session")) != key
        ]
    refresh()


def _open_clinic_slot_dialog(
    rule: Draft,
    weekday: Weekday,
    session: Session,
    refresh: Callable[[], None],
    site_options: dict[str, str],
    default_site_ids: list[str],
) -> None:
    from nicegui import ui

    slot = clinic_slots_by_time(rule).get((weekday.value, session.value))
    if slot is None:
        return
    selected_sites = display_site_ids(list(slot.get("sites") or []), default_site_ids)
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-lg gap-4 p-5"):
        with ui.column().classes("gap-0"):
            ui.label(
                f"{_WEEKDAY_OPTIONS[weekday.value]} · {_SESSION_OPTIONS[session.value]}"
            ).classes("rbs-type-dialog-title")
            ui.label("Allowed clinic slot").classes("rbs-type-caption rbs-text-muted")
        sites = (
            ui.select(
                site_options,
                value=selected_sites,
                label="Clinic sites",
                multiple=True,
            )
            .props("outlined options-dense use-chips")
            .classes("w-full")
        )
        preferred = ui.checkbox(
            "Preferred clinic time",
            value=bool(slot.get("preferred")),
        )
        ui.label(
            "Preferred is a soft scheduling goal; this slot remains an allowed fallback."
        ).classes("rbs-type-caption rbs-text-muted")
        status = ui.label().classes("rbs-type-caption rbs-text-danger")

        def apply() -> None:
            selected = [str(site_id) for site_id in (sites.value or [])]
            if not selected:
                status.set_text("Select at least one clinic site.")
                return
            slot["sites"] = selected
            slot["preferred"] = bool(preferred.value)
            dialog.close()
            refresh()

        with ui.row().classes("w-full items-center justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
            ui.button("Apply slot", icon="done", on_click=apply).props("unelevated no-caps")
    dialog.open()


def clinic_slots_by_time(rule: Draft) -> dict[tuple[str, str], Draft]:
    return {
        (str(slot.get("weekday")), str(slot.get("session"))): slot
        for slot in rule.get("slots", [])
        if isinstance(slot, dict) and slot.get("weekday") and slot.get("session")
    }


def set_clinic_slot_preferred(
    rule: Draft,
    weekday: Weekday,
    session: Session,
    event,
) -> None:
    slot = clinic_slots_by_time(rule).get((weekday.value, session.value))
    if slot is not None:
        slot["preferred"] = bool(event.value)


def _sort_clinic_slots(rule: Draft) -> None:
    day_order = {weekday.value: index for index, weekday in enumerate(_CLINIC_WEEK)}
    session_order = {session.value: index for index, session in enumerate(Session)}
    rule["slots"].sort(
        key=lambda slot: (
            day_order.get(str(slot.get("weekday")), len(day_order)),
            session_order.get(str(slot.get("session")), len(session_order)),
        )
    )


def display_site_ids(site_ids: list[str], configured_site_ids: list[str]) -> list[str]:
    if not site_ids or ALL_CLINIC_SITES in site_ids:
        return list(configured_site_ids)
    configured = set(configured_site_ids)
    return [site_id for site_id in dict.fromkeys(site_ids) if site_id in configured]


def prerequisite_options(
    instance: SchedulerInput,
    rotation_id: str,
    pgy: int,
) -> dict[str, str]:
    offered = instance.rotation_ids_for_pgy(pgy)
    return {
        rotation.id: f"{rotation.code} — {rotation.name}"
        for rotation in sorted(instance.rotations, key=lambda item: item.code.casefold())
        if rotation.id != rotation_id and rotation.id in offered
        if rotation.kind is not RotationKind.ELECTIVE or instance.is_elective_option(rotation.id)
    }


def toggle_pgy_rule(
    draft: Draft,
    pgy: int,
    training_level_ids: tuple[int, ...],
    refresh: Callable[[], None],
    event,
) -> None:
    rules = draft["pgy_rules"]
    rules[:] = [rule for rule in rules if int(rule["pgy"]) != pgy]
    if event.value:
        rules.append(
            {
                "pgy": pgy,
                "min_concurrent": None,
                "max_concurrent": None,
                "max_total_weeks": None,
                "block_configs": [
                    {
                        "duration_weeks": _DEFAULT_BLOCK_DURATION_WEEKS,
                        "vacation": {
                            "allowed": False,
                            "max_weeks_per_block": None,
                        },
                    }
                ],
                "prerequisite_rotation_ids": [],
                "earliest_start_week": None,
            }
        )
        order = {level: index for index, level in enumerate(training_level_ids)}
        rules.sort(key=lambda rule: order[int(rule["pgy"])])
    refresh()


def add_block_config(rule: Draft, refresh: Callable[[], None]) -> None:
    used = {int(config["duration_weeks"]) for config in rule["block_configs"]}
    duration = _default_block_duration(
        candidate for candidate in [2, 4, 1, 3, 5] if candidate not in used
    )
    if duration is None:
        return
    rule["block_configs"].append(
        {
            "duration_weeks": duration,
            "vacation": {"allowed": False, "max_weeks_per_block": None},
        }
    )
    rule["block_configs"].sort(key=lambda config: int(config["duration_weeks"]))
    refresh()


def set_block_vacation_allowed(
    vacation: Draft,
    refresh: Callable[[], None],
    event,
) -> None:
    vacation["allowed"] = bool(event.value)
    if event.value and vacation.get("max_weeks_per_block") is None:
        vacation["max_weeks_per_block"] = 1
    elif not event.value:
        vacation["max_weeks_per_block"] = None
    refresh()
