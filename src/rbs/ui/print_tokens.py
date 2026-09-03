"""Shared typography tokens for printable schedule exports.

Screen typography is expressed in CSS rem units. ReportLab works in points and
ships with its own PDF-safe Helvetica family, so exports deliberately use a
small, separate scale rather than pretending the two media are interchangeable.
"""

from __future__ import annotations

PRINT_FONT_REGULAR = "Helvetica"
PRINT_FONT_BOLD = "Helvetica-Bold"

PRINT_TITLE_SIZE = 18
PRINT_TITLE_LEADING = 21
PRINT_SECTION_SIZE = 14
PRINT_SECTION_LEADING = 17
PRINT_BODY_SIZE = 9
PRINT_BODY_LEADING = 11
PRINT_BODY_LOOSE_LEADING = 12
PRINT_SMALL_SIZE = 8
PRINT_SMALL_LEADING = 10
PRINT_CAPTION_SIZE = 7
PRINT_CAPTION_LEADING = 9
PRINT_MICRO_SIZE = 6
PRINT_MICRO_LEADING = 7
PRINT_AVATAR_SIZE = 15

__all__ = [
    "PRINT_AVATAR_SIZE",
    "PRINT_BODY_LEADING",
    "PRINT_BODY_LOOSE_LEADING",
    "PRINT_BODY_SIZE",
    "PRINT_CAPTION_LEADING",
    "PRINT_CAPTION_SIZE",
    "PRINT_FONT_BOLD",
    "PRINT_FONT_REGULAR",
    "PRINT_MICRO_LEADING",
    "PRINT_MICRO_SIZE",
    "PRINT_SECTION_LEADING",
    "PRINT_SECTION_SIZE",
    "PRINT_SMALL_LEADING",
    "PRINT_SMALL_SIZE",
    "PRINT_TITLE_LEADING",
    "PRINT_TITLE_SIZE",
]
