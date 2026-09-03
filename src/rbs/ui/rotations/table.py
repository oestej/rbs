"""Rotation-constraint rows for the NiceGUI Rotations tab."""

from __future__ import annotations

from rbs.models.clinic import ClinicPolicy, ClinicRule, ClinicSlot
from rbs.models.enums import WEEKDAYS_MF, Session, Weekday
from rbs.models.instance import SchedulerInput
from rbs.models.rotation import Rotation
from rbs.ui.editor_common import (
    _academic_block_name,
    _academic_block_start_for_week,
)

DAY_SHORT = {
    Weekday.MONDAY: "Mon",
    Weekday.TUESDAY: "Tue",
    Weekday.WEDNESDAY: "Wed",
    Weekday.THURSDAY: "Thu",
    Weekday.FRIDAY: "Fri",
    Weekday.SATURDAY: "Sat",
    Weekday.SUNDAY: "Sun",
}
SESS_SHORT = {Session.MORNING: "AM", Session.AFTERNOON: "PM"}

ROTATION_COLUMNS = [
    {"name": "code", "label": "Code", "field": "code", "align": "left"},
    {"name": "name", "label": "Name", "field": "name", "align": "left"},
    {"name": "color", "label": "Color", "field": "color", "align": "left"},
    {"name": "kind", "label": "Kind", "field": "kind", "align": "left"},
    {"name": "duration", "label": "Weeks", "field": "duration", "align": "left"},
    {"name": "away", "label": "Away", "field": "away"},
    {"name": "no_clinic_hours", "label": "No clinic", "field": "no_clinic_hours"},
    {"name": "no_weekend_call", "label": "No weekend call", "field": "no_weekend_call"},
    {"name": "vacation", "label": "Vacation", "field": "vacation", "align": "left"},
    {"name": "placement", "label": "Placement", "field": "placement", "align": "left"},
    {"name": "capacity", "label": "Capacity", "field": "capacity", "align": "left"},
    {"name": "consecutive", "label": "Consecutive", "field": "consecutive", "align": "left"},
    {"name": "grouping", "label": "Grouping", "field": "grouping", "align": "left"},
    {"name": "clinic", "label": "Clinic", "field": "clinic", "align": "left"},
    {"name": "curriculum", "label": "Curriculum", "field": "curriculum", "align": "left"},
]


def rotation_rows(instance: SchedulerInput) -> list[dict]:
    return [
        _rotation_row(instance, rotation)
        for rotation in sorted(instance.rotations, key=lambda item: item.code.casefold())
    ]


def _rotation_row(instance: SchedulerInput, rotation: Rotation) -> dict:
    return {
        "id": rotation.id,
        "code": rotation.code,
        "name": rotation.name,
        "color": rotation.color,
        "kind": rotation.kind.value,
        "duration": _block_configs(instance, rotation),
        "away": "yes" if rotation.away else "no",
        "no_clinic_hours": "yes" if rotation.clinic_hours_disabled else "no",
        "no_weekend_call": "yes" if rotation.no_weekend_call else "no",
        "vacation": _vacation(instance, rotation),
        "placement": _placement(instance, rotation),
        "capacity": _capacity(instance, rotation),
        "consecutive": (
            f"max {rotation.max_consecutive_weeks} wk" if rotation.max_consecutive_weeks else "—"
        ),
        "grouping": _grouping(instance, rotation),
        "clinic": (
            "none"
            if rotation.clinic_hours_disabled
            else _clinic(instance, rotation.clinic, instance.clinic_policy)
        ),
        "curriculum": _curriculum(instance, rotation.id),
    }


def _vacation(instance: SchedulerInput, rotation: Rotation) -> str:
    bits: list[str] = []
    for rule in rotation.pgy_rules:
        for config in rule.block_configs:
            if not config.vacation.allowed:
                label = "no"
            elif config.vacation.max_weeks_per_block is not None:
                label = f"yes, max {config.vacation.max_weeks_per_block} wk"
            else:
                label = "yes"
            bits.append(
                f"{instance.training_level_name(rule.pgy)} {config.duration_weeks}wk: {label}"
            )
    return "; ".join(bits)


def _block_configs(instance: SchedulerInput, rotation: Rotation) -> str:
    return "; ".join(
        f"{instance.training_level_name(rule.pgy)}: "
        + ", ".join(f"{config.duration_weeks}wk" for config in rule.block_configs)
        for rule in rotation.pgy_rules
    )


def _capacity(instance: SchedulerInput, rotation: Rotation) -> str:
    cap = rotation.capacity
    bits = []
    if cap.min_concurrent is not None:
        bits.append(f"min {cap.min_concurrent}")
    if cap.max_concurrent is not None:
        bits.append(f"max {cap.max_concurrent}")
    for rule in rotation.pgy_rules:
        limits = []
        if rule.min_concurrent is not None:
            limits.append(f"min {rule.min_concurrent}")
        if rule.max_concurrent is not None:
            limits.append(f"max {rule.max_concurrent}")
        if limits:
            bits.append(f"{instance.training_level_name(rule.pgy)} {'/'.join(limits)}")
    return "; ".join(bits) if bits else "—"


def _grouping(instance: SchedulerInput, rotation: Rotation) -> str:
    parts = []
    for group in instance.rotation_groups:
        if rotation.id not in group.rotation_ids:
            continue
        members = " + ".join(instance.rotation(item).code for item in group.rotation_ids)
        parts.append(f"{instance.training_level_name(group.pgy)}: {members}")
    return "; ".join(parts) if parts else "—"


def _clinic(
    instance: SchedulerInput,
    rule: ClinicRule | None,
    policy: ClinicPolicy,
) -> str:
    if rule is None:
        return "none"
    half_day_count = rule.half_days_per_week
    bits = [f"{half_day_count} {'half-day' if half_day_count == 1 else 'half-days'} per week"]
    if rule.admin_half_days_per_week:
        bits.append(
            f"{rule.admin_half_days_per_week} admin half-day"
            + ("s" if rule.admin_half_days_per_week != 1 else "")
        )
    if rule.max_concurrent is not None:
        bits.append(f"max {rule.max_concurrent} concurrent")
    for pgy in sorted(
        rule.max_concurrent_by_pgy,
        key=instance.training_level_sort_key,
    ):
        maximum = rule.max_concurrent_by_pgy[pgy]
        bits.append(f"{instance.training_level_name(pgy)} max {maximum} concurrent")
    if rule.no_academic_day_attendance:
        bits.append("no academic day attendance")
    slots = _slot_summary(rule.slots, policy)
    if slots:
        bits.append(slots)
    preferred = [
        f"{DAY_SHORT[slot.weekday]} {SESS_SHORT[slot.session]}"
        for slot in rule.slots
        if slot.preferred and slot.weekday is not None and slot.session is not None
    ]
    if preferred:
        bits.append(f"prefer {', '.join(preferred)}")
    return "; ".join(bits)


def _placement(instance: SchedulerInput, rotation: Rotation) -> str:
    codes = {item.id: item.code for item in instance.rotations}
    bits: list[str] = []
    for rule in rotation.pgy_rules:
        details: list[str] = []
        if rule.prerequisite_rotation_ids:
            details.append(
                "after "
                + ", ".join(
                    codes.get(rotation_id, rotation_id)
                    for rotation_id in rule.prerequisite_rotation_ids
                )
            )
        if rule.earliest_start_week is not None:
            start_week = _academic_block_start_for_week(
                rule.earliest_start_week,
                instance.calendar.weeks,
            )
            assert start_week is not None
            details.append(f"from {_academic_block_name(start_week)}")
        if details:
            bits.append(f"{instance.training_level_name(rule.pgy)} {'; '.join(details)}")
    return "; ".join(bits) if bits else "Any week"


def _curriculum(instance: SchedulerInput, rotation_id: str) -> str:
    bits: list[str] = []
    for curriculum in instance.requirements:
        for block in curriculum.blocks:
            if block.rotation_id != rotation_id:
                continue
            parts = [f"{curriculum.display_label} {block.count}×{block.duration_weeks}wk"]
            bits.append("; ".join(parts))
    return " · ".join(bits) if bits else "—"


def _slot_summary(slots: list[ClinicSlot], policy: ClinicPolicy) -> str:
    if not slots:
        return ""
    keys = {(slot.weekday, slot.session) for slot in slots}
    if keys == {(day, session) for day in WEEKDAYS_MF for session in Session}:
        return "any M–F half-day"
    labels = [_slot_label(slot, policy) for slot in slots]
    seen: list[str] = []
    for label in labels:
        if label not in seen:
            seen.append(label)
    return ", ".join(seen)


def _slot_label(slot: ClinicSlot, policy: ClinicPolicy) -> str:
    day = DAY_SHORT[slot.weekday] if slot.weekday is not None else "any day"
    session = SESS_SHORT[slot.session] if slot.session is not None else "AM/PM"
    text = f"{day} {session}"
    if slot.sites:
        text += " " + "/".join(
            policy.site_name(site_id) for site_id in policy.resolve_site_ids(slot.sites)
        )
    return text
