"""CP-SAT rules for one attending's week.

The block solve's precepting envelope and the weekly attending pass build
their models from this one function. Sharing it is what makes the envelope
sound: a precepting pattern the envelope admits was proven possible under
exactly the rules the weekly pass later enforces.

Rules, in the language of the Attendings tab:

- work fills only open half-days, one category per half-day;
- the weekly half-day total is never exceeded, and is met exactly in full
  weeks without vacation;
- Fixed minimum/maximum ranges hold (minimums only in full weeks);
- the Attending Clinic day minimum holds in full weeks without weekday
  vacation.

In ``relaxed`` mode every rule a hand-entered week could already break (a
minimum or an exact total) becomes a counted violation instead of a hard
constraint, so a week with contradictory setup still gets a schedule.
Maximums stay hard: they can always be met by placing less work.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rbs.models.attending import AttendingWorkType
from rbs.models.enums import Session, Weekday
from rbs.solver.attending_availability import AttendingWeekFacts, HalfDay

ChoiceKey = tuple[HalfDay, AttendingWorkType, str | None]
ClinicOpen = Callable[[str, Weekday, Session], bool]
"""Whether attendings may precept at a clinic on one half-day of the week."""


@dataclass
class AttendingWeekModel:
    """Handles for one attending-week added to a CP-SAT model."""

    facts: AttendingWeekFacts
    choices: dict[ChoiceKey, Any] = field(default_factory=dict)
    by_half_day: dict[HalfDay, list[Any]] = field(default_factory=lambda: defaultdict(list))
    generated: dict[AttendingWorkType, list[Any]] = field(
        default_factory=lambda: defaultdict(list)
    )
    violations: list[tuple[Any, int]] = field(default_factory=list)
    """Relaxed-rule shortfalls as ``(expression, upper bound)`` pairs."""

    def generated_count(self, work_type: AttendingWorkType):
        return sum(self.generated.get(work_type, ()))

    def precepting(self, clinic_ids: set[str] | None = None) -> list[Any]:
        return [
            literal
            for (_half_day, work_type, clinic_id), literal in self.choices.items()
            if work_type is AttendingWorkType.PRECEPTING_CLINIC
            and (clinic_ids is None or clinic_id in clinic_ids)
        ]


def add_attending_week(
    model,
    facts: AttendingWeekFacts,
    *,
    prefix: str,
    clinic_open: ClinicOpen,
    relaxed: bool,
) -> AttendingWeekModel:
    """Add one attending-week's choices and rules to ``model``."""
    handles = AttendingWeekModel(facts=facts)
    for half_day in facts.open_half_days:
        weekday, session = half_day
        options: list[tuple[AttendingWorkType, str | None]] = []
        for work_type in sorted(facts.work_types):
            if work_type is AttendingWorkType.PRECEPTING_CLINIC:
                options.extend(
                    (work_type, clinic_id)
                    for clinic_id in facts.precepting_clinics
                    if clinic_open(clinic_id, weekday, session)
                )
            else:
                options.append((work_type, None))
        for work_type, clinic_id in options:
            literal = model.NewBoolVar(
                f"{prefix}:{weekday.value}:{session.value}:{work_type.value}"
                + (f":{clinic_id}" if clinic_id else "")
            )
            handles.choices[half_day, work_type, clinic_id] = literal
            handles.by_half_day[half_day].append(literal)
            handles.generated[work_type].append(literal)
        if len(handles.by_half_day[half_day]) > 1:
            model.AddAtMostOne(handles.by_half_day[half_day])

    all_generated = list(handles.choices.values())
    remaining = facts.remaining
    if all_generated:
        model.Add(sum(all_generated) <= remaining)
    if facts.full_week and remaining > 0:
        _at_least(
            model,
            handles,
            sum(all_generated),
            remaining,
            relaxed=relaxed,
            name=f"{prefix}:total",
        )

    for work_type, (minimum, maximum) in facts.fixed_ranges:
        fixed = facts.fixed_count(work_type)
        generated = handles.generated_count(work_type)
        if handles.generated.get(work_type):
            model.Add(generated <= max(0, maximum - fixed))
        if facts.full_week and minimum > fixed:
            _at_least(
                model,
                handles,
                generated,
                minimum - fixed,
                relaxed=relaxed,
                name=f"{prefix}:{work_type.value}:min",
            )

    # The effective week already zeroes the minimum in partial, inactive, and
    # weekday-vacation weeks. A weekend vacation keeps it, as the schedule
    # checks do, even though it relaxes the weekly total.
    needed_days = facts.clinic_day_minimum - len(facts.fixed_clinic_days)
    if needed_days > 0:
        day_literals = []
        for weekday in Weekday:
            if weekday in facts.fixed_clinic_days:
                continue
            clinic = [
                literal
                for (half_day, work_type, _clinic_id), literal in handles.choices.items()
                if half_day[0] is weekday and work_type is AttendingWorkType.ATTENDING_CLINIC
            ]
            if not clinic:
                continue
            day = model.NewBoolVar(f"{prefix}:{weekday.value}:clinic_day")
            model.Add(day <= sum(clinic))
            day_literals.append(day)
        _at_least(
            model,
            handles,
            sum(day_literals) if day_literals else 0,
            needed_days,
            relaxed=relaxed,
            name=f"{prefix}:clinic_days",
        )
    return handles


def _at_least(
    model,
    handles: AttendingWeekModel,
    expression,
    minimum: int,
    *,
    relaxed: bool,
    name: str,
) -> None:
    if isinstance(expression, int):
        if expression >= minimum:
            return
        if not relaxed:
            model.AddBoolOr([])  # No open half-day can satisfy this rule.
            return
        handles.violations.append((minimum - expression, minimum - expression))
        return
    if not relaxed:
        model.Add(expression >= minimum)
        return
    shortfall = model.NewIntVar(0, minimum, f"{name}:short")
    model.Add(expression + shortfall >= minimum)
    handles.violations.append((shortfall, minimum))


__all__ = ["AttendingWeekModel", "ChoiceKey", "ClinicOpen", "add_attending_week"]
