"""Read-only rotation summary views and overview builders."""

from __future__ import annotations

import html
from collections.abc import Callable
from urllib.parse import urlencode

from rbs.models.clinic import ALL_CLINIC_SITES
from rbs.models.curriculum import (
    default_training_level_code,
    default_training_level_name,
)
from rbs.models.enums import RotationKind, Session, Weekday
from rbs.models.instance import SchedulerInput
from rbs.models.rotation import (
    ClinicSlot,
    Rotation,
)
from rbs.models.schedule import Schedule
from rbs.ui.editor_common import (
    _academic_block_name,
    _academic_block_start_for_week,
    _capacity_range_label,
    _vacation_label,
    _weeks_label,
)
from rbs.ui.rotations.ops import (
    resident_missing_mandatory_rotations,
    resident_rotation_week_totals,
)
from rbs.ui.rotations.widgets import rotation_code_style


def _rotation_summary(
    instance: SchedulerInput,
    *,
    schedule: Schedule | None = None,
    resident_edit_url: str | None = None,
) -> None:
    from nicegui import ui

    ui.html(
        _rotation_summary_html(
            instance,
            schedule=schedule,
            resident_edit_url=resident_edit_url,
        )
    ).classes("w-full")


def _rotation_summary_html(
    instance: SchedulerInput,
    *,
    schedule: Schedule | None = None,
    resident_edit_url: str | None = None,
) -> str:
    sections = []
    for pgy in instance.training_level_ids:
        residents = sorted(
            (resident for resident in instance.residents if resident.pgy == pgy),
            key=lambda resident: resident.name.casefold(),
        )
        if not residents:
            continue
        rows = []
        for index, resident in enumerate(residents):
            totals = resident_rotation_week_totals(instance, resident.id)
            missing_count, missing_labels = resident_missing_mandatory_rotations(
                instance,
                schedule,
                resident.id,
            )
            total_weeks = sum(totals.values())
            pgy_cell = (
                f'<th class="rbs-pgy-label" scope="rowgroup" rowspan="{len(residents)}">'
                f"<span>{html.escape(instance.training_level_label(pgy, compact=True))}</span>"
                "</th>"
                if index == 0
                else ""
            )
            values = (
                _week_total_label(totals["mandatory"]),
                _missing_mandatory_html(missing_count, missing_labels),
                _week_total_label(totals["elective"]),
                _week_total_label(totals["clinic"]),
                _resident_time_off_label(instance, resident),
                _rotation_total_label(total_weeks, expected=instance.calendar.weeks),
            )
            status = "is-complete" if total_weeks == instance.calendar.weeks else "is-incomplete"
            cells = "".join(
                f'<td class="{_rotation_summary_cell_class(position, values, status)}">'
                f"{value if position == 1 else html.escape(value)}</td>"
                for position, value in enumerate(values)
            )
            resident_name = html.escape(resident.name)
            if resident_edit_url is not None:
                separator = "&" if "?" in resident_edit_url else "?"
                query = urlencode({"resident": resident.id})
                href = html.escape(f"{resident_edit_url}{separator}{query}", quote=True)
                title = html.escape(f"Edit resident {resident.name}", quote=True)
                resident_name = (
                    f'<a class="rbs-resident-link" href="{href}" title="{title}" '
                    f'aria-label="{title}">{resident_name}</a>'
                )
            rows.append(
                f'<tr class="rbs-resident-row" data-pgy="{resident.pgy}">'
                f"{pgy_cell}"
                f'<th class="rbs-resident-name" scope="row">'
                f"{resident_name}</th>"
                f"{cells}</tr>"
            )
        sections.append(f'<tbody class="rbs-pgy-section">{"".join(rows)}</tbody>')
    return (
        '<div class="rbs-grid-wrap rbs-rotation-summary-wrap">'
        '<table class="rbs-grid rbs-rotation-summary-table">'
        '<colgroup><col class="pgy"><col class="resident">'
        '<col class="time"><col class="missing"><col class="time"><col class="time">'
        '<col class="time"><col class="time"></colgroup>'
        '<thead><tr><th class="rbs-pgy-column" aria-label="Training level"></th>'
        '<th class="rbs-resident-column">Resident</th>'
        "<th>Mandatory</th>"
        '<th class="rbs-missing-mandatory-column">Missing mandatory</th>'
        "<th>Elective</th><th>Clinic</th>"
        '<th title="Time off occurs within the rotation totals">Time Off (included)</th>'
        "<th>Total</th></tr></thead>"
        f"{''.join(sections)}</table></div>"
    )


def _rotation_summary_cell_class(
    position: int,
    values: tuple[str, str, str, str, str, str],
    status: str,
) -> str:
    if position == 1:
        return "rbs-missing-mandatory-cell"
    if position == len(values) - 1:
        return status
    return ""


def _missing_mandatory_html(count: int, labels: list[str]) -> str:
    count_badge = (
        '<span class="rbs-missing-mandatory-count">'
        f"<strong>{count}</strong><small>missing</small></span>"
    )
    if count == 0:
        return (
            '<span class="rbs-missing-mandatory is-complete">'
            f'{count_badge}<span class="rbs-missing-mandatory-complete">'
            "Complete</span></span>"
        )
    details = "".join(
        f'<span class="rbs-missing-mandatory-item">{html.escape(label)}</span>' for label in labels
    )
    return (
        '<span class="rbs-missing-mandatory is-incomplete">'
        f'{count_badge}<span class="rbs-missing-mandatory-list">'
        f"{details}</span></span>"
    )


def _week_total_label(total: int) -> str:
    return _weeks_label(total)


def _resident_time_off_label(instance: SchedulerInput, resident) -> str:
    vacation = _weeks_label(len(resident.vacation_weeks))
    day_count = len(resident.days_off)
    parts = [vacation]
    if day_count:
        parts.append(f"{day_count} {'day' if day_count == 1 else 'days'}")
    special_count = len(instance.special_rotations_for_resident(resident.id))
    if special_count:
        parts.append(f"{special_count} special {'item' if special_count == 1 else 'items'}")
    return " · ".join(parts)


def _rotation_total_label(weeks: int, *, expected: int = 52) -> str:
    marker = "✅" if weeks == expected else "❌"
    return f"{marker} {_weeks_label(weeks)}"


def _rotation_kind_label(rotation: Rotation) -> str | None:
    labels = {
        RotationKind.STANDARD: None,
        RotationKind.CLINIC: "Dedicated Clinic configuration",
        RotationKind.FMED: None,
        RotationKind.ELECTIVE: "Dedicated Elective configuration",
    }
    return labels[rotation.kind]


def _configured_training_level_label(
    rotation: Rotation,
    instance: SchedulerInput | None = None,
    *,
    compact: bool = False,
) -> str:
    return ", ".join(
        instance.training_level_label(rule.pgy, compact=compact)
        if instance is not None
        else (
            default_training_level_code(rule.pgy)
            if compact
            else default_training_level_name(rule.pgy)
        )
        for rule in rotation.pgy_rules
    )


def _configured_duration_label(rotation: Rotation) -> str:
    durations = rotation.configured_durations()
    labels = [f"{duration}-week" for duration in durations]
    if len(labels) == 1:
        return f"{labels[0]} blocks"
    return f"{', '.join(labels)} blocks"


def _eligible_elective_block_size_label(sizes: list[int] | tuple[int, ...]) -> str:
    if not sizes:
        return "Not available as an elective"
    return "Eligible elective block sizes · " + ", ".join(_weeks_label(size) for size in sizes)


def _elective_policy_summary_chips(
    instance: SchedulerInput,
    rotation_id: str,
) -> None:
    levels = ", ".join(
        instance.training_level_label(pgy, compact=True)
        for pgy in instance.eligible_elective_pgys(rotation_id)
    )
    _rotation_summary_chip(f"Elective for {levels}")
    _rotation_summary_chip(
        _eligible_elective_block_size_label(instance.eligible_elective_block_sizes(rotation_id))
    )
    _rotation_summary_chip(
        "May repeat as an elective"
        if instance.elective_option_is_repeatable(rotation_id)
        else "One elective block per resident"
    )


def _elective_block_size_options(sizes: tuple[int, ...]) -> dict[int, str]:
    return {size: _weeks_label(size) for size in sizes}


def _rotation_identity(
    rotation: Rotation,
    *,
    instance: SchedulerInput | None = None,
    editing: bool = False,
) -> None:
    from nicegui import ui

    with ui.row().classes("rbs-rotation-identity min-w-0 items-center gap-4"):
        with (
            ui.avatar(color=None)
            .props("square")
            .classes("rbs-rotation-code-avatar rbs-rotation-code-avatar-large")
            .style(rotation_code_style(rotation.color))
        ):
            ui.label(rotation.code).classes("rbs-rotation-code-text")
        with ui.column().classes("min-w-0 gap-1"):
            with ui.row().classes("items-center gap-2"):
                ui.label(rotation.name).classes("rbs-type-page-title")
                if editing:
                    ui.badge("Editing", color="secondary").props("outline")
            ui.label(
                f"{_configured_training_level_label(rotation, instance)} · "
                f"{_configured_duration_label(rotation)}"
            ).classes("rbs-type-caption rbs-text-muted")


def _rotation_requirement_label(
    instance: SchedulerInput,
    rotation_id: str,
    pgy: int,
) -> str:
    curriculum = instance.curriculum_for(pgy)
    direct = [block for block in curriculum.blocks if block.rotation_id == rotation_id]
    direct_labels = [
        f"{block.count} × {_weeks_label(block.duration_weeks)}"
        for block in sorted(direct, key=lambda item: item.duration_weeks)
    ]
    if direct_labels:
        return "Required " + " + ".join(direct_labels)
    return "Not required program-wide"


def _rotation_summary_chip(label: str) -> None:
    from nicegui import ui

    ui.badge(label).props("outline").classes("rbs-rotation-summary-chip rbs-muted-badge")


def _rotation_overview_row(label: str, value: str, *, icon: str | None = None) -> None:
    from nicegui import ui

    with ui.row().classes("rbs-rotation-overview-row w-full items-start gap-3"):
        if icon is not None:
            ui.icon(icon).classes("rbs-rotation-overview-icon rbs-text-subtle")
        with ui.column().classes("min-w-0 flex-1 gap-0"):
            ui.label(label).classes("rbs-type-caption rbs-font-semibold uppercase rbs-text-muted")
            ui.label(value).classes("rbs-type-body")


def _rotation_pgy_overview(
    instance: SchedulerInput,
    rotation: Rotation,
    *,
    on_edit: Callable[[], None] | None = None,
) -> None:
    from nicegui import ui

    with ui.column().classes("w-full gap-3"):
        with ui.row().classes("w-full items-center justify-between gap-3"):
            ui.label(
                "Elective Rules"
                if rotation.kind is RotationKind.ELECTIVE
                else "Training-level rules"
            ).classes("rbs-type-section-title")
            if on_edit is not None:
                ui.button("Edit rules", icon="edit", on_click=on_edit).props(
                    "outline dense no-caps"
                )
        with ui.element("div").classes("rbs-rotation-pgy-grid w-full"):
            for rule in rotation.pgy_rules:
                with (
                    ui.card()
                    .props("flat bordered")
                    .classes("rbs-rotation-overview-card rbs-rotation-pgy-card gap-3 p-4")
                ):
                    with ui.row().classes("w-full items-start justify-between gap-3"):
                        ui.label(instance.training_level_name(rule.pgy)).classes(
                            "rbs-type-control-label"
                        )
                        if rotation.kind is not RotationKind.ELECTIVE:
                            ui.badge(
                                _rotation_requirement_label(
                                    instance,
                                    rotation.id,
                                    rule.pgy,
                                ),
                                color="secondary",
                            ).props("outline")
                    block_summary = "; ".join(
                        f"{_weeks_label(config.duration_weeks)} · "
                        f"{_vacation_label(config.vacation)}"
                        for config in rule.block_configs
                    )
                    _rotation_overview_row("Block format", block_summary, icon="view_week")
                    _rotation_overview_row(
                        "Staffing",
                        _capacity_range_label(rule.min_concurrent, rule.max_concurrent),
                        icon="groups",
                    )
                    if rule.max_total_weeks is not None:
                        _rotation_overview_row(
                            "Maximum total weeks",
                            _weeks_label(rule.max_total_weeks),
                            icon="event_repeat",
                        )
                    prerequisites = (
                        "After "
                        + ", ".join(
                            instance.rotation(rotation_id).code
                            for rotation_id in rule.prerequisite_rotation_ids
                        )
                        if rule.prerequisite_rotation_ids
                        else "No prerequisites"
                    )
                    earliest_block = _academic_block_start_for_week(
                        rule.earliest_start_week,
                        instance.calendar.weeks,
                    )
                    start = (
                        "earliest "
                        + _academic_block_name(earliest_block).replace("Block ", "block ", 1)
                        if earliest_block is not None
                        else "may start in any block"
                    )
                    _rotation_overview_row(
                        "Placement",
                        f"{prerequisites} · {start}",
                        icon="event_available",
                    )


def _clinic_slot_label(slot: ClinicSlot) -> str:
    weekday = Weekday(str(slot.weekday))
    session = Session(str(slot.session))
    day = weekday.value[:3].title()
    time = "AM" if session is Session.MORNING else "PM"
    return f"{'★ ' if slot.preferred else ''}{day} {time}"


def _rotation_clinic_overview(instance: SchedulerInput, rotation: Rotation) -> None:
    from nicegui import ui

    with ui.card().props("flat bordered").classes("rbs-rotation-overview-card w-full gap-3 p-4"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.icon("event_available").classes("rbs-text-primary")
            ui.label("Continuity clinic").classes("rbs-type-control-label")
        if rotation.clinic_hours_disabled:
            ui.label("No clinic hours during this rotation.").classes(
                "rbs-type-body rbs-text-muted"
            )
            return
        if rotation.clinic is None:
            ui.label("No continuity clinic is configured.").classes("rbs-type-body rbs-text-muted")
            return

        rule = rotation.clinic
        ui.label(
            f"{rule.half_days_per_week} "
            f"{'half-day' if rule.half_days_per_week == 1 else 'half-days'} per week · "
            f"{len(rule.slots)} allowed slots"
        ).classes("rbs-type-body rbs-font-semibold")
        with ui.row().classes("w-full gap-2 flex-wrap"):
            for slot in rule.slots:
                badge = ui.badge(
                    _clinic_slot_label(slot),
                    color="secondary" if slot.preferred else None,
                ).props("outline")
                if not slot.preferred:
                    badge.classes("rbs-muted-badge")

        site_ids: set[str] = set()
        for slot in rule.slots:
            if ALL_CLINIC_SITES in slot.sites:
                site_ids.update(instance.clinic_policy.site_ids)
            else:
                site_ids.update(slot.sites)
        site_names = [
            instance.clinic_policy.site(site_id).name
            for site_id in instance.clinic_policy.site_ids
            if site_id in site_ids
        ]
        _rotation_overview_row(
            "Clinic sites",
            ", ".join(site_names) if site_names else "No sites configured",
            icon="location_on",
        )
        clinic_flags: list[str] = []
        if rule.max_concurrent is not None:
            noun = "resident" if rule.max_concurrent == 1 else "residents"
            clinic_flags.append(f"At most {rule.max_concurrent} {noun} per clinic half-day")
        for pgy in sorted(
            rule.max_concurrent_by_pgy,
            key=instance.training_level_sort_key,
        ):
            maximum = rule.max_concurrent_by_pgy[pgy]
            noun = "resident" if maximum == 1 else "residents"
            clinic_flags.append(
                f"At most {maximum} {instance.training_level_name(pgy)} {noun} per clinic half-day"
            )
        if rule.no_academic_day_attendance:
            clinic_flags.append("No academic-day attendance")
        if rule.admin_half_days_per_week:
            clinic_flags.append(
                f"{rule.admin_half_days_per_week} admin half-day"
                + ("s" if rule.admin_half_days_per_week != 1 else "")
            )
        if clinic_flags:
            ui.label(" · ".join(clinic_flags)).classes("rbs-type-caption rbs-text-muted")


def _rotation_operational_overview(rotation: Rotation) -> None:
    from nicegui import ui

    with ui.card().props("flat bordered").classes("rbs-rotation-overview-card w-full gap-3 p-4"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.icon("tune").classes("rbs-text-primary")
            ui.label("Operational rules").classes("rbs-type-control-label")
        with ui.row().classes("items-center gap-2"):
            ui.element("span").classes("rbs-rotation-color-swatch").style(
                f"--rbs-rotation-choice-color:{rotation.color}"
            )
            ui.label(rotation.color).classes("rbs-type-body")
        _rotation_overview_row(
            "Consecutive limit",
            _weeks_label(rotation.max_consecutive_weeks),
            icon="date_range",
        )
        if rotation.max_total_weeks is not None:
            _rotation_overview_row(
                "Maximum total weeks",
                _weeks_label(rotation.max_total_weeks),
                icon="event_repeat",
            )
        _rotation_overview_row(
            "All-year staffing",
            _capacity_range_label(
                rotation.capacity.min_concurrent,
                rotation.capacity.max_concurrent,
            ),
            icon="groups",
        )
        active_flags = [
            label
            for enabled, label in (
                (rotation.away, "Away rotation"),
                (rotation.no_weekend_call, "No weekend call"),
            )
            if enabled
        ]
        if active_flags:
            with ui.row().classes("w-full gap-2 flex-wrap"):
                for label in active_flags:
                    _rotation_summary_chip(label)
