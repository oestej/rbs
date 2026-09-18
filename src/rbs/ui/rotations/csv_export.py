"""CSV export for rotation configuration.

One row per rotation mirrors the Rotations table so the spreadsheet shows
every configurable parameter in a single view.
"""

from __future__ import annotations

import csv
import re
from datetime import date
from io import StringIO

from rbs.models.instance import SchedulerInput
from rbs.ui.rotations.table import ROTATION_COLUMNS, rotation_rows

MAX_TOTAL_WEEKS_FIELD = "max_total_weeks"
MAX_TOTAL_WEEKS_BY_PGY_FIELD = "max_total_weeks_by_training_level"


def rotation_csv_columns() -> tuple[tuple[str, str], ...]:
    """Column field keys and spreadsheet headers for the rotations export."""
    return (
        *((column["field"], column["label"]) for column in ROTATION_COLUMNS),
        (MAX_TOTAL_WEEKS_FIELD, "Max total weeks"),
        (MAX_TOTAL_WEEKS_BY_PGY_FIELD, "Max total weeks by training level"),
    )


def rotation_csv_rows(instance: SchedulerInput) -> list[dict[str, str]]:
    """Return one flat row per rotation, including total-week limits.

    Display values reuse the Rotations table so the screen and the export
    tell the same story; the table has no column for total-week caps, so
    those are appended explicitly.
    """
    rows = rotation_rows(instance)
    rotations = {rotation.id: rotation for rotation in instance.rotations}
    for row in rows:
        rotation = rotations[row["id"]]
        row[MAX_TOTAL_WEEKS_FIELD] = (
            str(rotation.max_total_weeks) if rotation.max_total_weeks is not None else ""
        )
        bits = [
            f"{instance.training_level_name(rule.pgy)} max {rule.max_total_weeks} wk"
            for rule in sorted(rotation.pgy_rules, key=lambda rule: rule.pgy)
            if rule.max_total_weeks is not None
        ]
        row[MAX_TOTAL_WEEKS_BY_PGY_FIELD] = "; ".join(bits)
    return rows


def build_rotations_csv(instance: SchedulerInput) -> str:
    """Build the full rotations configuration as spreadsheet-friendly CSV."""
    columns = rotation_csv_columns()
    # rotation_csv_rows already follows rotation display order.
    rows = rotation_csv_rows(instance)
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([label for _field, label in columns])
    for row in rows:
        writer.writerow([row.get(field, "") for field, _label in columns])
    return output.getvalue()


def rotations_csv_filename(
    academic_year: str,
    *,
    exported_on: date | None = None,
) -> str:
    year_slug = re.sub(r"[^0-9a-z]+", "-", academic_year.lower()).strip("-")
    export_date = (exported_on or date.today()).isoformat()
    return f"rotations-{year_slug}-exported-{export_date}.csv"
