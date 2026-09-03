"""Shared editor constants and NiceGUI bind converters for rotation and clinic UIs."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, timedelta
from typing import Any

from pydantic import ValidationError

from rbs.models.enums import Session, Weekday
from rbs.ui.drafts import Draft

_DURATION_OPTIONS = {week: f"{week} week" if week == 1 else f"{week} weeks" for week in range(1, 6)}
_DEFAULT_BLOCK_DURATION_WEEKS = 2
_CONSECUTIVE_OPTIONS = {
    week: f"{week} week" if week == 1 else f"{week} weeks" for week in range(1, 7)
}
_CLINIC_WEEK = (
    Weekday.SUNDAY,
    Weekday.MONDAY,
    Weekday.TUESDAY,
    Weekday.WEDNESDAY,
    Weekday.THURSDAY,
    Weekday.FRIDAY,
    Weekday.SATURDAY,
)
_WEEKDAY_OPTIONS = {weekday.value: weekday.value.title() for weekday in _CLINIC_WEEK}
_SESSION_OPTIONS = {
    Session.MORNING.value: "Morning",
    Session.AFTERNOON.value: "Afternoon",
}
_ACADEMIC_BLOCK_WEEKS = 4


def _default_block_duration(options: Iterable[int]) -> int | None:
    """Prefer the two-week default, falling back to the first valid duration."""
    durations = tuple(int(duration) for duration in options)
    if _DEFAULT_BLOCK_DURATION_WEEKS in durations:
        return _DEFAULT_BLOCK_DURATION_WEEKS
    return durations[0] if durations else None


def _academic_block_start_options(
    first_week_start: date,
    calendar_weeks: int,
) -> dict[int, str]:
    """Map each four-week academic block's first week to its display label."""
    return {
        start_week: _academic_block_option_label(first_week_start, start_week)
        for start_week in range(1, calendar_weeks + 1, _ACADEMIC_BLOCK_WEEKS)
    }


def _academic_block_option_label(first_week_start: date, start_week: int) -> str:
    start_date = _academic_block_start_date(first_week_start, start_week)
    return f"{_academic_block_name(start_week)} · {start_date:%b} {start_date.day}"


def _academic_block_start_date(first_week_start: date, start_week: int) -> date:
    return first_week_start + timedelta(weeks=start_week - 1)


def _academic_block_name(start_week: int) -> str:
    """Return the compact schedule name for a four-week academic block."""
    block_number = (start_week - 1) // _ACADEMIC_BLOCK_WEEKS + 1
    block_letter = chr(ord("A") + block_number - 1)
    return f"Block {block_letter}/{block_number}"


def _academic_block_start_for_week(
    week: int | None,
    calendar_weeks: int,
) -> int | None:
    """Normalize a legacy threshold to the first block start not preceding it."""
    if week is None:
        return None
    bounded = min(max(int(week), 1), calendar_weeks)
    offset = (bounded - 1) % _ACADEMIC_BLOCK_WEEKS
    candidate = bounded if offset == 0 else bounded + _ACADEMIC_BLOCK_WEEKS - offset
    last_start = ((calendar_weeks - 1) // _ACADEMIC_BLOCK_WEEKS) * _ACADEMIC_BLOCK_WEEKS + 1
    return min(candidate, last_start)


def _capacity_range_label(minimum: int | None, maximum: int | None) -> str:
    if minimum is None and maximum is None:
        return "No minimum or maximum"
    if minimum is not None and maximum is not None:
        if minimum == maximum:
            return _resident_count_label(minimum)
        return f"{minimum}–{maximum} residents"
    if minimum is not None:
        return f"At least {_resident_count_label(minimum)}"
    return f"At most {_resident_count_label(maximum or 0)}"


def _clinic_capacity_range_label(
    minimum: int | None,
    maximum: int | None,
) -> str:
    if minimum is None and maximum is None:
        return "Constrained by clinic capacity only"
    return _capacity_range_label(minimum, maximum)


def _resident_count_label(count: int) -> str:
    return f"{count} resident" if count == 1 else f"{count} residents"


def _vacation_label(vacation) -> str:
    if not vacation.allowed:
        return "Vacation not allowed"
    if vacation.max_weeks_per_block is None:
        return "Vacation allowed"
    maximum = vacation.max_weeks_per_block
    unit = "week" if maximum == 1 else "weeks"
    return f"Vacation allowed · max {maximum} {unit}"


def _clinic_pgy_capacity_label(rule: Draft) -> str:
    return _clinic_capacity_range_label(
        int(rule["min_concurrent"]) if rule.get("min_concurrent") is not None else None,
        int(rule["max_concurrent"]) if rule.get("max_concurrent") is not None else None,
    )


def _pgy_capacity_label(rule: Draft) -> str:
    return _capacity_range_label(
        int(rule["min_concurrent"]) if rule.get("min_concurrent") is not None else None,
        int(rule["max_concurrent"]) if rule.get("max_concurrent") is not None else None,
    )


def _remove_index(
    items: list,
    index: int,
    refresh: Callable[[], None],
) -> None:
    if 0 <= index < len(items):
        items.pop(index)
        refresh()


def _as_text(value: Any) -> str:
    return str(value or "")


def _as_code(value: Any) -> str:
    return str(value or "").upper()


def _as_int(value: Any) -> int | None:
    return None if value is None or value == "" else int(value)


def _as_string_list(value: Any) -> list[str]:
    return [str(item) for item in (value or [])]


def _as_percent(value: Any) -> float:
    return float(value or 0) / 100.0


def _from_percent(value: Any) -> float:
    return float(value or 0) * 100.0


def _optional_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _weeks_label(weeks: int) -> str:
    return f"{weeks} week" if weeks == 1 else f"{weeks} weeks"


def _validation_message(exc: ValidationError | ValueError) -> str:
    if not isinstance(exc, ValidationError):
        return str(exc)
    messages = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error["loc"])
        prefix = f"{location}: " if location else ""
        messages.append(prefix + str(error["msg"]))
    return "\n".join(messages)
