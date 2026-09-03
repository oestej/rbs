"""Calendar-style PDF export for the overall program clinic schedule."""

from __future__ import annotations

import re
from datetime import date, timedelta
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import TABLOID, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from rbs.models.clinic import ClinicPolicy
from rbs.models.color_scheme import (
    DEFAULT_INK_COLOR,
    DEFAULT_NEUTRAL_COLOR,
    DEFAULT_PRIMARY_COLOR,
)
from rbs.models.enums import Session, Weekday
from rbs.models.instance import SchedulerInput
from rbs.models.schedule import Schedule
from rbs.models.special import SpecialRotation
from rbs.ui.clinic.board import (
    ACADEMIC_LABEL,
    ClinicOccupant,
    calendar_occupants,
    clinic_closure_view,
    clinic_weekdays,
    is_academic_week,
    occupancy,
    occupant_site,
    occupants_for_site,
    site_headcount,
    special_events_for_slot,
)
from rbs.ui.grid import visible_week_numbers, week_monday
from rbs.ui.print_tokens import (
    PRINT_BODY_LEADING,
    PRINT_BODY_LOOSE_LEADING,
    PRINT_BODY_SIZE,
    PRINT_CAPTION_LEADING,
    PRINT_CAPTION_SIZE,
    PRINT_FONT_BOLD,
    PRINT_FONT_REGULAR,
    PRINT_MICRO_LEADING,
    PRINT_MICRO_SIZE,
    PRINT_SMALL_LEADING,
    PRINT_SMALL_SIZE,
    PRINT_TITLE_LEADING,
    PRINT_TITLE_SIZE,
)
from rbs.ui.schedule_styles import (
    ACADEMIC_TINT,
    ADMIN_COLOR,
    SPECIAL_EVENT_COLOR,
    SPECIAL_EVENT_TINT,
)
from rbs.ui.visual_tokens import (
    BORDER_STRONG,
    CLOSURE_TINT,
    PARTIAL_CLOSURE_TINT,
    SURFACE_MUTED,
    SURFACE_SUBTLE,
)

PRIMARY = colors.HexColor(DEFAULT_PRIMARY_COLOR)
NEUTRAL = colors.HexColor(DEFAULT_NEUTRAL_COLOR)
INK = colors.HexColor(DEFAULT_INK_COLOR)
LIGHT_GREY = colors.HexColor(SURFACE_MUTED)
ROW_GREY = colors.HexColor(SURFACE_SUBTLE)
GRID_GREY = colors.HexColor(BORDER_STRONG)
ACADEMIC_GREY = colors.HexColor(ACADEMIC_TINT)
CLOSURE_GREY = colors.HexColor(CLOSURE_TINT)
PARTIAL_CLOSURE_GREY = colors.HexColor(PARTIAL_CLOSURE_TINT)
SPECIAL_EVENT_BACKGROUND = colors.HexColor(SPECIAL_EVENT_TINT)

WEEKS_PER_PAGE = 3
CALENDAR_WIDTH = 16.3 * inch
DAY_CARD_HEIGHT = 3.0 * inch
DAY_LABEL_WIDTH = 0.58 * inch
SESSION_CARD_HEIGHT = (DAY_CARD_HEIGHT - 0.3 * inch) / 2


def build_clinic_schedule_pdf(
    instance: SchedulerInput,
    schedule: Schedule | None,
    *,
    show_past_weeks: bool = True,
    today: date | None = None,
    site: str | None = None,
) -> bytes:
    """Build a dated calendar with configured weekday AM/PM sections."""
    board = occupancy(instance, schedule)
    weeks = visible_week_numbers(
        instance.calendar.first_week_start,
        instance.calendar.weeks,
        show_past_weeks=show_past_weeks,
        today=today,
    )
    site_name = instance.clinic_policy.site_name(site) if site is not None else "All Sites"
    range_name = "All dates" if show_past_weeks else "Current and future dates"
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=landscape(TABLOID),
        leftMargin=0.35 * inch,
        rightMargin=0.35 * inch,
        topMargin=0.34 * inch,
        bottomMargin=0.38 * inch,
        title=f"Clinic Schedule - {site_name}",
        author="RBS",
        subject=f"Calendar-style clinic schedule for {instance.academic_year}",
    )
    styles = _styles()
    story = [
        Paragraph("Clinic Schedule", styles["title"]),
        Paragraph(
            _paragraph_text(f"{instance.academic_year} | {site_name} | {range_name}"),
            styles["meta"],
        ),
        Spacer(1, 0.08 * inch),
    ]
    if not weeks:
        story.append(
            Paragraph(
                "No clinic schedule dates are available for the selected range.",
                styles["empty"],
            )
        )
    else:
        week_groups = _chunks(weeks, WEEKS_PER_PAGE)
        for group_index, group in enumerate(week_groups):
            if group_index:
                story.append(PageBreak())
            story.extend(
                [
                    _calendar_page_header(instance, group, site, styles),
                    Spacer(1, 0.08 * inch),
                ]
            )
            for index, week in enumerate(group):
                story.append(_week_calendar(instance, board, week, site, styles))
                if index < len(group) - 1:
                    story.append(Spacer(1, 0.08 * inch))

    def footer(canvas, doc) -> None:
        _page_footer(canvas, doc, site_name)

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def clinic_schedule_pdf_filename(
    academic_year: str,
    *,
    site: str | None,
    exported_on: date | None = None,
) -> str:
    year_slug = re.sub(r"[^0-9a-z]+", "-", academic_year.lower()).strip("-")
    site_slug = site.replace("_", "-") if site is not None else "all-sites"
    export_date = (exported_on or date.today()).isoformat()
    return f"clinic-schedule-{year_slug}-{site_slug}-exported-{export_date}.pdf"


def _calendar_page_header(
    instance: SchedulerInput,
    weeks: list[int],
    site: str | None,
    styles: dict[str, ParagraphStyle],
) -> Table:
    first = week_monday(instance.calendar.first_week_start, weeks[0])
    weekdays = clinic_weekdays(instance)
    last = week_monday(instance.calendar.first_week_start, weeks[-1]) + timedelta(
        days=list(Weekday).index(weekdays[-1])
    )
    header = Table(
        [
            [
                Paragraph(_paragraph_text(_date_range_label(first, last)), styles["range"]),
                Paragraph(_legend_markup(instance.clinic_policy, site), styles["legend"]),
            ]
        ],
        colWidths=[4.0 * inch, 12.3 * inch],
        rowHeights=[0.28 * inch],
        hAlign="LEFT",
    )
    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LINEBELOW", (0, 0), (-1, -1), 0.7, PRIMARY),
            ]
        )
    )
    return header


def _week_calendar(
    instance: SchedulerInput,
    board: dict[tuple[int, Weekday, Session], list[ClinicOccupant]],
    week: int,
    site: str | None,
    styles: dict[str, ParagraphStyle],
) -> Table:
    monday = week_monday(instance.calendar.first_week_start, week)
    weekdays = clinic_weekdays(instance)
    column_width = CALENDAR_WIDTH / len(weekdays)
    card_width = column_width - 0.1 * inch
    cards = [
        _day_card(
            instance,
            board,
            week,
            weekday,
            monday + timedelta(days=list(Weekday).index(weekday)),
            site,
            styles,
            card_width,
        )
        for weekday in weekdays
    ]
    week_table = Table(
        [cards],
        colWidths=[column_width] * len(weekdays),
        rowHeights=[DAY_CARD_HEIGHT],
        hAlign="LEFT",
    )
    week_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return week_table


def _day_card(
    instance: SchedulerInput,
    board: dict[tuple[int, Weekday, Session], list[ClinicOccupant]],
    week: int,
    weekday: Weekday,
    calendar_day: date,
    site: str | None,
    styles: dict[str, ParagraphStyle],
    card_width: float,
) -> Table:
    policy = instance.clinic_policy
    closure = clinic_closure_view(policy, calendar_day, site)
    session_cells = []
    academic_rows = []
    closure_rows = []
    event_rows = []
    for row_index, session in enumerate(Session, start=1):
        special_events = special_events_for_slot(instance, calendar_day, session)
        event_labels = [_special_event_label(instance, event) for event in special_events]
        if special_events:
            event_rows.append(row_index)
        if closure.all_selected_sites_closed:
            session_cells.append(
                Paragraph(
                    _paragraph_text("\n".join([closure.label(), *event_labels])),
                    styles["closure"],
                )
            )
            closure_rows.append(row_index)
            continue
        if is_academic_week(instance, week, weekday, session):
            academic_label = "\n".join(
                [
                    *([closure.label()] if closure.is_partial else []),
                    ACADEMIC_LABEL,
                    *event_labels,
                ]
            )
            session_cells.append(Paragraph(_paragraph_text(academic_label), styles["academic"]))
            academic_rows.append(row_index)
            continue
        people = calendar_occupants(
            occupants_for_site(board[(week, weekday, session)], site),
            policy,
        )
        session_cells.append(
            _session_cell(
                people,
                policy,
                site,
                styles,
                card_width,
                closure_label=closure.label() if closure.is_partial else "",
                special_events=special_events,
                instance=instance,
            )
        )
    card = Table(
        [
            [
                Paragraph(weekday.value.upper(), styles["day"]),
                Paragraph(_date_heading(calendar_day), styles["date"]),
            ],
            [Paragraph("AM", styles["session"]), session_cells[0]],
            [Paragraph("PM", styles["session"]), session_cells[1]],
        ],
        colWidths=[DAY_LABEL_WIDTH, card_width - DAY_LABEL_WIDTH],
        rowHeights=[0.3 * inch, SESSION_CARD_HEIGHT, SESSION_CARD_HEIGHT],
        hAlign="LEFT",
    )
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), LIGHT_GREY),
        ("BACKGROUND", (0, 1), (0, -1), ROW_GREY),
        ("BOX", (0, 0), (-1, -1), 0.65, GRID_GREY),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, GRID_GREY),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, PRIMARY),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    commands.extend(
        ("BACKGROUND", (1, row_index), (1, row_index), SPECIAL_EVENT_BACKGROUND)
        for row_index in event_rows
    )
    commands.extend(
        ("BACKGROUND", (1, row_index), (1, row_index), ACADEMIC_GREY) for row_index in academic_rows
    )
    commands.extend(
        ("BACKGROUND", (0, row_index), (-1, row_index), CLOSURE_GREY) for row_index in closure_rows
    )
    if closure.all_selected_sites_closed:
        commands.append(("BACKGROUND", (0, 0), (-1, 0), CLOSURE_GREY))
    elif closure.is_partial:
        commands.append(("BACKGROUND", (0, 0), (-1, 0), PARTIAL_CLOSURE_GREY))
    card.setStyle(TableStyle(commands))
    return card


def _session_cell(
    people: list[ClinicOccupant],
    policy: ClinicPolicy,
    selected_site: str | None,
    styles: dict[str, ParagraphStyle],
    card_width: float,
    *,
    closure_label: str = "",
    special_events: tuple[SpecialRotation, ...] = (),
    instance: SchedulerInput | None = None,
) -> Table:
    attending_parts = []
    visible_sites = (selected_site,) if selected_site is not None else policy.site_ids
    for clinic_site in visible_sites:
        count = site_headcount(people, clinic_site)
        needed = policy.attendings_needed(count, clinic_site)
        if needed:
            config = policy.site(clinic_site)
            attending_parts.append(
                f'<font color="{config.color}"><b>{needed} {escape(config.name)}</b></font>'
            )
    resident_lines = []
    for person in people:
        if person.admin:
            clinic_name = "Admin"
            color = ADMIN_COLOR
        else:
            clinic_site = occupant_site(person) or policy.primary_site_id
            site_config = policy.site(clinic_site)
            clinic_name = person.site_name or site_config.name
            color = person.site_color or site_config.color
        resident_lines.append(
            f'<font color="{color}"><b>{escape(clinic_name)}:</b></font> '
            f"{escape(person.display_label())}"
        )
    rows = []
    if closure_label:
        rows.append([Paragraph(_paragraph_text(closure_label), styles["partial_closure"])])
    if special_events and instance is not None:
        event_markup = "<br/>".join(
            f'<font color="{SPECIAL_EVENT_COLOR}"><b>EVENT:</b></font> '
            + escape(_special_event_label(instance, event))
            for event in special_events
        )
        rows.append([Paragraph(event_markup, styles["special_event"])])
    if attending_parts:
        rows.append([Paragraph(" &#8226; ".join(attending_parts), styles["attending"])])
    resident_markup = "<br/>".join(resident_lines) if resident_lines else "-"
    rows.append([Paragraph(resident_markup, styles["cell"])])
    content = Table(
        rows,
        colWidths=[card_width - DAY_LABEL_WIDTH - 0.13 * inch],
    )
    content.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]
        )
    )
    return content


def _legend_markup(policy: ClinicPolicy, site: str | None) -> str:
    sites = (site,) if site is not None else policy.site_ids
    parts = ["<b>KEY</b>"]
    for item in sites:
        config = policy.site(item)
        parts.append(f'<font color="{config.color}"><b>{escape(config.name)}</b></font>')
    if site is None:
        parts.append(f'<font color="{ADMIN_COLOR}"><b>Admin</b></font>')
    parts.append(f'<font color="{SPECIAL_EVENT_COLOR}"><b>EVENT</b></font> Special event')
    return "&nbsp;&nbsp; | &nbsp;&nbsp;".join(parts)


def _special_event_label(
    instance: SchedulerInput,
    event: SpecialRotation,
) -> str:
    residents = [instance.residents_by_id[resident_id] for resident_id in event.resident_ids]
    return (
        event.name
        + ": "
        + ", ".join(
            f"{instance.training_level_label(resident.pgy, compact=True)} {resident.name}"
            for resident in residents
        )
    )


def _styles() -> dict[str, ParagraphStyle]:
    sample = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ClinicCalendarTitle",
            parent=sample["Heading1"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_TITLE_SIZE,
            leading=PRINT_TITLE_LEADING,
            textColor=PRIMARY,
            spaceAfter=2,
        ),
        "meta": ParagraphStyle(
            "ClinicCalendarMeta",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_REGULAR,
            fontSize=PRINT_BODY_SIZE,
            leading=PRINT_BODY_LEADING,
            textColor=NEUTRAL,
        ),
        "range": ParagraphStyle(
            "ClinicCalendarRange",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_SMALL_SIZE,
            leading=PRINT_SMALL_LEADING,
            textColor=INK,
            alignment=TA_LEFT,
        ),
        "legend": ParagraphStyle(
            "ClinicCalendarLegend",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_REGULAR,
            fontSize=PRINT_CAPTION_SIZE,
            leading=PRINT_CAPTION_LEADING,
            textColor=NEUTRAL,
            alignment=TA_RIGHT,
        ),
        "day": ParagraphStyle(
            "ClinicCalendarDay",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_CAPTION_SIZE,
            leading=PRINT_CAPTION_LEADING,
            textColor=NEUTRAL,
            alignment=TA_LEFT,
        ),
        "date": ParagraphStyle(
            "ClinicCalendarDate",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_SMALL_SIZE,
            leading=PRINT_SMALL_LEADING,
            textColor=PRIMARY,
            alignment=TA_RIGHT,
        ),
        "session": ParagraphStyle(
            "ClinicCalendarSession",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_MICRO_SIZE,
            leading=PRINT_MICRO_LEADING,
            textColor=NEUTRAL,
        ),
        "cell": ParagraphStyle(
            "ClinicCalendarCell",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_REGULAR,
            fontSize=PRINT_MICRO_SIZE,
            leading=PRINT_MICRO_LEADING,
            textColor=INK,
        ),
        "attending": ParagraphStyle(
            "ClinicCalendarAttending",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_CAPTION_SIZE,
            leading=PRINT_CAPTION_LEADING,
            textColor=INK,
            alignment=TA_RIGHT,
        ),
        "academic": ParagraphStyle(
            "ClinicCalendarAcademic",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_MICRO_SIZE,
            leading=PRINT_MICRO_LEADING,
            textColor=INK,
        ),
        "closure": ParagraphStyle(
            "ClinicCalendarClosure",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_MICRO_SIZE,
            leading=PRINT_MICRO_LEADING,
            textColor=NEUTRAL,
            alignment=TA_LEFT,
        ),
        "partial_closure": ParagraphStyle(
            "ClinicCalendarPartialClosure",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_MICRO_SIZE,
            leading=PRINT_MICRO_LEADING,
            textColor=NEUTRAL,
            alignment=TA_LEFT,
            spaceAfter=1,
        ),
        "special_event": ParagraphStyle(
            "ClinicCalendarSpecialEvent",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_REGULAR,
            fontSize=PRINT_MICRO_SIZE,
            leading=PRINT_MICRO_LEADING,
            textColor=INK,
            alignment=TA_LEFT,
            spaceAfter=1,
        ),
        "empty": ParagraphStyle(
            "ClinicCalendarEmpty",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_REGULAR,
            fontSize=PRINT_BODY_SIZE,
            leading=PRINT_BODY_LOOSE_LEADING,
            textColor=NEUTRAL,
        ),
    }


def _chunks(values: list[int], size: int) -> list[list[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _date_heading(value: date) -> str:
    return f"{value:%b} {value.day}".upper()


def _date_range_label(start: date, end: date) -> str:
    if start.year == end.year:
        return f"{start:%b} {start.day} - {end:%b} {end.day}, {end.year}"
    return f"{start:%b} {start.day}, {start.year} - {end:%b} {end.day}, {end.year}"


def _paragraph_text(value: object) -> str:
    text = str(value or "-")
    for dash in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2212"):
        text = text.replace(dash, "-")
    text = text.replace("\u00b7", "-")
    return escape(text).replace("\n", "<br/>")


def _page_footer(canvas, document, site_name: str) -> None:
    canvas.saveState()
    page_width, _page_height = landscape(TABLOID)
    canvas.setStrokeColor(GRID_GREY)
    canvas.setLineWidth(0.5)
    canvas.line(0.35 * inch, 0.25 * inch, page_width - 0.35 * inch, 0.25 * inch)
    canvas.setFont(PRINT_FONT_REGULAR, PRINT_CAPTION_SIZE)
    canvas.setFillColor(NEUTRAL)
    canvas.drawString(0.35 * inch, 0.13 * inch, f"RBS clinic calendar | {site_name}")
    canvas.drawRightString(page_width - 0.35 * inch, 0.13 * inch, f"Page {document.page}")
    canvas.restoreState()
