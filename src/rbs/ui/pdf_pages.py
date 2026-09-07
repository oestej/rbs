"""Shared page decorations for PDF exports."""

from collections.abc import Callable
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import BaseDocTemplate

from rbs.models.color_scheme import DEFAULT_NEUTRAL_COLOR
from rbs.ui.print_tokens import PRINT_FONT_REGULAR, PRINT_SMALL_SIZE

PageCallback = Callable[[Canvas, BaseDocTemplate], None]


def with_export_timestamp(footer: PageCallback) -> PageCallback:
    """Add a top-right timestamp, captured once in local time for the whole PDF."""
    exported_at = datetime.now().astimezone()
    time_label = exported_at.strftime("%I:%M %p %Z").lstrip("0")
    label = f"Exported {exported_at:%B} {exported_at.day}, {exported_at:%Y} at {time_label}"

    def draw_page(canvas: Canvas, document: BaseDocTemplate) -> None:
        footer(canvas, document)
        canvas.saveState()
        canvas.setFont(PRINT_FONT_REGULAR, PRINT_SMALL_SIZE)
        canvas.setFillColor(colors.HexColor(DEFAULT_NEUTRAL_COLOR))
        page_width, page_height = document.pagesize
        canvas.drawRightString(
            page_width - document.rightMargin,
            page_height - 0.25 * inch,
            label,
        )
        canvas.restoreState()

    return draw_page
