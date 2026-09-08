"""Printable full-program block schedule."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import TABLOID, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from rbs.models.color_scheme import (
    DEFAULT_INK_COLOR,
    DEFAULT_NEUTRAL_COLOR,
    DEFAULT_PRIMARY_COLOR,
    contrasting_text_color,
)
from rbs.models.instance import SchedulerInput
from rbs.models.resident import Resident, resident_display_sort_key
from rbs.models.schedule import Schedule
from rbs.models.special import SpecialRotationKind
from rbs.ui.grid import four_week_block_groups, week_monday
from rbs.ui.pdf_pages import with_export_timestamp
from rbs.ui.print_tokens import (
    PRINT_BODY_LEADING,
    PRINT_BODY_SIZE,
    PRINT_CAPTION_LEADING,
    PRINT_CAPTION_SIZE,
    PRINT_FONT_BOLD,
    PRINT_FONT_REGULAR,
    PRINT_SMALL_LEADING,
    PRINT_SMALL_SIZE,
    PRINT_TITLE_LEADING,
    PRINT_TITLE_SIZE,
)
from rbs.ui.schedule_styles import (
    CONFERENCE_COLOR,
    CONFERENCE_TINT,
    VACATION_COLOR,
    VACATION_TINT,
)
from rbs.ui.visual_tokens import BORDER_STRONG, INFO_SOFT, SURFACE_MUTED, SURFACE_SUBTLE

PRIMARY = colors.HexColor(DEFAULT_PRIMARY_COLOR)
NEUTRAL = colors.HexColor(DEFAULT_NEUTRAL_COLOR)
INK = colors.HexColor(DEFAULT_INK_COLOR)
GRID_GREY = colors.HexColor(BORDER_STRONG)
HEADER_BACKGROUND = colors.HexColor(INFO_SOFT)
BLOCK_BACKGROUND = colors.HexColor(SURFACE_MUTED)
ROW_BACKGROUND = colors.HexColor(SURFACE_SUBTLE)

BLOCKS_PER_PAGE = 4
SCHEDULE_WIDTH = 16.3 * inch
LEVEL_WIDTH = 0.62 * inch
RESIDENT_WIDTH = 1.88 * inch


@dataclass(frozen=True, slots=True)
class _ScheduleCell:
    """One display state which may span adjacent weeks on a printed row."""

    kind: str
    rotation_id: str | None = None
    elective: bool = False
    vacation: bool = False
    conference_names: tuple[str, ...] = ()


def block_schedule_pdf_filename(
    academic_year: str,
    *,
    exported_on: date | None = None,
) -> str:
    """Return a stable, filesystem-friendly full block-schedule filename."""
    year_slug = re.sub(r"[^0-9a-z]+", "-", academic_year.lower()).strip("-")
    export_date = (exported_on or date.today()).isoformat()
    return f"block-schedule-{year_slug}-exported-{export_date}.pdf"


def block_schedule_page_weeks(
    number_of_weeks: int,
    *,
    blocks_per_page: int = BLOCKS_PER_PAGE,
) -> list[list[int]]:
    """Split a year at four-week block boundaries without leaving an orphan page.

    A 52-week year contains thirteen blocks. A strict four-block chunk would put
    Block M alone on a final page, so the block counts are balanced as 4/3/3/3.
    """
    if number_of_weeks < 1:
        return []
    blocks_per_page = max(1, blocks_per_page)
    blocks = four_week_block_groups(list(range(1, number_of_weeks + 1)))
    page_count = (len(blocks) + blocks_per_page - 1) // blocks_per_page
    smaller_page_size, larger_page_count = divmod(len(blocks), page_count)
    page_sizes = [
        smaller_page_size + (1 if index < larger_page_count else 0)
        for index in range(page_count)
    ]

    pages: list[list[int]] = []
    cursor = 0
    for page_size in page_sizes:
        page_blocks = blocks[cursor : cursor + page_size]
        pages.append([week for _label, weeks in page_blocks for week in weeks])
        cursor += page_size
    return pages


def build_block_schedule_pdf(
    instance: SchedulerInput,
    schedule: Schedule | None,
) -> bytes:
    """Build a landscape PDF covering every resident and all 52 academic weeks."""
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=landscape(TABLOID),
        leftMargin=0.35 * inch,
        rightMargin=0.35 * inch,
        topMargin=0.42 * inch,
        bottomMargin=0.38 * inch,
        title=f"Block Schedule - {instance.academic_year}",
        author="RBS",
        subject=f"Full residency block schedule for {instance.academic_year}",
    )
    styles = _styles()
    story: list[object] = []
    for page_index, weeks in enumerate(block_schedule_page_weeks(instance.calendar.weeks)):
        if page_index:
            story.append(PageBreak())
        first_day = week_monday(instance.calendar.first_week_start, weeks[0])
        last_day = week_monday(instance.calendar.first_week_start, weeks[-1]) + timedelta(days=6)
        story.extend(
            [
                Paragraph("Block Schedule", styles["title"]),
                Paragraph(
                    _paragraph_text(
                        f"{instance.academic_year} | {_block_range_label(weeks)} | "
                        f"Weeks {weeks[0]}-{weeks[-1]} | "
                        f"{_date_range_label(first_day, last_day)}"
                    ),
                    styles["meta"],
                ),
                Spacer(1, 0.07 * inch),
                Paragraph(
                    "VAC = Vacation | CONF = Conference | (E) = Elective service | "
                    "Blank = Not scheduled",
                    styles["legend"],
                ),
                Spacer(1, 0.08 * inch),
                _block_schedule_table(instance, schedule, weeks, styles),
            ]
        )

    def footer(canvas, doc) -> None:
        _page_footer(canvas, doc, instance.academic_year)

    draw_page = with_export_timestamp(footer)
    document.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
    return buffer.getvalue()


def _styles() -> dict[str, ParagraphStyle]:
    sample = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "BlockScheduleTitle",
            parent=sample["Heading1"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_TITLE_SIZE,
            leading=PRINT_TITLE_LEADING,
            textColor=INK,
            spaceAfter=1,
        ),
        "meta": ParagraphStyle(
            "BlockScheduleMeta",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_REGULAR,
            fontSize=PRINT_BODY_SIZE,
            leading=PRINT_BODY_LEADING,
            textColor=NEUTRAL,
        ),
        "legend": ParagraphStyle(
            "BlockScheduleLegend",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_REGULAR,
            fontSize=PRINT_CAPTION_SIZE,
            leading=PRINT_CAPTION_LEADING,
            textColor=NEUTRAL,
        ),
        "column_header": ParagraphStyle(
            "BlockScheduleColumnHeader",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_CAPTION_SIZE,
            leading=PRINT_CAPTION_LEADING,
            textColor=INK,
        ),
        "block_header": ParagraphStyle(
            "BlockScheduleBlockHeader",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_SMALL_SIZE,
            leading=PRINT_SMALL_LEADING,
            textColor=INK,
            alignment=TA_CENTER,
        ),
        "week_header": ParagraphStyle(
            "BlockScheduleWeekHeader",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_CAPTION_SIZE,
            leading=PRINT_CAPTION_LEADING,
            textColor=INK,
            alignment=TA_CENTER,
        ),
        "level": ParagraphStyle(
            "BlockScheduleLevel",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_CAPTION_SIZE,
            leading=PRINT_CAPTION_LEADING,
            textColor=NEUTRAL,
            alignment=TA_CENTER,
        ),
        "resident": ParagraphStyle(
            "BlockScheduleResident",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_SMALL_SIZE,
            leading=PRINT_SMALL_LEADING,
            textColor=INK,
        ),
        "cell": ParagraphStyle(
            "BlockScheduleCell",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_REGULAR,
            fontSize=PRINT_CAPTION_SIZE,
            leading=PRINT_CAPTION_LEADING,
            textColor=INK,
            alignment=TA_CENTER,
        ),
        "empty": ParagraphStyle(
            "BlockScheduleEmpty",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_REGULAR,
            fontSize=PRINT_BODY_SIZE,
            leading=PRINT_BODY_LEADING,
            textColor=NEUTRAL,
        ),
    }


def _block_schedule_table(
    instance: SchedulerInput,
    schedule: Schedule | None,
    weeks: Sequence[int],
    styles: Mapping[str, ParagraphStyle],
) -> Table:
    week_list = list(weeks)
    data: list[list[object]] = [
        [
            Paragraph("LEVEL", styles["column_header"]),
            Paragraph("RESIDENT", styles["column_header"]),
            *([""] * len(week_list)),
        ],
        [
            "",
            "",
            *[
                Paragraph(
                    f"Week {week}<br/><font color='{DEFAULT_NEUTRAL_COLOR}'>"
                    f"{week_monday(instance.calendar.first_week_start, week):%b} "
                    f"{week_monday(instance.calendar.first_week_start, week).day}</font>",
                    styles["week_header"],
                )
                for week in week_list
            ],
        ],
    ]
    commands: list[tuple] = [
        ("BACKGROUND", (0, 0), (-1, 0), BLOCK_BACKGROUND),
        ("BACKGROUND", (0, 1), (-1, 1), HEADER_BACKGROUND),
        ("GRID", (0, 0), (-1, -1), 0.35, GRID_GREY),
        ("SPAN", (0, 0), (0, 1)),
        ("SPAN", (1, 0), (1, 1)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (1, 0), (1, -1), 6),
        ("RIGHTPADDING", (1, 0), (1, -1), 6),
    ]

    week_position = {week: index for index, week in enumerate(week_list)}
    for block_label, block_weeks in four_week_block_groups(week_list):
        start_column = 2 + week_position[block_weeks[0]]
        end_column = 2 + week_position[block_weeks[-1]]
        block_number = (block_weeks[0] - 1) // 4 + 1
        data[0][start_column] = Paragraph(
            f"Block {block_label}/{block_number}",
            styles["block_header"],
        )
        commands.append(("SPAN", (start_column, 0), (end_column, 0)))
        if start_column > 2:
            commands.append(("LINEBEFORE", (start_column, 0), (start_column, -1), 1, PRIMARY))

    residents = _ordered_residents(instance)
    if not residents:
        row_index = len(data)
        data.append(
            [
                Paragraph("No residents are configured.", styles["empty"]),
                *([""] * (len(week_list) + 1)),
            ]
        )
        commands.append(("SPAN", (0, row_index), (-1, row_index)))
    else:
        grid = schedule.week_grid if schedule is not None else {}
        elective_grid = schedule.elective_grid if schedule is not None else {}
        previous_pgy: int | None = None
        for resident_index, resident in enumerate(residents):
            row_index = len(data)
            data.append(
                [
                    Paragraph(
                        _paragraph_text(instance.training_level_label(resident.pgy, compact=True)),
                        styles["level"],
                    ),
                    Paragraph(_paragraph_text(resident.name), styles["resident"]),
                    *([""] * len(week_list)),
                ]
            )
            if resident_index % 2:
                commands.append(("BACKGROUND", (0, row_index), (-1, row_index), ROW_BACKGROUND))
            if resident.pgy != previous_pgy:
                commands.append(("LINEABOVE", (0, row_index), (-1, row_index), 1.2, PRIMARY))
            previous_pgy = resident.pgy

            conferences = _conference_names_by_week(instance, resident.id)
            segments = _resident_segments(
                resident,
                week_list,
                grid.get(resident.id, {}),
                elective_grid.get(resident.id, {}),
                conferences,
            )
            for cell, segment_weeks in segments:
                start_column = 2 + week_position[segment_weeks[0]]
                end_column = 2 + week_position[segment_weeks[-1]]
                label, background, foreground, accent = _cell_appearance(
                    instance,
                    cell,
                    len(segment_weeks),
                )
                if label:
                    data[row_index][start_column] = Paragraph(
                        f"<font color='{foreground}'>{label}</font>",
                        styles["cell"],
                    )
                if end_column > start_column:
                    commands.append(
                        ("SPAN", (start_column, row_index), (end_column, row_index))
                    )
                if background is not None:
                    commands.append(
                        (
                            "BACKGROUND",
                            (start_column, row_index),
                            (end_column, row_index),
                            colors.HexColor(background),
                        )
                    )
                if accent is not None:
                    commands.extend(
                        [
                            (
                                "LINEBEFORE",
                                (start_column, row_index),
                                (start_column, row_index),
                                1.1,
                                colors.HexColor(accent),
                            ),
                            (
                                "LINEAFTER",
                                (end_column, row_index),
                                (end_column, row_index),
                                1.1,
                                colors.HexColor(accent),
                            ),
                        ]
                    )

    week_width = (SCHEDULE_WIDTH - LEVEL_WIDTH - RESIDENT_WIDTH) / len(week_list)
    table = Table(
        data,
        colWidths=[LEVEL_WIDTH, RESIDENT_WIDTH, *([week_width] * len(week_list))],
        repeatRows=2,
        hAlign="LEFT",
        splitByRow=1,
    )
    table.setStyle(TableStyle(commands))
    return table


def _ordered_residents(instance: SchedulerInput) -> list[Resident]:
    return sorted(
        instance.residents,
        key=lambda resident: (
            instance.training_level_sort_key(resident.pgy),
            resident_display_sort_key(resident),
        ),
    )


def _conference_names_by_week(
    instance: SchedulerInput,
    resident_id: str,
) -> dict[int, tuple[str, ...]]:
    names_by_week: dict[int, list[str]] = {}
    first_day = instance.calendar.first_week_start
    for special in instance.special_rotations_for_resident(
        resident_id,
        kind=SpecialRotationKind.CONFERENCE,
    ):
        for calendar_day in special.dates():
            week = (calendar_day - first_day).days // 7 + 1
            if not 1 <= week <= instance.calendar.weeks:
                continue
            names = names_by_week.setdefault(week, [])
            if special.name not in names:
                names.append(special.name)
    return {week: tuple(names) for week, names in names_by_week.items()}


def _resident_segments(
    resident: Resident,
    weeks: Sequence[int],
    resident_grid: Mapping[str, str],
    resident_elective_grid: Mapping[str, bool],
    conference_names_by_week: Mapping[int, tuple[str, ...]],
) -> list[tuple[_ScheduleCell, list[int]]]:
    vacation_weeks = set(resident.vacation_weeks)
    segments: list[tuple[_ScheduleCell, list[int]]] = []
    for week in weeks:
        vacation = week in vacation_weeks
        conference_names = conference_names_by_week.get(week, ())
        rotation_id = resident_grid.get(str(week))
        if vacation or conference_names:
            cell = _ScheduleCell(
                kind="state",
                vacation=vacation,
                conference_names=conference_names,
            )
        elif rotation_id is not None:
            cell = _ScheduleCell(
                kind="rotation",
                rotation_id=rotation_id,
                elective=bool(resident_elective_grid.get(str(week), False)),
            )
        else:
            # Preserve each open week as a visible cell instead of spanning a
            # featureless empty range across the schedule.
            cell = _ScheduleCell(kind=f"empty-{week}")
        if segments and segments[-1][0] == cell:
            segments[-1][1].append(week)
        else:
            segments.append((cell, [week]))
    return segments


def _cell_appearance(
    instance: SchedulerInput,
    cell: _ScheduleCell,
    duration_weeks: int,
) -> tuple[str, str | None, str, str | None]:
    if cell.kind == "rotation":
        assert cell.rotation_id is not None
        rotation = instance.rotation(cell.rotation_id)
        color = instance.assignment_color(cell.rotation_id, elective=cell.elective)
        code = _paragraph_text(rotation.code + (" (E)" if cell.elective else ""))
        label = f"<b>{code}</b>"
        if duration_weeks > 1:
            label += "<br/>" + _paragraph_text(
                instance.assignment_name(cell.rotation_id, elective=cell.elective)
            )
        return label, color, contrasting_text_color(color), color
    if cell.kind == "state":
        if cell.vacation and cell.conference_names:
            return "<b>VAC / CONF</b>", CONFERENCE_TINT, DEFAULT_INK_COLOR, CONFERENCE_COLOR
        if cell.vacation:
            return "<b>VAC</b>", VACATION_TINT, DEFAULT_INK_COLOR, VACATION_COLOR
        return "<b>CONF</b>", CONFERENCE_TINT, DEFAULT_INK_COLOR, CONFERENCE_COLOR
    return "", None, DEFAULT_INK_COLOR, None


def _block_range_label(weeks: Sequence[int]) -> str:
    block_groups = four_week_block_groups(list(weeks))
    first = block_groups[0][0]
    last = block_groups[-1][0]
    return f"Block {first}" if first == last else f"Blocks {first}-{last}"


def _date_range_label(start: date, end: date) -> str:
    if start.year == end.year:
        return f"{start:%b} {start.day}-{end:%b} {end.day}, {end.year}"
    return f"{start:%b} {start.day}, {start.year}-{end:%b} {end.day}, {end.year}"


def _paragraph_text(value: object) -> str:
    text = str(value or "-")
    for dash in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2212"):
        text = text.replace(dash, "-")
    text = text.replace("\u00b7", "-")
    return escape(text).replace("\n", "<br/>")


def _page_footer(canvas, document, academic_year: str) -> None:
    canvas.saveState()
    page_width, _page_height = landscape(TABLOID)
    canvas.setStrokeColor(GRID_GREY)
    canvas.setLineWidth(0.5)
    canvas.line(0.35 * inch, 0.25 * inch, page_width - 0.35 * inch, 0.25 * inch)
    canvas.setFont(PRINT_FONT_REGULAR, PRINT_CAPTION_SIZE)
    canvas.setFillColor(NEUTRAL)
    canvas.drawString(0.35 * inch, 0.13 * inch, f"RBS block schedule | {academic_year}")
    canvas.drawRightString(page_width - 0.35 * inch, 0.13 * inch, f"Page {document.page}")
    canvas.restoreState()


__all__ = [
    "BLOCKS_PER_PAGE",
    "block_schedule_page_weeks",
    "block_schedule_pdf_filename",
    "build_block_schedule_pdf",
]
