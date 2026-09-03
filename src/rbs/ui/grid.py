"""Block-schedule year-grid HTML for the NiceGUI workspace."""

from __future__ import annotations

import hashlib
import html
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlencode

from rbs.models.color_scheme import contrasting_text_color
from rbs.models.instance import SchedulerInput
from rbs.models.resident import Resident
from rbs.models.rotation import ROTATION_COLOR_PALETTE
from rbs.models.schedule import Schedule
from rbs.models.special import SpecialRotationKind


def rotation_color_class(rotation_color: str) -> str:
    """Return the CSS class for a configured palette color.

    The hash fallback keeps older callers and malformed external schedule labels
    deterministic; configured rotations always take the palette branch.
    """
    normalized = rotation_color.strip().upper()
    try:
        index = tuple(ROTATION_COLOR_PALETTE).index(normalized)
    except ValueError:
        digest = hashlib.sha256(rotation_color.encode()).hexdigest()
        index = int(digest[:2], 16) % 24
    return f"rbs-rotation-color-{index}"


def parse_weeks(text: str) -> list[int]:
    if not text.strip():
        return []
    weeks: list[int] = []
    for part in text.replace(";", ",").split(","):
        token = part.strip()
        if not token:
            continue
        weeks.append(int(token))
    return weeks


def format_weeks(weeks: list[int]) -> str:
    return ", ".join(str(week) for week in weeks)


def cell_label(rotation_id: str | None, vacation: bool, locked: bool) -> str:
    if rotation_id:
        text = rotation_id
        if vacation:
            text += " · vac"
    elif vacation:
        text = "vac"
    elif locked:
        text = "lock"
    else:
        text = ""
    return text


def week_groups(n_weeks: int, size: int) -> list[list[int]]:
    size = max(size, 1)
    return [
        list(range(start, min(start + size, n_weeks + 1))) for start in range(1, n_weeks + 1, size)
    ]


def group_label(weeks: list[int]) -> str:
    if len(weeks) == 1:
        return str(weeks[0])
    return f"{weeks[0]}–{weeks[-1]}"


def week_monday(first_week_start: date, week: int) -> date:
    return first_week_start + timedelta(weeks=week - 1)


def visible_week_numbers(
    first_week_start: date,
    n_weeks: int,
    *,
    show_past_weeks: bool,
    today: date | None = None,
) -> list[int]:
    """Weeks to display, retaining the current Monday-through-Sunday week."""
    weeks = list(range(1, n_weeks + 1))
    if show_past_weeks:
        return weeks
    cutoff = today or date.today()
    return [
        week for week in weeks if week_monday(first_week_start, week) + timedelta(days=6) >= cutoff
    ]


def monday_header(first_week_start: date, week: int) -> str:
    """Column label: Monday of this week, e.g. Jun 29."""
    day = week_monday(first_week_start, week)
    return f"{day:%b} {day.day:02d}"


def four_week_block_groups(weeks: list[int]) -> list[tuple[str, list[int]]]:
    """Group visible weeks into academic Blocks A through M."""
    groups: list[tuple[str, list[int]]] = []
    for week in weeks:
        block_index = (week - 1) // 4
        label = chr(ord("A") + block_index)
        if groups and groups[-1][0] == label:
            groups[-1][1].append(week)
        else:
            groups.append((label, [week]))
    return groups


def assignment_runs(
    weeks: list[int],
    resident_grid: dict[str, str],
    resident_elective_grid: dict[str, bool] | None = None,
) -> list[tuple[str | None, bool, list[int]]]:
    runs: list[tuple[str | None, bool, list[int]]] = []
    current: tuple[str | None, bool] | None = None
    bucket: list[int] = []
    started = False
    elective_grid = resident_elective_grid or {}
    for week in weeks:
        rotation_id = resident_grid.get(str(week))
        assignment = (rotation_id, bool(elective_grid.get(str(week), False)))
        if started and (assignment != current or rotation_id is None):
            assert current is not None
            runs.append((current[0], current[1], bucket))
            bucket = []
        started = True
        current = assignment
        bucket.append(week)
    if bucket and current is not None:
        runs.append((current[0], current[1], bucket))
    return runs


@dataclass
class _ScheduleRun:
    rotation_id: str | None
    elective: bool
    weeks: list[int]
    vacation: bool = False
    conference_names: tuple[str, ...] = ()


def _schedule_runs(
    weeks: list[int],
    resident_grid: dict[str, str],
    resident_elective_grid: dict[str, bool],
    vacation_weeks: set[int],
    conference_names_by_week: dict[int, list[str]],
) -> list[_ScheduleRun]:
    """Build uninterrupted visual runs, promoting time off to its own block."""
    runs: list[_ScheduleRun] = []
    current_key: tuple[object, ...] | None = None
    for week in weeks:
        vacation = week in vacation_weeks
        conference_names = tuple(conference_names_by_week.get(week, ()))
        rotation_id = resident_grid.get(str(week))
        elective = bool(resident_elective_grid.get(str(week), False))

        # Vacation and conferences replace the colored assignment at this
        # week-level overview. The underlying assignment remains available in
        # its neighboring run and in the resident's detailed schedule.
        if vacation or conference_names:
            rotation_id = None
            elective = False

        key = (
            rotation_id,
            elective,
            vacation,
            conference_names,
        )
        if runs and key == current_key:
            runs[-1].weeks.append(week)
            continue
        runs.append(
            _ScheduleRun(
                rotation_id=rotation_id,
                elective=elective,
                weeks=[week],
                vacation=vacation,
                conference_names=conference_names,
            )
        )
        current_key = key
    return runs


def block_run_html(
    rotation_id: str,
    weeks: list[int],
    vacation_weeks: set[int],
    locked_weeks: set[int],
    first_week_start: date,
    conference_names_by_week: dict[int, list[str]] | None = None,
    *,
    elective: bool = False,
    label: str | None = None,
    code: str | None = None,
) -> str:
    """Render one uninterrupted colored assignment band."""
    display_label = label or rotation_id + (" (Elec)" if elective else "")
    display_code = code or rotation_id
    escaped_label = html.escape(display_label)
    return (
        '<span class="rbs-block-run">'
        f'<span class="rbs-block-name" aria-label="{html.escape(display_label, quote=True)}">'
        f'<span class="rbs-block-name-full" aria-hidden="true">{escaped_label}</span>'
        f'<span class="rbs-block-name-code" aria-hidden="true">'
        f"{html.escape(display_code)}</span></span>"
        "</span>"
    )


def _resident_row_html(
    instance: SchedulerInput,
    resident: Resident,
    weeks: list[int],
    first_week_start: date,
    grid: dict[str, dict[str, str]],
    resident_edit_url: str | None,
    pgy_cell: str = "",
    *,
    schedule: Schedule | None = None,
) -> str:
    vacation = set(resident.vacation_weeks)
    conference_names_by_week: dict[int, list[str]] = {}
    for special in instance.special_rotations_for_resident(
        resident.id,
        kind=SpecialRotationKind.CONFERENCE,
    ):
        for calendar_day in special.dates():
            week = (calendar_day - first_week_start).days // 7 + 1
            names = conference_names_by_week.setdefault(week, [])
            if special.name not in names:
                names.append(special.name)
    resident_grid = grid.get(resident.id, {})
    elective_grid = schedule.elective_grid.get(resident.id, {}) if schedule is not None else {}
    cells: list[str] = []
    if resident_grid:
        grouped_runs = _schedule_runs(
            weeks,
            resident_grid,
            elective_grid,
            vacation,
            conference_names_by_week,
        )
    else:
        grouped_runs = [
            _ScheduleRun(
                rotation_id=None,
                elective=False,
                weeks=[week],
                vacation=week in vacation,
                conference_names=tuple(conference_names_by_week.get(week, ())),
            )
            for week in weeks
        ]
    for run_index, run in enumerate(grouped_runs):
        rotation_id = run.rotation_id
        elective = run.elective
        group = run.weeks
        is_vac = run.vacation
        is_special = bool(run.conference_names)
        classes = []
        style_attr = ""
        if rotation_id:
            rotation_color = instance.assignment_color(
                rotation_id,
                elective=elective,
            )
            classes.extend(["rbs-block-cell", rotation_color_class(rotation_color)])
            style_attr = (
                f' style="--rbs-rotation-color:{rotation_color};'
                f'--rbs-rotation-foreground:{contrasting_text_color(rotation_color)}"'
            )
            content = block_run_html(
                rotation_id,
                group,
                vacation,
                set(),
                first_week_start,
                conference_names_by_week,
                elective=elective,
                label=instance.assignment_name(rotation_id, elective=elective),
                code=instance.rotation(rotation_id).code,
            )
        else:
            if is_vac:
                classes.extend(["rbs-state-cell", "vac"])
            if is_special:
                if "rbs-state-cell" not in classes:
                    classes.append("rbs-state-cell")
                classes.append("special")
            labels = []
            if is_vac:
                labels.append('<strong class="rbs-vacation-marker">VAC</strong>')
            if is_special:
                labels.append('<strong class="rbs-special-marker">CONF</strong>')
            content = (
                f'<span class="rbs-block-state">{"".join(labels)}</span>'
                if labels
                else html.escape(cell_label(rotation_id, is_vac, False))
            )
        # The larger separator belongs to the new visual run, not to the week
        # column. An uninterrupted run can therefore cross a four-week boundary
        # cleanly, while two different runs still have a clear division.
        if run_index and (group[0] - 1) % 4 == 0:
            classes.append("rbs-four-week-boundary")
        mondays = monday_header(first_week_start, group[0])
        if len(group) > 1:
            mondays += f"–{monday_header(first_week_start, group[-1])}"
        title_bits = [resident.name, mondays]
        if rotation_id:
            title_bits.append(instance.assignment_label(rotation_id, elective=elective))
        if is_vac:
            vac_weeks = [str(week) for week in group if week in vacation]
            title_bits.append("vacation " + ", ".join(vac_weeks))
        if is_special:
            title_bits.append("conference " + ", ".join(run.conference_names))
        title = html.escape(" · ".join(title_bits))
        class_attr = f' class="{" ".join(classes)}"' if classes else ""
        span = f' colspan="{len(group)}"' if len(group) > 1 else ""
        cells.append(f'<td{span}{class_attr}{style_attr} title="{title}">{content}</td>')
    name = html.escape(resident.name)
    if resident_edit_url is not None:
        separator = "&" if "?" in resident_edit_url else "?"
        query = urlencode({"resident": resident.id})
        href = html.escape(f"{resident_edit_url}{separator}{query}", quote=True)
        title = html.escape(f"Edit resident {resident.name}", quote=True)
        name = (
            f'<a class="rbs-resident-link" href="{href}" title="{title}" '
            f'aria-label="{title}">{name}</a>'
        )
    return (
        f'<tr class="rbs-resident-row" data-pgy="{resident.pgy}">'
        f'{pgy_cell}<th class="rbs-resident-name" scope="row">{name}</th>'
        f"{''.join(cells)}</tr>"
    )


def render_grid_html(
    instance: SchedulerInput,
    schedule: Schedule | None,
    *,
    resident_edit_url: str | None = None,
    show_past_weeks: bool = True,
    today: date | None = None,
) -> str:
    start = instance.calendar.first_week_start
    weeks = visible_week_numbers(
        start,
        instance.calendar.weeks,
        show_past_weeks=show_past_weeks,
        today=today,
    )
    grid = schedule.week_grid if schedule is not None else {}
    filled = bool(grid)
    week_head = "".join(
        f'<th class="rbs-block-week-header'
        f'{" rbs-block-start" if (week - 1) % 4 == 0 else ""}" '
        f'scope="col" title="Week {week} · Monday '
        f'{html.escape(monday_header(start, week))}">'
        f'<span class="rbs-block-week-number">Week {week}</span>'
        f'<span class="rbs-block-week-date">'
        f"({html.escape(monday_header(start, week))})</span></th>"
        for week in weeks
    )
    block_head = "".join(
        f'<th class="rbs-block-group" scope="colgroup" colspan="{len(block_weeks)}">'
        f"Block {label}/{block_number}</th>"
        for block_number, (label, block_weeks) in enumerate(
            four_week_block_groups(weeks),
            start=((weeks[0] - 1) // 4 + 1) if weeks else 1,
        )
    )
    residents_by_pgy: dict[int, list[Resident]] = {}
    for resident in instance.residents:
        residents_by_pgy.setdefault(resident.pgy, []).append(resident)

    sections: list[str] = []
    for pgy, residents in sorted(
        residents_by_pgy.items(),
        key=lambda item: instance.training_level_sort_key(item[0]),
    ):
        rows: list[str] = []
        for index, resident in enumerate(residents):
            pgy_cell = ""
            if index == 0:
                pgy_cell = (
                    f'<th class="rbs-pgy-label" scope="rowgroup" rowspan="{len(residents)}">'
                    f"<span>{html.escape(instance.training_level_label(pgy, compact=True))}</span>"
                    "</th>"
                )
            rows.append(
                _resident_row_html(
                    instance,
                    resident,
                    weeks,
                    start,
                    grid,
                    resident_edit_url,
                    pgy_cell,
                    schedule=schedule,
                )
            )
        sections.append(f'<tbody class="rbs-pgy-section">{"".join(rows)}</tbody>')

    grouped = " grouped" if filled else ""
    week_columns = '<col class="rbs-block-week-column">' * len(weeks)
    pgy_spacer = (
        '<tbody class="rbs-pgy-spacer" aria-hidden="true"><tr>'
        f'<td colspan="{len(weeks) + 2}"></td></tr></tbody>'
    )
    schedule_body = pgy_spacer.join(sections)
    table_width = 34 + 180 + 56 * len(weeks)
    return (
        '<div id="rbs-block-grid" class="rbs-grid-wrap" tabindex="0" '
        'aria-label="Block schedule; scroll horizontally to view later weeks">'
        f'<table class="rbs-grid rbs-block-schedule-grid{grouped}" '
        f'style="width:{table_width}px">'
        '<colgroup><col class="rbs-block-pgy-column">'
        f'<col class="rbs-block-resident-column">{week_columns}</colgroup>'
        '<thead><tr class="rbs-block-group-row">'
        '<th class="rbs-pgy-column" aria-label="Training level" rowspan="2"></th>'
        '<th class="rbs-resident-column" rowspan="2">Resident</th>'
        f'{block_head}</tr><tr class="rbs-block-week-row">{week_head}</tr></thead>'
        f"{schedule_body}</table></div>"
    )
