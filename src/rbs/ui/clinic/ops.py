"""Clinic directory, allocation, closure, and academic half-day edits.

Pure ``SchedulerInput -> SchedulerInput`` operations. Kept free of NiceGUI so
the clinic policy rules they encode can be read and tested without a UI.
"""

from __future__ import annotations

from rbs.models.clinic import (
    ALL_CLINIC_SITES,
    ClinicAllocationRule,
    ClinicClosureDay,
    ClinicRule,
    ClinicSiteConfig,
    ClinicSlot,
)
from rbs.models.enums import WEEKDAYS_MF, Session, Weekday
from rbs.models.instance import SchedulerInput
from rbs.ui.drafts import Draft

__all__ = [
    "replace_clinic",
    "replace_primary_clinic",
    "add_clinic",
    "remove_clinic",
    "replace_clinic_allocation_rules",
    "replace_clinic_closure_days",
    "replace_academic_half_day",
    "set_academic_half_day_override",
    "remove_academic_half_day_override",
]


def replace_clinic(
    instance: SchedulerInput,
    original_id: str,
    replacement: ClinicSiteConfig | Draft,
) -> SchedulerInput:
    """Replace one clinic without changing its stable reference ID."""
    clinic = (
        replacement
        if isinstance(replacement, ClinicSiteConfig)
        else ClinicSiteConfig.model_validate(replacement)
    )
    if clinic.id != original_id:
        raise ValueError("clinic ID is a stable system key and cannot be changed")
    if original_id not in instance.clinic_policy.site_ids:
        raise ValueError(f"unknown clinic {original_id!r}")
    raw = instance.model_dump(mode="json")
    policy = raw["clinic_policy"]
    policy["sites"] = [
        clinic.model_dump(mode="json") if site["id"] == original_id else site
        for site in policy["sites"]
    ]
    _sync_clinic_allocation_view(policy)
    _sync_clinic_closure_view(policy)
    return SchedulerInput.from_payload(raw)


def replace_primary_clinic(
    instance: SchedulerInput,
    clinic_id: str,
) -> SchedulerInput:
    """Set the configurable fallback clinic used by flexible scheduling."""
    if clinic_id not in instance.clinic_policy.site_ids:
        raise ValueError(f"unknown clinic {clinic_id!r}")
    raw = instance.model_dump(mode="json")
    raw["clinic_policy"]["primary_site_id"] = clinic_id
    return SchedulerInput.from_payload(raw)


def add_clinic(
    instance: SchedulerInput,
    clinic: ClinicSiteConfig | Draft,
) -> SchedulerInput:
    """Add a clinic and a zero-target allocation rule."""
    added = (
        clinic if isinstance(clinic, ClinicSiteConfig) else ClinicSiteConfig.model_validate(clinic)
    )
    if added.id in instance.clinic_policy.site_ids:
        raise ValueError(f"clinic ID {added.id!r} is already configured")
    raw = instance.model_dump(mode="json")
    policy = raw["clinic_policy"]
    added_raw = added.model_dump(mode="json")
    if not added_raw.get("allocation_rules"):
        added_raw["allocation_rules"] = [
            ClinicAllocationRule(
                clinic_id=added.id,
                target_fraction=0.0,
            ).model_dump(mode="json")
        ]
    policy["sites"].append(added_raw)
    _sync_clinic_allocation_view(policy)
    _sync_clinic_closure_view(policy)
    return SchedulerInput.from_payload(raw)


def remove_clinic(instance: SchedulerInput, clinic_id: str) -> SchedulerInput:
    """Remove one clinic and repair allocation and rotation references."""
    if clinic_id not in instance.clinic_policy.site_ids:
        raise ValueError(f"unknown clinic {clinic_id!r}")
    if len(instance.clinic_policy.sites) <= 1:
        raise ValueError("at least one clinic must remain configured")

    raw = instance.model_dump(mode="json")
    policy = raw["clinic_policy"]
    policy["sites"] = [site for site in policy["sites"] if site["id"] != clinic_id]
    rules = [rule for rule in policy["allocation_rules"] if rule["clinic_id"] != clinic_id]
    grouped: dict[tuple[str, str], list[Draft]] = {}
    for rule in rules:
        scope = (
            ("resident", str(rule["resident_id"]))
            if rule.get("resident_id") is not None
            else ("pgy", str(rule["pgy"]))
            if rule.get("pgy") is not None
            else ("overall", "")
        )
        grouped.setdefault(scope, []).append(rule)
    for scoped_rules in grouped.values():
        target_total = sum(float(rule["target_fraction"]) for rule in scoped_rules)
        for rule in scoped_rules:
            target = (
                float(rule["target_fraction"]) / target_total
                if target_total > 0
                else 1.0 / len(scoped_rules)
            )
            rule["target_fraction"] = target
            rule["min_fraction"] = min(float(rule["min_fraction"]), target)
            rule["max_fraction"] = max(float(rule["max_fraction"]), target)
    policy["allocation_rules"] = rules
    if policy.get("primary_site_id") == clinic_id:
        overall_rules = [
            rule for rule in rules if rule.get("pgy") is None and rule.get("resident_id") is None
        ]
        policy["primary_site_id"] = max(
            overall_rules,
            key=lambda rule: float(rule["target_fraction"]),
        )["clinic_id"]
    _distribute_clinic_allocation_rules(policy)
    _remove_clinic_references(raw["rotations"], clinic_id)
    _sync_clinic_closure_view(policy)
    return SchedulerInput.from_payload(raw)


def replace_clinic_allocation_rules(
    instance: SchedulerInput,
    rules: list[ClinicAllocationRule | Draft],
) -> SchedulerInput:
    """Replace and validate allocation rules for the configured clinics."""
    raw = instance.model_dump(mode="json")
    raw["clinic_policy"]["allocation_rules"] = [
        rule.model_dump(mode="json") if isinstance(rule, ClinicAllocationRule) else dict(rule)
        for rule in rules
    ]
    _distribute_clinic_allocation_rules(raw["clinic_policy"])
    return SchedulerInput.from_payload(raw)


def _sync_clinic_allocation_view(policy: Draft) -> None:
    rules: list[Draft] = []
    for site in policy.get("sites", []):
        for rule in site.get("allocation_rules", []):
            item = dict(rule)
            item["clinic_id"] = site["id"]
            rules.append(item)
    policy["allocation_rules"] = rules


def _distribute_clinic_allocation_rules(policy: Draft) -> None:
    by_clinic: dict[str, list[Draft]] = {str(site["id"]): [] for site in policy.get("sites", [])}
    for rule in policy.get("allocation_rules", []):
        clinic_id = str(rule.get("clinic_id"))
        if clinic_id in by_clinic:
            by_clinic[clinic_id].append(dict(rule))
    for site in policy.get("sites", []):
        site["allocation_rules"] = by_clinic.get(str(site["id"]), [])


def _sync_clinic_closure_view(policy: Draft) -> None:
    grouped: dict[str, Draft] = {}
    for site in policy.get("sites", []):
        for closure in site.get("closure_days", []):
            closure_date = str(closure.get("date") or "")
            if not closure_date:
                continue
            item = grouped.setdefault(
                closure_date,
                {"date": closure_date, "sites": [], "name": ""},
            )
            item["sites"].append(site["id"])
            name = str(closure.get("name") or "").strip()
            if name and name not in str(item["name"]).split(" / "):
                item["name"] = " / ".join(filter(None, (str(item["name"]), name)))
    policy["closure_days"] = list(grouped.values())


def _remove_clinic_references(rotations: list[Draft], clinic_id: str) -> None:
    def repair(rule: Draft | None) -> None:
        if not isinstance(rule, dict):
            return
        for slot in rule.get("slots", []):
            sites = list(slot.get("sites") or [])
            if ALL_CLINIC_SITES in sites or clinic_id not in sites:
                continue
            remaining = [site_id for site_id in sites if site_id != clinic_id]
            slot["sites"] = remaining or [ALL_CLINIC_SITES]

    for rotation in rotations:
        repair(rotation.get("clinic"))


def replace_clinic_closure_days(
    instance: SchedulerInput,
    closure_days: list[ClinicClosureDay | Draft],
) -> SchedulerInput:
    """Return a validated instance with replacement site-specific closure dates."""
    raw = instance.model_dump(mode="json")
    raw["clinic_policy"]["closure_days"] = [
        closure.model_dump(mode="json") if isinstance(closure, ClinicClosureDay) else dict(closure)
        for closure in closure_days
    ]
    return SchedulerInput.from_payload(raw)


def replace_academic_half_day(
    instance: SchedulerInput,
    weekday: Weekday,
    session: Session,
) -> SchedulerInput:
    """Return a validated instance with a new system-wide academic half-day."""
    raw = instance.model_dump(mode="json")
    raw["clinic_policy"]["academic"] = {
        "weekday": weekday.value,
        "session": session.value,
        "sites": [],
    }
    return SchedulerInput.from_payload(raw)


def set_academic_half_day_override(
    instance: SchedulerInput,
    week: int,
    weekday: Weekday,
    session: Session,
) -> SchedulerInput:
    """Add or replace the Academic half-day override for one week."""
    raw = instance.model_dump(mode="json")
    overrides = [
        item for item in raw.get("academic_half_day_overrides", []) if int(item["week"]) != week
    ]
    overrides.append(
        {
            "week": week,
            "weekday": weekday.value,
            "session": session.value,
        }
    )
    raw["academic_half_day_overrides"] = overrides
    return SchedulerInput.from_payload(raw)


def remove_academic_half_day_override(
    instance: SchedulerInput,
    week: int,
) -> SchedulerInput:
    """Remove one week-specific Academic override."""
    raw = instance.model_dump(mode="json")
    existing = raw.get("academic_half_day_overrides", [])
    overrides = [item for item in existing if int(item["week"]) != week]
    if len(overrides) == len(existing):
        raise ValueError(f"academic week {week} does not have an override")
    raw["academic_half_day_overrides"] = overrides
    return SchedulerInput.from_payload(raw)


def _new_clinic_draft(instance: SchedulerInput) -> Draft:
    existing = set(instance.clinic_policy.site_ids)
    index = len(existing) + 1
    clinic_id = f"clinic_{index}"
    while clinic_id in existing:
        index += 1
        clinic_id = f"clinic_{index}"
    colors = tuple(instance.color_scheme.palette)
    return {
        "id": clinic_id,
        "name": f"Clinic {index}",
        "color": colors[(index - 1) % len(colors)],
        "residents_per_attending": 4,
        "half_days": [],
        "capacity_overrides": [],
        "closure_days": [],
        "allocation_rules": [
            {
                "clinic_id": clinic_id,
                "pgy": None,
                "resident_id": None,
                "min_fraction": 0.0,
                "target_fraction": 0.0,
                "max_fraction": 1.0,
            }
        ],
    }


def _default_clinic_rule() -> Draft:
    return ClinicRule(
        half_days_per_week=1,
        slots=[
            ClinicSlot(
                weekday=weekday,
                session=session,
                sites=[ALL_CLINIC_SITES],
            )
            for weekday in WEEKDAYS_MF
            for session in Session
        ],
    ).model_dump(mode="json")
