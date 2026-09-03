"""Clinic policy: the composition root for sites, rules, and allocation."""

from __future__ import annotations

from datetime import date
from functools import cached_property
from typing import Any

from pydantic import Field, field_validator, model_validator

from rbs.models.clinic_rules import ClinicAllocationRule, ClinicClosureDay, ClinicSlot
from rbs.models.clinic_site import (
    ALL_CLINIC_SITES,
    ClinicSiteConfig,
    normalize_clinic_site_ids,
)
from rbs.models.common import StrictModel
from rbs.models.enums import Session, Weekday


class ClinicPolicy(StrictModel):
    """Clinic directory, allocation targets, and shared academic rules."""

    sites: list[ClinicSiteConfig] = Field(min_length=1)
    allocation_rules: list[ClinicAllocationRule] = Field(default_factory=list)
    primary_site_id: str = Field(
        description=(
            "Configurable deterministic fallback for flexible sessions and "
            "solver-stage staffing estimates."
        )
    )
    # The aggregate closure list is a read view rebuilt from the sites on
    # every validation. The editor and scheduling code use each
    # ClinicSiteConfig.closure_days list as the source of truth.
    closure_days: list[ClinicClosureDay] = Field(default_factory=list)
    academic: ClinicSlot = Field(description="Recurring program-wide academic half-day.")
    notes: str = ""

    @field_validator("primary_site_id")
    @classmethod
    def normalize_site_reference(cls, value: str) -> str:
        normalized = normalize_clinic_site_ids([value])
        if normalized == [ALL_CLINIC_SITES]:
            raise ValueError("clinic policy site references must select one configured site")
        return normalized[0]

    @field_validator("closure_days")
    @classmethod
    def unique_closure_dates(
        cls,
        closure_days: list[ClinicClosureDay],
    ) -> list[ClinicClosureDay]:
        dates = [closure.date for closure in closure_days]
        if len(dates) != len(set(dates)):
            raise ValueError("clinic closure dates must be unique")
        return sorted(closure_days, key=lambda closure: closure.date)

    @model_validator(mode="before")
    @classmethod
    def sync_derived_views(cls, value: Any) -> Any:
        """Keep each site's lists and the policy-level views in sync.

        Site-authored allocation rules are hoisted to the policy level,
        missing overall allocation entries default to zero targets, and
        every rule is distributed back to its clinic. A top-level closure
        list wins when present and is persisted under each clinic,
        otherwise the aggregate is rebuilt from the sites — matching the
        workspace editor, which maintains both views of the same data.
        """
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        raw_sites = migrated.get("sites")
        if isinstance(raw_sites, list):
            migrated["sites"] = [
                site.model_dump(mode="json")
                if isinstance(site, ClinicSiteConfig)
                else site
                for site in raw_sites
            ]
        sites = migrated.get("sites", [])
        site_ids = [str(site.get("id")) for site in sites if isinstance(site, dict)]

        nested_rules: list[dict[str, Any]] = []
        for site in sites:
            if not isinstance(site, dict):
                continue
            for rule_value in site.get("allocation_rules") or []:
                rule = (
                    rule_value.model_dump(mode="json")
                    if isinstance(rule_value, ClinicAllocationRule)
                    else dict(rule_value)
                    if isinstance(rule_value, dict)
                    else None
                )
                if rule is None:
                    continue
                rule["clinic_id"] = str(site.get("id"))
                nested_rules.append(rule)
        if "allocation_rules" not in migrated and nested_rules:
            migrated["allocation_rules"] = nested_rules
        raw_rules = [
            rule.model_dump(mode="json")
            if isinstance(rule, ClinicAllocationRule)
            else dict(rule)
            for rule in migrated.get("allocation_rules") or []
            if isinstance(rule, (dict, ClinicAllocationRule))
        ]
        grouped_rules: dict[tuple[str, Any], list[dict[str, Any]]] = {}
        for rule in raw_rules:
            scope = (
                ("resident", str(rule["resident_id"]))
                if rule.get("resident_id") is not None
                else ("pgy", int(rule["pgy"]))
                if rule.get("pgy") is not None
                else ("overall", None)
            )
            grouped_rules.setdefault(scope, []).append(rule)
        for scope, rules in grouped_rules.items():
            if scope[0] != "overall":
                continue
            referenced = {str(rule.get("clinic_id")) for rule in rules}
            for site_id in site_ids:
                if site_id in referenced:
                    continue
                rules.append(
                    {
                        "clinic_id": site_id,
                        "pgy": None,
                        "resident_id": None,
                        "min_fraction": 0.0,
                        "target_fraction": 0.0,
                        "max_fraction": 1.0,
                    }
                )
        raw_rules = [rule for rules in grouped_rules.values() for rule in rules]
        migrated["allocation_rules"] = raw_rules
        by_clinic: dict[str, list[dict[str, Any]]] = {site_id: [] for site_id in site_ids}
        for rule in raw_rules:
            clinic_id = str(rule.get("clinic_id"))
            if clinic_id in by_clinic:
                by_clinic[clinic_id].append(dict(rule))
        for site in sites:
            if isinstance(site, dict):
                site["allocation_rules"] = by_clinic.get(str(site.get("id")), [])

        top_level = migrated.get("closure_days")
        if isinstance(top_level, list):
            by_site: dict[str, list[dict[str, Any]]] = {
                site_id: [] for site_id in site_ids
            }
            for closure_value in top_level:
                closure = (
                    closure_value.model_dump(mode="json")
                    if isinstance(closure_value, ClinicClosureDay)
                    else closure_value
                )
                if not isinstance(closure, dict):
                    continue
                for site_id in closure.get("sites") or []:
                    normalized = normalize_clinic_site_ids([site_id])[0]
                    if normalized in by_site:
                        by_site[normalized].append(
                            {
                                "date": closure.get("date"),
                                "name": closure.get("name", ""),
                            }
                        )
            for site in sites:
                if isinstance(site, dict):
                    site["closure_days"] = by_site.get(str(site.get("id")), [])
        else:
            grouped: dict[str, dict[str, Any]] = {}
            for site in sites:
                if not isinstance(site, dict):
                    continue
                for closure in site.get("closure_days") or []:
                    if not isinstance(closure, dict) or closure.get("date") is None:
                        continue
                    item = grouped.setdefault(
                        str(closure["date"]),
                        {"date": closure["date"], "sites": [], "name": ""},
                    )
                    item["sites"].append(str(site.get("id")))
                    if not item["name"] and closure.get("name"):
                        item["name"] = closure["name"]
            migrated["closure_days"] = list(grouped.values())
        return migrated

    @model_validator(mode="after")
    def validate_policy(self) -> ClinicPolicy:
        if self.academic.weekday is None or self.academic.session is None:
            raise ValueError("academic half day must select a day and session")
        ids = [site.id for site in self.sites]
        if len(ids) != len(set(ids)):
            raise ValueError("clinic site IDs must be unique")
        names = [site.name.casefold() for site in self.sites]
        if len(names) != len(set(names)):
            raise ValueError("clinic site names must be unique (case-insensitive)")
        known = set(ids)
        if self.primary_site_id not in known:
            raise ValueError("primary_site_id must reference a configured clinic site")
        allocation_groups: dict[
            tuple[str, int | str | None],
            list[ClinicAllocationRule],
        ] = {}
        for rule in self.allocation_rules:
            allocation_groups.setdefault(rule.scope_key, []).append(rule)
        if ("overall", None) not in allocation_groups:
            raise ValueError("clinic allocation rules require an overall rule")
        for scope, rules in allocation_groups.items():
            allocation_ids = [rule.clinic_id for rule in rules]
            if len(allocation_ids) != len(set(allocation_ids)):
                raise ValueError(
                    f"clinic allocation {scope[0]} override must reference each clinic once"
                )
            unknown_allocations = set(allocation_ids) - known
            if unknown_allocations:
                raise ValueError(
                    "clinic allocation references unknown clinic(s): "
                    + ", ".join(sorted(unknown_allocations))
                )
            missing_allocations = known - set(allocation_ids)
            if scope == ("overall", None) and missing_allocations:
                raise ValueError(
                    f"clinic allocation {scope[0]} override is missing clinic(s): "
                    + ", ".join(sorted(missing_allocations))
                )
        for closure in self.closure_days:
            unknown = set(closure.sites) - known
            if unknown:
                raise ValueError(
                    "clinic closure references unknown clinic site(s): "
                    + ", ".join(sorted(unknown))
                )
        return self

    @property
    def site_ids(self) -> tuple[str, ...]:
        return tuple(site.id for site in self.sites)

    @cached_property
    def _site_by_id(self) -> dict[str, ClinicSiteConfig]:
        return {site.id: site for site in self.sites}

    def site(self, site_id: str) -> ClinicSiteConfig:
        site = self._site_by_id.get(site_id)
        if site is not None:
            return site
        normalized = normalize_clinic_site_ids([site_id])[0]
        try:
            return self._site_by_id[normalized]
        except KeyError:
            raise KeyError(site_id) from None

    def site_name(self, site_id: str) -> str:
        return self.site(site_id).name

    def allocation_rules_for(
        self,
        *,
        pgy: int | None = None,
        resident_id: str | None = None,
    ) -> list[ClinicAllocationRule]:
        """Resolve resident, then training level, then overall allocation rules."""
        resolved: list[ClinicAllocationRule] = []
        for clinic_id in self.site_ids:
            candidates = [rule for rule in self.allocation_rules if rule.clinic_id == clinic_id]
            selected = None
            if resident_id is not None:
                selected = next(
                    (rule for rule in candidates if rule.resident_id == resident_id),
                    None,
                )
            if selected is None and pgy is not None:
                selected = next(
                    (rule for rule in candidates if rule.pgy == pgy),
                    None,
                )
            if selected is None:
                selected = next(
                    (rule for rule in candidates if rule.scope_key == ("overall", None)),
                    None,
                )
            if selected is not None:
                resolved.append(selected)
        return resolved

    def allocation(
        self,
        site_id: str,
        *,
        pgy: int | None = None,
        resident_id: str | None = None,
    ) -> ClinicAllocationRule:
        normalized = normalize_clinic_site_ids([site_id])[0]
        for rule in self.allocation_rules_for(pgy=pgy, resident_id=resident_id):
            if rule.clinic_id == normalized:
                return rule
        raise KeyError(site_id)

    def resolve_site_ids(self, site_ids: list[str]) -> list[str]:
        normalized = normalize_clinic_site_ids(site_ids)
        if normalized == [ALL_CLINIC_SITES]:
            return list(self.site_ids)
        return normalized

    def closure_on(self, calendar_day: date) -> ClinicClosureDay | None:
        return next(
            (closure for closure in self.closure_days if closure.date == calendar_day),
            None,
        )

    def closed_site_ids(self, calendar_day: date) -> tuple[str, ...]:
        closure = self.closure_on(calendar_day)
        return tuple(closure.sites) if closure is not None else ()

    def is_site_closed(self, site_id: str, calendar_day: date) -> bool:
        return self.site(site_id).is_closed(calendar_day)

    def open_site_ids(self, calendar_day: date, site_ids: list[str]) -> list[str]:
        resolved = self.resolve_site_ids(site_ids)
        closed = set(self.closed_site_ids(calendar_day))
        return [site_id for site_id in resolved if site_id not in closed]

    def is_academic(self, slot: ClinicSlot) -> bool:
        return slot.weekday is self.academic.weekday and slot.session is self.academic.session

    def max_capacity(
        self,
        site_id: str,
        weekday: Weekday,
        session: Session,
    ) -> int:
        return self.site(site_id).max_capacity(weekday, session)

    def max_capacity_on(
        self,
        site_id: str,
        calendar_day: date,
        session: Session,
    ) -> int:
        return self.site(site_id).max_capacity_on(calendar_day, session)

    def min_capacity(
        self,
        site_id: str,
        weekday: Weekday,
        session: Session,
    ) -> int:
        half_day = self.site(site_id).half_day(weekday, session)
        return half_day.min_residents if half_day is not None else 0

    def min_capacity_on(
        self,
        site_id: str,
        calendar_day: date,
        session: Session,
    ) -> int:
        return self.site(site_id).min_capacity_on(calendar_day, session)

    def available_site_ids(
        self,
        weekday: Weekday,
        session: Session,
    ) -> list[str]:
        return [site.id for site in self.sites if site.max_capacity(weekday, session) > 0]

    def attendings_needed(self, resident_count: int, site_id: str | None = None) -> int:
        """Attendings required for this many residents at one site in a half-day."""
        if resident_count <= 0:
            return 0
        ratio = self.site(site_id or self.primary_site_id).residents_per_attending
        return (resident_count + ratio - 1) // ratio
