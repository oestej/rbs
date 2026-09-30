"""CSV display exports: preserve cell boundaries and treat formulas as text.

This is spreadsheet-facing output, not the lossless portable document format.
Spreadsheet applications can discard text markers when saving CSV again; these
protections apply to the file RBS exports, not subsequent third-party rewrites.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from io import StringIO

_FORMULA_PREFIXES = ("=", "+", "-", "@", "＝", "＋", "－", "＠")


def build_spreadsheet_csv(headers: Iterable[str], rows: Iterable[Iterable[str]]) -> str:
    """Quote every field and prefix formula-like display text with an apostrophe."""
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n", quoting=csv.QUOTE_ALL)
    writer.writerow([_spreadsheet_text(value) for value in headers])
    writer.writerows([_spreadsheet_text(value) for value in row] for row in rows)
    return output.getvalue()


def _spreadsheet_text(value: str) -> str:
    # Quoting alone does not stop formulas. Check past leading whitespace but
    # retain the original text, with its marker before any whitespace/controls.
    if value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value
