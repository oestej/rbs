"""Compatibility for attending eligibility before parsing legacy inputs."""

from copy import deepcopy


def migrate_eligibility(case: dict, clinic_policy: dict) -> dict:
    """Snapshot existing clinics for legacy attendings; preserve explicit empty lists.

    Leave malformed shapes to the domain validators rather than hiding invalid input.
    """
    result = deepcopy(case)
    if not isinstance(clinic_policy, dict):
        return result
    sites = clinic_policy.get("sites", [])
    attendings = result.get("attendings", [])
    if not isinstance(sites, list) or not isinstance(attendings, list):
        return result
    clinics = [
        site["id"]
        for site in sites
        if isinstance(site, dict)
        and "id" in site
        and site.get("staffing_mode") == "attending_managed"
    ]
    for attending in attendings:
        if isinstance(attending, dict):
            attending.setdefault("eligible_clinic_ids", list(clinics))
    return result
