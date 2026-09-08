"""Renderer-independent helpers shared by schedule views and exports."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

from rbs.models.rotation import ROTATION_COLOR_PALETTE


def rotation_color_class(rotation_color: str) -> str:
    """Return the stable CSS class assigned to a configured rotation color.

    The hash fallback keeps older callers and malformed external schedule
    labels deterministic; configured rotations use the shared palette branch.
    """
    normalized = rotation_color.strip().upper()
    try:
        index = tuple(ROTATION_COLOR_PALETTE).index(normalized)
    except ValueError:
        digest = hashlib.sha256(rotation_color.encode()).hexdigest()
        index = int(digest[:2], 16) % 24
    return f"rbs-rotation-color-{index}"


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
        week
        for week in weeks
        if week_monday(first_week_start, week) + timedelta(days=6) >= cutoff
    ]


def four_week_block_groups(weeks: list[int]) -> list[tuple[str, list[int]]]:
    """Group visible weeks into academic Blocks A through M."""
    groups: list[tuple[str, list[int]]] = []
    for week in weeks:
        label = chr(ord("A") + (week - 1) // 4)
        if groups and groups[-1][0] == label:
            groups[-1][1].append(week)
        else:
            groups.append((label, [week]))
    return groups
