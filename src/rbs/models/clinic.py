"""Clinic directory, allocation, capacity, and overlay-slot models.

The models live in focused submodules; this module re-exports the public
surface so existing imports keep working:

- ``rbs.models.clinic_site`` — sites, capacity, closures, colors, site IDs
- ``rbs.models.clinic_rules`` — slots, rules, allocation, closure days
- ``rbs.models.clinic_policy`` — the ``ClinicPolicy`` composition root
"""

from rbs.models.clinic_policy import ClinicPolicy
from rbs.models.clinic_rules import (
    ClinicAllocationRule,
    ClinicClosureDay,
    ClinicRule,
    ClinicSlot,
    clinic_slot_date,
)
from rbs.models.clinic_site import (
    ALL_CLINIC_SITES,
    ClinicCapacityOverride,
    ClinicHalfDayCapacity,
    ClinicSiteClosure,
    ClinicSiteConfig,
    lighten_hex_color,
    normalize_clinic_site_ids,
)

__all__ = [
    "ALL_CLINIC_SITES",
    "ClinicAllocationRule",
    "ClinicCapacityOverride",
    "ClinicClosureDay",
    "ClinicHalfDayCapacity",
    "ClinicPolicy",
    "ClinicRule",
    "ClinicSiteClosure",
    "ClinicSiteConfig",
    "ClinicSlot",
    "clinic_slot_date",
    "lighten_hex_color",
    "normalize_clinic_site_ids",
]
