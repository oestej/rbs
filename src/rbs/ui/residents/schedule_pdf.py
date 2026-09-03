"""Printable resident schedule report."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.graphics.shapes import Circle, Drawing, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from rbs.models.clinic import lighten_hex_color
from rbs.models.color_scheme import (
    DEFAULT_INK_COLOR,
    DEFAULT_NEUTRAL_COLOR,
    DEFAULT_PRIMARY_COLOR,
)
from rbs.models.curriculum import default_training_level_name
from rbs.models.resident import Resident
from rbs.ui.print_tokens import (
    PRINT_AVATAR_SIZE,
    PRINT_BODY_LEADING,
    PRINT_BODY_LOOSE_LEADING,
    PRINT_BODY_SIZE,
    PRINT_CAPTION_LEADING,
    PRINT_CAPTION_SIZE,
    PRINT_FONT_BOLD,
    PRINT_FONT_REGULAR,
    PRINT_SECTION_LEADING,
    PRINT_SECTION_SIZE,
    PRINT_SMALL_LEADING,
    PRINT_SMALL_SIZE,
    PRINT_TITLE_LEADING,
    PRINT_TITLE_SIZE,
)
from rbs.ui.schedule_styles import (
    ADMIN_COLOR,
    ADMIN_TINT,
    CONFERENCE_COLOR,
    CONFERENCE_TINT,
    VACATION_COLOR,
    VACATION_TINT,
)
from rbs.ui.visual_tokens import BORDER_STRONG, SURFACE_MUTED, SURFACE_SUBTLE, TEXT_DISABLED

PRIMARY = colors.HexColor(DEFAULT_PRIMARY_COLOR)
NEUTRAL = colors.HexColor(DEFAULT_NEUTRAL_COLOR)
INK = colors.HexColor(DEFAULT_INK_COLOR)
LIGHT_GREY = colors.HexColor(SURFACE_MUTED)
ROW_GREY = colors.HexColor(SURFACE_SUBTLE)
GRID_GREY = colors.HexColor(BORDER_STRONG)
AVATAR_BACKGROUND = colors.HexColor(ADMIN_TINT)

BlockRow = Mapping[str, str]
ClinicRow = Mapping[str, str]


def resident_schedule_pdf_filename(
    resident: Resident,
    academic_year: str,
    *,
    exported_on: date | None = None,
) -> str:
    """Return a stable, filesystem-friendly resident schedule filename."""
    resident_slug = re.sub(r"[^a-z0-9]+", "-", resident.name.lower()).strip("-")
    year_slug = re.sub(r"[^0-9]+", "-", academic_year).strip("-")
    export_date = (exported_on or date.today()).isoformat()
    return f"{resident_slug or resident.id}-schedule-{year_slug}-exported-{export_date}.pdf"


def build_resident_schedule_pdf(
    *,
    resident: Resident,
    academic_year: str,
    block_rows: Sequence[BlockRow],
    clinic_rows: Sequence[ClinicRow],
    training_level_label: str | None = None,
) -> bytes:
    """Build a landscape PDF containing both resident schedule reports."""
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=landscape(letter),
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.45 * inch,
        bottomMargin=0.45 * inch,
        title=f"{resident.name} Schedule",
        author="RBS",
        subject=f"Resident block and clinic schedule for {academic_year}",
    )
    styles = _styles()
    story = [
        _identity_header(
            resident,
            academic_year,
            styles,
            training_level_label=training_level_label,
        ),
        Spacer(1, 0.22 * inch),
        Paragraph("Block Schedule", styles["section"]),
        Spacer(1, 0.08 * inch),
    ]
    if block_rows:
        story.append(_block_schedule_table(block_rows, styles))
    else:
        story.append(
            _empty_report(
                "No block schedule available",
                "Run Solve to generate this resident's schedule.",
                styles,
            )
        )

    story.extend(
        [
            PageBreak(),
            _identity_header(
                resident,
                academic_year,
                styles,
                training_level_label=training_level_label,
            ),
            Spacer(1, 0.22 * inch),
            Paragraph("Clinic Schedule", styles["section"]),
            Spacer(1, 0.08 * inch),
        ]
    )
    if clinic_rows:
        story.extend(
            [
                *_clinic_schedule_calendars(clinic_rows, styles),
                Paragraph(
                    "Every week in the selected range is listed. Weeks without assigned "
                    "clinic dates remain blank; vacation weeks and individual days off "
                    "are also blank.",
                    styles["note"],
                ),
            ]
        )
    else:
        story.append(
            _empty_report(
                "No clinic schedule available",
                "This resident has no assigned clinic sessions.",
                styles,
            )
        )

    document.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)
    return buffer.getvalue()


def _styles() -> dict[str, ParagraphStyle]:
    sample = getSampleStyleSheet()
    return {
        "name": ParagraphStyle(
            "ResidentName",
            parent=sample["Heading1"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_TITLE_SIZE,
            leading=PRINT_TITLE_LEADING,
            textColor=INK,
            spaceAfter=2,
        ),
        "meta": ParagraphStyle(
            "ResidentMeta",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_REGULAR,
            fontSize=PRINT_BODY_SIZE,
            leading=PRINT_BODY_LEADING,
            textColor=NEUTRAL,
        ),
        "section": ParagraphStyle(
            "ScheduleSection",
            parent=sample["Heading2"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_SECTION_SIZE,
            leading=PRINT_SECTION_LEADING,
            textColor=PRIMARY,
            spaceAfter=0,
        ),
        "table_header": ParagraphStyle(
            "TableHeader",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_CAPTION_SIZE,
            leading=PRINT_CAPTION_LEADING,
            textColor=NEUTRAL,
            alignment=TA_LEFT,
        ),
        "table_cell": ParagraphStyle(
            "TableCell",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_REGULAR,
            fontSize=PRINT_SMALL_SIZE,
            leading=PRINT_SMALL_LEADING,
            textColor=INK,
            alignment=TA_LEFT,
        ),
        "table_cell_bold": ParagraphStyle(
            "TableCellBold",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_SMALL_SIZE,
            leading=PRINT_SMALL_LEADING,
            textColor=INK,
            alignment=TA_LEFT,
        ),
        "clinic_week": ParagraphStyle(
            "ClinicWeek",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_BODY_SIZE,
            leading=PRINT_BODY_LEADING,
            textColor=PRIMARY,
        ),
        "clinic_rotation": ParagraphStyle(
            "ClinicRotation",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_SMALL_SIZE,
            leading=PRINT_SMALL_LEADING,
            textColor=INK,
            alignment=TA_RIGHT,
        ),
        "clinic_day": ParagraphStyle(
            "ClinicDay",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_CAPTION_SIZE,
            leading=PRINT_CAPTION_LEADING,
            textColor=NEUTRAL,
            alignment=TA_LEFT,
        ),
        "clinic_session": ParagraphStyle(
            "ClinicSession",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_CAPTION_SIZE,
            leading=PRINT_CAPTION_LEADING,
            textColor=NEUTRAL,
            alignment=TA_LEFT,
        ),
        "clinic_event": ParagraphStyle(
            "ClinicEvent",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_CAPTION_SIZE,
            leading=PRINT_CAPTION_LEADING,
            textColor=INK,
            alignment=TA_LEFT,
        ),
        "clinic_empty": ParagraphStyle(
            "ClinicEmpty",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_REGULAR,
            fontSize=PRINT_CAPTION_SIZE,
            leading=PRINT_CAPTION_LEADING,
            textColor=colors.HexColor(TEXT_DISABLED),
            alignment=TA_LEFT,
        ),
        "note": ParagraphStyle(
            "ScheduleNote",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_REGULAR,
            fontSize=PRINT_CAPTION_SIZE,
            leading=PRINT_CAPTION_LEADING,
            textColor=NEUTRAL,
        ),
        "empty_title": ParagraphStyle(
            "EmptyTitle",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_BODY_SIZE,
            leading=PRINT_BODY_LOOSE_LEADING,
            textColor=INK,
        ),
        "empty_detail": ParagraphStyle(
            "EmptyDetail",
            parent=sample["BodyText"],
            fontName=PRINT_FONT_REGULAR,
            fontSize=PRINT_SMALL_SIZE,
            leading=PRINT_SMALL_LEADING,
            textColor=NEUTRAL,
        ),
    }


def _identity_header(
    resident: Resident,
    academic_year: str,
    styles: Mapping[str, ParagraphStyle],
    *,
    training_level_label: str | None = None,
) -> Table:
    initials = "".join(part[0] for part in resident.name.split() if part)[:2].upper() or "?"
    avatar = Drawing(44, 44)
    avatar.add(Circle(22, 22, 22, fillColor=AVATAR_BACKGROUND, strokeColor=None))
    avatar.add(
        String(
            22,
            16.5,
            initials,
            textAnchor="middle",
            fontName=PRINT_FONT_BOLD,
            fontSize=PRINT_AVATAR_SIZE,
            fillColor=PRIMARY,
        )
    )
    identity = [
        Paragraph(_paragraph_text(resident.name), styles["name"]),
        Paragraph(
            _paragraph_text(
                f"{training_level_label or default_training_level_name(resident.pgy)}"
                f" | {academic_year}"
            ),
            styles["meta"],
        ),
    ]
    header = Table(
        [[avatar, identity, Paragraph("RESIDENT SCHEDULE", styles["meta"])]],
        colWidths=[0.65 * inch, 7.65 * inch, 1.7 * inch],
        rowHeights=[0.62 * inch],
        hAlign="LEFT",
    )
    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (-1, 0), (-1, 0), "RIGHT"),
                ("TEXTCOLOR", (-1, 0), (-1, 0), PRIMARY),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LINEBELOW", (0, 0), (-1, -1), 1, PRIMARY),
            ]
        )
    )
    return header


def _block_schedule_table(rows: Sequence[BlockRow], styles: Mapping[str, ParagraphStyle]) -> Table:
    fields = (
        ("period", "Weeks"),
        ("rotation", "Rotation"),
    )
    table = _report_table(
        rows,
        fields,
        col_widths=[3.25, 6.75],
        bold_fields={"rotation"},
        styles=styles,
    )
    block_styles: list[tuple] = []
    for row_index, row in enumerate(rows, start=1):
        if row.get("kind") == "vacation":
            background = colors.HexColor(VACATION_TINT)
            accent = colors.HexColor(VACATION_COLOR)
        elif row.get("kind") == "special":
            background = colors.HexColor(CONFERENCE_TINT)
            accent = colors.HexColor(CONFERENCE_COLOR)
        else:
            rotation_color = row.get("color", "") or DEFAULT_PRIMARY_COLOR
            background = colors.HexColor(lighten_hex_color(rotation_color, white_mix=0.88))
            accent = colors.HexColor(rotation_color)
        block_styles.extend(
            [
                ("BACKGROUND", (1, row_index), (1, row_index), background),
                ("LINEBEFORE", (1, row_index), (1, row_index), 2, accent),
            ]
        )
    table.setStyle(TableStyle(block_styles))
    return table


def _clinic_schedule_calendars(
    rows: Sequence[ClinicRow], styles: Mapping[str, ParagraphStyle]
) -> list[object]:
    days = [
        (key, label)
        for key, label in (
            ("monday", "MON"),
            ("tuesday", "TUE"),
            ("wednesday", "WED"),
            ("thursday", "THU"),
            ("friday", "FRI"),
            ("saturday", "SAT"),
            ("sunday", "SUN"),
        )
        if any(f"{key}_date" in row for row in rows)
    ]
    if not days:
        return [
            _report_table(
                rows,
                (
                    ("dates", "Dates"),
                    ("week", "Academic week"),
                    ("rotation", "Rotation"),
                    ("sessions", "Clinic sessions"),
                ),
                col_widths=[1.8, 1.0, 3.2, 4.0],
                bold_fields={"rotation"},
                styles=styles,
            )
        ]
    return [_clinic_week_calendar(row, days, styles) for row in rows]


def _clinic_week_calendar(
    row: ClinicRow,
    days: Sequence[tuple[str, str]],
    styles: Mapping[str, ParagraphStyle],
) -> KeepTogether:
    heading = Table(
        [
            [
                Paragraph(
                    _paragraph_text(f"WEEK {row.get('week', '-')} | {row.get('dates', '-')}"),
                    styles["clinic_week"],
                ),
                Paragraph(
                    _paragraph_text(row.get("rotation", "-")),
                    styles["clinic_rotation"],
                ),
            ]
        ],
        colWidths=[5 * inch, 5 * inch],
        hAlign="LEFT",
    )
    heading.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), ROW_GREY),
                ("BOX", (0, 0), (-1, -1), 0.5, GRID_GREY),
                ("LINEBELOW", (0, 0), (-1, -1), 1.1, PRIMARY),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )

    data: list[list[Paragraph]] = [
        [Paragraph("", styles["clinic_day"])]
        + [
            Paragraph(
                f'{label}<br/><font color="{DEFAULT_PRIMARY_COLOR}">'
                f"{_paragraph_text(row.get(f'{key}_date', '-'))}</font>",
                styles["clinic_day"],
            )
            for key, label in days
        ]
    ]
    for session_key, session_label in (("morning", "AM"), ("afternoon", "PM")):
        cells = [Paragraph(session_label, styles["clinic_session"])]
        for day_key, _ in days:
            value = row.get(f"{day_key}_{session_key}", "")
            cells.append(
                Paragraph(
                    _paragraph_text(value or "-"),
                    styles["clinic_event" if value else "clinic_empty"],
                )
            )
        data.append(cells)

    day_width = 9.45 * inch / len(days)
    calendar = Table(
        data,
        colWidths=[0.55 * inch, *([day_width] * len(days))],
        hAlign="LEFT",
    )
    commands: list[tuple] = [
        ("BACKGROUND", (0, 0), (-1, 0), LIGHT_GREY),
        ("BACKGROUND", (0, 1), (0, -1), ROW_GREY),
        ("GRID", (0, 0), (-1, -1), 0.35, GRID_GREY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for row_index, session_key in enumerate(("morning", "afternoon"), start=1):
        for column_index, (day_key, _) in enumerate(days, start=1):
            key = f"{day_key}_{session_key}"
            if not row.get(key):
                continue
            kind = row.get(f"{key}_kind", "")
            if kind == "admin":
                background = colors.HexColor(ADMIN_TINT)
                accent = colors.HexColor(ADMIN_COLOR)
            else:
                tint = row.get(f"{key}_tint", "") or ADMIN_TINT
                color = row.get(f"{key}_color", "") or DEFAULT_PRIMARY_COLOR
                background = colors.HexColor(tint)
                accent = colors.HexColor(color)
            commands.extend(
                [
                    (
                        "BACKGROUND",
                        (column_index, row_index),
                        (column_index, row_index),
                        background,
                    ),
                    (
                        "LINEBEFORE",
                        (column_index, row_index),
                        (column_index, row_index),
                        2,
                        accent,
                    ),
                ]
            )
    calendar.setStyle(TableStyle(commands))
    return KeepTogether([heading, calendar, Spacer(1, 0.13 * inch)])


def _report_table(
    rows: Sequence[Mapping[str, str]],
    fields: Sequence[tuple[str, str]],
    *,
    col_widths: Sequence[float],
    bold_fields: set[str],
    styles: Mapping[str, ParagraphStyle],
) -> Table:
    data: list[list[Paragraph]] = [
        [Paragraph(_paragraph_text(label.upper()), styles["table_header"]) for _, label in fields]
    ]
    for row in rows:
        data.append(
            [
                Paragraph(
                    _paragraph_text(row.get(field, "-")),
                    styles["table_cell_bold" if field in bold_fields else "table_cell"],
                )
                for field, _ in fields
            ]
        )
    table = Table(
        data,
        colWidths=[width * inch for width in col_widths],
        repeatRows=1,
        hAlign="LEFT",
        splitByRow=1,
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), LIGHT_GREY),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ROW_GREY]),
                ("GRID", (0, 0), (-1, -1), 0.35, GRID_GREY),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def _empty_report(
    title: str,
    detail: str,
    styles: Mapping[str, ParagraphStyle],
) -> Table:
    callout = Table(
        [
            [Paragraph(_paragraph_text(title), styles["empty_title"])],
            [Paragraph(_paragraph_text(detail), styles["empty_detail"])],
        ],
        colWidths=[10 * inch],
        hAlign="LEFT",
    )
    callout.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), ROW_GREY),
                ("BOX", (0, 0), (-1, -1), 0.5, GRID_GREY),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, 0), 9),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 9),
            ]
        )
    )
    return callout


def _paragraph_text(value: object) -> str:
    text = str(value or "-")
    for dash in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2212"):
        text = text.replace(dash, "-")
    text = text.replace("\u00b7", "-")
    return escape(text).replace("\n", "<br/>")


def _page_footer(canvas, document) -> None:
    canvas.saveState()
    page_width, _ = landscape(letter)
    canvas.setStrokeColor(GRID_GREY)
    canvas.setLineWidth(0.5)
    canvas.line(0.5 * inch, 0.29 * inch, page_width - 0.5 * inch, 0.29 * inch)
    canvas.setFont(PRINT_FONT_REGULAR, PRINT_CAPTION_SIZE)
    canvas.setFillColor(NEUTRAL)
    canvas.drawString(0.5 * inch, 0.16 * inch, "RBS resident schedule")
    canvas.drawRightString(
        page_width - 0.5 * inch,
        0.16 * inch,
        f"Page {document.page}",
    )
    canvas.restoreState()
