from datetime import date

import pytest
from pydantic import ValidationError

from rbs.academic_year import academic_year_choices, rebase_academic_year
from rbs.catalog import (
    blank_instance,
    bootstrap_catalog,
    bundled_catalog,
    current_sample_instance,
    default_clinic_policy,
    default_requirements,
    default_rotations,
    monday_of_week_containing,
    sample_instance,
)
from rbs.models.catalog import ConstraintCatalog
from rbs.models.color_scheme import DEFAULT_COLOR_SCHEME
from rbs.models.enums import RotationKind, Session, Weekday
from rbs.models.instance import SchedulerInput
from rbs.models.rotation import (
    ALL_CLINIC_SITES,
    ROTATION_CODE_MAX_LENGTH,
    ROTATION_COLOR_PALETTE,
    ClinicAllocationRule,
    ClinicCapacityOverride,
    ClinicHalfDayCapacity,
    ClinicPolicy,
    ClinicSiteClosure,
    ClinicSiteConfig,
    ClinicSlot,
    Rotation,
)


def test_training_levels_cover_52_weeks() -> None:
    requirements = {item.pgy: item for item in default_requirements()}
    assert requirements[1].required_weeks() == 52
    assert requirements[2].required_weeks() == 52
    assert requirements[3].required_weeks() == 52


def test_sample_cohort_is_8_8_8() -> None:
    instance = sample_instance()
    assert instance.cohort_counts() == {1: 8, 2: 8, 3: 8}


def test_sample_residents_have_four_vacation_weeks() -> None:
    instance = sample_instance()
    for resident in instance.residents:
        assert len(resident.vacation_weeks) == 4, resident.id


def test_rotation_ids_are_unique() -> None:
    ids = [rotation.id for rotation in default_rotations()]
    codes = [rotation.code for rotation in default_rotations()]
    assert len(ids) == len(set(ids))
    assert len(codes) == len({code.casefold() for code in codes})
    assert all(len(code) <= ROTATION_CODE_MAX_LENGTH for code in codes)
    assert all(code == code.upper() for code in codes)


def test_rotation_code_is_trimmed_and_normalized_to_uppercase() -> None:
    raw = default_rotations()[0].model_dump(mode="json")
    raw["code"] = "  peds c  "

    assert Rotation.model_validate(raw).code == "PEDS C"


def test_rotation_code_counts_spaces_toward_six_character_limit() -> None:
    raw = default_rotations()[0].model_dump(mode="json")
    raw["code"] = "ABC DE"
    assert Rotation.model_validate(raw).code == "ABC DE"

    raw["code"] = "ABC  DE"
    with pytest.raises(ValidationError, match="at most 6 characters"):
        Rotation.model_validate(raw)


def test_rotations_store_a_normalized_schedule_color_from_the_palette() -> None:
    rotations = default_rotations()
    assert all(rotation.color in ROTATION_COLOR_PALETTE for rotation in rotations)
    assert len({rotation.color for rotation in rotations}) > 8

    raw = rotations[0].model_dump(mode="json")
    raw["color"] = " #2b6f8a "
    assert Rotation.model_validate(raw).color == "#2B6F8A"

    raw["color"] = "#123ABC"
    assert Rotation.model_validate(raw).color == "#123ABC"


def test_bundled_and_blank_catalog_colors_use_the_generated_default_palette() -> None:
    catalog = bundled_catalog()
    palette = set(DEFAULT_COLOR_SCHEME.palette)
    assigned = {
        catalog.electives.color,
        *(rotation.color for rotation in catalog.rotations),
        *(site.color for site in catalog.clinic_policy.sites),
    }

    assert assigned <= palette
    assert blank_instance().clinic_policy.sites[0].color == DEFAULT_COLOR_SCHEME.accents[0].color


def test_rotations_without_a_color_receive_the_neutral_default() -> None:
    from rbs.models.rotation import DEFAULT_ROTATION_COLOR

    raw = default_rotations()[0].model_dump(mode="json")
    raw.pop("color")

    assert Rotation.model_validate(raw).color == DEFAULT_ROTATION_COLOR


def test_bundled_json_matches_hardcoded_bootstrap() -> None:
    assert bundled_catalog().model_dump(mode="json") == bootstrap_catalog().model_dump(mode="json")


def test_catalog_schema_rejects_curriculum_choice_groups() -> None:
    raw = bootstrap_catalog().model_dump(mode="json")
    raw["requirements"][0]["choices"] = []

    with pytest.raises(ValidationError, match="choices"):
        ConstraintCatalog.model_validate(raw)


def test_pre_v5_catalogs_are_rejected() -> None:
    raw = bootstrap_catalog().model_dump(mode="json")
    raw["schema_version"] = 3

    with pytest.raises(ValidationError, match="Input should be 5"):
        ConstraintCatalog.model_validate(raw)

    raw["schema_version"] = 4
    with pytest.raises(ValidationError, match="Input should be 5"):
        ConstraintCatalog.model_validate(raw)


def test_instance_catalog_projection_preserves_explicit_elective_policy() -> None:
    from rbs.ui.rotations.ops import set_elective_eligibility

    instance = set_elective_eligibility(
        sample_instance(),
        "night_float",
        eligible=True,
        eligible_pgys=[2],
        repeatable=False,
    )

    catalog = instance.constraint_catalog()
    option = catalog.electives.option_for("night_float")

    assert catalog.schema_version == 5
    assert option is not None
    assert option.eligible_pgys == [2]
    assert not option.repeatable


def test_legacy_rotation_shape_is_rejected() -> None:
    with pytest.raises(ValidationError, match="code|pgy_rules|duration_weeks"):
        Rotation.model_validate(
            {
                "id": "legacy",
                "name": "Legacy",
                "duration_weeks": 1,
                "vacation": {"allowed": True},
            }
        )


def test_dedicated_rotations_are_kinds_not_id_special_cases() -> None:
    rotations = {rotation.id: rotation for rotation in default_rotations()}
    assert rotations["fmed"].kind is RotationKind.FMED
    assert rotations["clinic"].kind is RotationKind.CLINIC
    assert rotations["elective"].kind is RotationKind.ELECTIVE
    assert rotations["fmed"].residency_managed
    assert rotations["clinic"].residency_managed
    assert not rotations["elective"].residency_managed
    assert rotations["fmed"].requires_dedicated_configuration
    assert rotations["clinic"].requires_dedicated_configuration
    assert rotations["elective"].requires_dedicated_configuration
    assert rotations["icu"].kind is RotationKind.STANDARD
    assert not rotations["icu"].residency_managed
    assert not rotations["icu"].requires_dedicated_configuration


def test_fmed_and_clinic_allow_2_and_4_week_blocks() -> None:
    rotations = {rotation.id: rotation for rotation in default_rotations()}
    assert not rotations["fmed"].allows_duration(2, pgy=1)
    assert rotations["fmed"].allows_duration(4, pgy=1)
    assert rotations["fmed"].allows_duration(2, pgy=2)
    assert rotations["fmed"].allows_duration(4, pgy=2)
    assert rotations["clinic"].allows_duration(2, pgy=1)
    assert not rotations["clinic"].allows_duration(4, pgy=1)
    assert rotations["clinic"].allows_duration(2, pgy=2)
    assert rotations["clinic"].allows_duration(4, pgy=2)


def test_clinic_slots_expand_weekdays() -> None:
    rotations = {rotation.id: rotation for rotation in default_rotations()}
    fmed = rotations["fmed"].clinic
    assert fmed is not None
    assert fmed.unique_among_concurrent
    assert len(fmed.expanded_slots()) == 4
    assert all(slot.weekday is not Weekday.WEDNESDAY for slot in fmed.expanded_slots())
    clinic = rotations["clinic"].clinic
    assert clinic is not None
    assert clinic.admin_half_days_per_week == 1
    assert not clinic.no_academic_day_attendance
    em = rotations["emergency_medicine"].clinic
    assert em is not None
    assert len(em.expanded_slots()) == 10
    population_health = rotations["population_health_qi"].clinic
    assert population_health is not None
    assert [(slot.weekday, slot.session) for slot in population_health.slots if slot.preferred] == [
        (Weekday.TUESDAY, Session.MORNING)
    ]
    assert all(
        slot.sites
        for rotation in rotations.values()
        if rotation.clinic
        for slot in rotation.clinic.slots
    )


def test_inpatient_clinic_concurrency_supports_overall_and_pgy_limits() -> None:
    raw = next(
        rotation.model_dump(mode="json")
        for rotation in default_rotations()
        if rotation.id == "fmed"
    )
    raw["clinic"]["max_concurrent"] = 2
    raw["clinic"]["max_concurrent_by_pgy"] = {"1": 1, "2": 2}

    clinic = Rotation.model_validate(raw).clinic

    assert clinic is not None
    assert clinic.max_concurrent == 2
    assert clinic.max_concurrent_for_pgy(1) == 1
    assert clinic.max_concurrent_for_pgy(2) == 2
    assert clinic.max_concurrent_for_pgy(3) is None
    assert not clinic.unique_among_concurrent


def test_legacy_unique_inpatient_clinic_rule_migrates_to_a_cap_of_one() -> None:
    raw = next(
        rotation.model_dump(mode="json")
        for rotation in default_rotations()
        if rotation.id == "fmed"
    )
    raw["clinic"].pop("max_concurrent")
    raw["clinic"].pop("max_concurrent_by_pgy")
    raw["clinic"]["unique_among_concurrent"] = True

    clinic = Rotation.model_validate(raw).clinic

    assert clinic is not None
    assert clinic.max_concurrent == 1
    assert clinic.max_concurrent_by_pgy == {}
    assert "unique_among_concurrent" not in clinic.model_dump(mode="json")


def test_inpatient_clinic_concurrency_rejects_invalid_pgy_limits() -> None:
    raw = next(
        rotation.model_dump(mode="json")
        for rotation in default_rotations()
        if rotation.id == "fmed"
    )
    raw["clinic"]["max_concurrent_by_pgy"] = {"4": 1}
    assert Rotation.model_validate(raw).clinic.max_concurrent_by_pgy == {4: 1}

    raw["clinic"]["max_concurrent_by_pgy"] = {"0": 1}
    with pytest.raises(ValidationError, match="training-level key must be positive"):
        Rotation.model_validate(raw)

    raw["clinic"]["max_concurrent_by_pgy"] = {"4": 0}
    with pytest.raises(ValidationError, match="maximum must be at least 1"):
        Rotation.model_validate(raw)


def test_inpatient_peds_metro_requires_its_two_pinned_clinic_slots() -> None:
    metro = next(
        rotation for rotation in default_rotations() if rotation.id == "inpatient_peds_metro"
    )

    assert not metro.clinic_hours_disabled
    assert metro.clinic is not None
    assert metro.clinic.half_days_per_week == len(metro.clinic.slots) == 2
    assert {(slot.weekday, slot.session, tuple(slot.sites)) for slot in metro.clinic.slots} == {
        (Weekday.FRIDAY, Session.MORNING, ("maple",)),
        (Weekday.FRIDAY, Session.AFTERNOON, ("cedar",)),
    }


def test_pgy1_icu_placement_rules_live_on_the_rotation() -> None:
    rotations = {rotation.id: rotation for rotation in default_rotations()}
    icu = rotations["icu"].pgy_rule(1)
    em = rotations["emergency_medicine"].pgy_rule(1)
    assert icu.earliest_start_week == 5
    assert icu.prerequisite_rotation_ids == ["fmed", "emergency_medicine"]
    assert em.prerequisite_rotation_ids == ["fmed"]


def test_other_pgy1_placement_rules_are_rotation_configuration() -> None:
    rotations = {rotation.id: rotation for rotation in default_rotations()}
    assert rotations["night_float"].pgy_rule(1).prerequisite_rotation_ids == ["fmed"]
    assert rotations["peds_community"].pgy_rule(1).prerequisite_rotation_ids == ["fmed"]
    assert rotations["outpatient_gyn"].pgy_rule(1).earliest_start_week == 21
    assert rotations["inpatient_ld"].pgy_rule(1).earliest_start_week == 21


def test_rotation_dump_has_no_legacy_note_fields() -> None:
    for rotation in default_rotations():
        raw = rotation.model_dump(mode="json")
        assert "notes" not in raw
        assert "notes" not in raw["capacity"]
        assert all("notes" not in rule for rule in raw["pgy_rules"])
        assert all(
            "notes" not in config for rule in raw["pgy_rules"] for config in rule["block_configs"]
        )
        if raw["clinic"] is not None:
            assert "notes" not in raw["clinic"]
        assert "phases" not in raw
        assert "pairing" not in raw


def test_direct_import_rejects_legacy_rotation_note() -> None:
    raw = bootstrap_catalog().model_dump(mode="json")
    raw["rotations"][0]["notes"] = "Retired free-text rule"

    with pytest.raises(ValidationError, match="notes"):
        ConstraintCatalog.model_validate(raw)


def test_direct_import_rejects_prerelease_nested_notes() -> None:
    raw = bootstrap_catalog().model_dump(mode="json")
    raw["rotations"][0]["capacity"]["notes"] = "Legacy nested note"
    with pytest.raises(ValidationError, match="capacity.notes"):
        ConstraintCatalog.model_validate(raw)


def test_rotation_prerequisites_cannot_form_a_cycle() -> None:
    raw = bootstrap_catalog().model_dump(mode="json")
    by_id = {rotation["id"]: rotation for rotation in raw["rotations"]}
    by_id["sports_med"]["pgy_rules"][0]["prerequisite_rotation_ids"] = ["surgery"]
    by_id["surgery"]["pgy_rules"][0]["prerequisite_rotation_ids"] = ["sports_med"]
    with pytest.raises(ValidationError, match="prerequisite cycle"):
        ConstraintCatalog.model_validate(raw)


def test_consecutive_caps_and_precept_policy() -> None:
    rotations = {rotation.id: rotation for rotation in default_rotations()}
    assert rotations["fmed"].max_consecutive_weeks == 4
    assert rotations["clinic"].max_consecutive_weeks == 6
    assert rotations["elective"].max_consecutive_weeks == 4
    assert all(1 <= rotation.max_consecutive_weeks <= 6 for rotation in rotations.values())
    assert not rotations["peds_community"].away
    assert not rotations["inpatient_peds_metro"].away
    assert rotations["peds_community"].clinic_hours_disabled
    assert not rotations["inpatient_peds_metro"].clinic_hours_disabled
    assert rotations["icu"].no_clinic_hours
    assert not rotations["sports_med"].no_clinic_hours
    assert not any(rotation.no_weekend_call for rotation in rotations.values())
    assert all(
        config.vacation.max_weeks_per_block == 1
        for rotation in rotations.values()
        for rule in rotation.pgy_rules
        for config in rule.block_configs
        if config.vacation.allowed
    )
    policy = default_clinic_policy()
    primary = policy.site(policy.primary_site_id)
    secondary_id = next(site_id for site_id in policy.site_ids if site_id != policy.primary_site_id)
    secondary = policy.site(secondary_id)
    assert primary.residents_per_attending == 4
    assert (
        max(
            half_day.max_residents(secondary.residents_per_attending)
            for half_day in secondary.half_days
        )
        == 4
    )
    assert policy.allocation(secondary_id).target_fraction == 0.25
    assert policy.attendings_needed(0) == 0
    assert policy.attendings_needed(1) == 1
    assert policy.attendings_needed(4) == 1
    assert policy.attendings_needed(5) == 2
    assert policy.attendings_needed(8) == 2
    assert policy.academic.weekday is Weekday.WEDNESDAY
    assert policy.max_capacity(secondary_id, Weekday.TUESDAY, Session.MORNING) > 0
    assert policy.max_capacity(secondary_id, Weekday.FRIDAY, Session.MORNING) > 0
    assert policy.max_capacity(secondary_id, Weekday.FRIDAY, Session.AFTERNOON) == 0
    assert policy.max_capacity(secondary_id, Weekday.MONDAY, Session.MORNING) == 0
    assert policy.academic.session is Session.AFTERNOON
    assert len(secondary.half_days) == 5
    assert policy.site_ids == ("maple", "cedar")
    assert [(site.id, site.name, site.color) for site in policy.sites] == [
        ("maple", "Maple", "#6D6BC2"),
        ("cedar", "Cedar", "#174A7E"),
    ]
    clinic_colors = {site.color for site in policy.sites}
    assert len(clinic_colors) == len(policy.sites)
    assert rotations["fmed"].color not in clinic_colors
    assert policy.site("maple").light_color == "#F0F0F9"
    assert "light_color" not in policy.site("maple").model_dump(mode="json")
    assert all(site.id != "harbor" for site in policy.sites)
    assert len(policy.closure_days) == 1
    christmas = policy.closure_days[0]
    assert christmas.date == date(2026, 12, 25)
    assert christmas.name == "Christmas"
    assert set(christmas.sites) == {"maple", "cedar"}
    assert policy.closed_site_ids(christmas.date) == ("maple", "cedar")
    assert policy.open_site_ids(christmas.date, [ALL_CLINIC_SITES]) == []


def test_clinic_sites_can_be_extended_entirely_through_policy_data() -> None:
    raw = default_clinic_policy().model_dump(mode="json")
    raw["sites"].append(
        ClinicSiteConfig(
            id="future_site",
            name="Future Site",
            color="#28735C",
        ).model_dump(mode="json")
    )

    policy = ClinicPolicy.model_validate(raw)

    assert policy.resolve_site_ids([ALL_CLINIC_SITES]) == [
        "maple",
        "cedar",
        "future_site",
    ]
    assert policy.site("future_site").light_color == "#EAF1EF"


def test_single_clinic_supports_weekend_capacity_and_owns_its_closures() -> None:
    policy = ClinicPolicy(
        sites=[
            ClinicSiteConfig(
                id="weekend_clinic",
                name="Weekend Clinic",
                color="#28735C",
                residents_per_attending=3,
                half_days=[
                    ClinicHalfDayCapacity(
                        weekday=Weekday.SATURDAY,
                        session=Session.MORNING,
                        attendings=2,
                        min_residents=1,
                    )
                ],
                capacity_overrides=[
                    ClinicCapacityOverride(
                        date=date(2026, 7, 5),
                        session=Session.AFTERNOON,
                        attendings=3,
                        min_residents=2,
                    )
                ],
                closure_days=[
                    ClinicSiteClosure(
                        date=date(2026, 12, 26),
                        name="Winter holiday",
                    )
                ],
            )
        ],
        allocation_rules=[
            ClinicAllocationRule(
                clinic_id="weekend_clinic",
                min_fraction=1,
                target_fraction=1,
                max_fraction=1,
            )
        ],
        primary_site_id="weekend_clinic",
        academic=ClinicSlot(
            weekday=Weekday.MONDAY,
            session=Session.AFTERNOON,
        ),
    )

    assert (
        policy.max_capacity(
            "weekend_clinic",
            Weekday.SATURDAY,
            Session.MORNING,
        )
        == 6
    )
    assert (
        policy.min_capacity(
            "weekend_clinic",
            Weekday.SATURDAY,
            Session.MORNING,
        )
        == 1
    )
    assert (
        policy.max_capacity_on(
            "weekend_clinic",
            date(2026, 7, 5),
            Session.AFTERNOON,
        )
        == 9
    )
    assert (
        policy.min_capacity_on(
            "weekend_clinic",
            date(2026, 7, 5),
            Session.AFTERNOON,
        )
        == 2
    )
    assert (
        policy.max_capacity_on(
            "weekend_clinic",
            date(2026, 12, 26),
            Session.MORNING,
        )
        == 0
    )
    assert policy.is_site_closed("weekend_clinic", date(2026, 12, 26))
    assert policy.closure_days[0].sites == ["weekend_clinic"]


def test_legacy_clinic_site_code_is_discarded_on_load() -> None:
    raw = default_clinic_policy().site("maple").model_dump(mode="json")
    raw["code"] = "MPL"

    site = ClinicSiteConfig.model_validate(raw)

    assert site.id == "maple"
    assert site.name == "Maple"
    assert "code" not in site.model_dump(mode="json")


def test_clinic_capacity_overrides_require_unique_slots_and_valid_minimums() -> None:
    raw = default_clinic_policy().site("maple").model_dump(mode="json")
    raw["capacity_overrides"] = [
        {
            "date": "2026-07-07",
            "session": "morning",
            "attendings": 1,
            "min_residents": 0,
        },
        {
            "date": "2026-07-07",
            "session": "morning",
            "attendings": 2,
            "min_residents": 0,
        },
    ]
    with pytest.raises(ValidationError, match="unique dates and sessions"):
        ClinicSiteConfig.model_validate(raw)

    raw["capacity_overrides"] = [
        {
            "date": "2026-07-07",
            "session": "morning",
            "attendings": 0,
            "min_residents": 1,
        }
    ]
    with pytest.raises(ValidationError, match="minimum residents cannot exceed"):
        ClinicSiteConfig.model_validate(raw)

    instance_raw = sample_instance().model_dump(mode="json")
    maple = next(site for site in instance_raw["clinic_policy"]["sites"] if site["id"] == "maple")
    maple["capacity_overrides"] = [
        {
            "date": "2030-01-01",
            "session": "morning",
            "attendings": 1,
            "min_residents": 0,
        }
    ]
    with pytest.raises(ValidationError, match="outside academic year"):
        SchedulerInput.model_validate(instance_raw)


def test_clinic_allocation_uses_resident_then_pgy_then_overall_rule() -> None:
    raw = default_clinic_policy().model_dump(mode="json")
    raw["allocation_rules"].extend(
        [
            {
                "clinic_id": "maple",
                "pgy": 1,
                "target_fraction": 0.4,
            },
            {
                "clinic_id": "cedar",
                "pgy": 1,
                "target_fraction": 0.6,
            },
            {
                "clinic_id": "maple",
                "resident_id": "resident-001",
                "target_fraction": 0.1,
            },
            {
                "clinic_id": "cedar",
                "resident_id": "resident-001",
                "target_fraction": 0.9,
            },
        ]
    )
    policy = ClinicPolicy.model_validate(raw)

    assert policy.allocation("maple", pgy=2).target_fraction == 0.25
    assert policy.allocation("maple", pgy=1).target_fraction == 0.4
    assert (
        policy.allocation(
            "maple",
            pgy=1,
            resident_id="resident-001",
        ).target_fraction
        == 0.1
    )
    assert any(rule.pgy == 1 for rule in policy.site("maple").allocation_rules)
    assert any(rule.resident_id == "resident-001" for rule in policy.site("maple").allocation_rules)


def test_clinic_closures_require_known_specific_sites_and_unique_dates() -> None:
    raw = default_clinic_policy().model_dump(mode="json")
    raw["closure_days"] = [
        {"date": "2026-12-24", "name": "Winter closure", "sites": ["maple"]},
        {"date": "2026-12-24", "name": "Duplicate", "sites": ["cedar"]},
    ]
    with pytest.raises(ValidationError, match="closure dates must be unique"):
        ClinicPolicy.model_validate(raw)

    raw["closure_days"] = [{"date": "2026-12-24", "name": "Unknown", "sites": ["future_site"]}]
    with pytest.raises(ValidationError, match="unknown clinic site"):
        ClinicPolicy.model_validate(raw)

    raw["closure_days"] = [{"date": "2026-12-24", "name": "Wildcard", "sites": [ALL_CLINIC_SITES]}]
    with pytest.raises(ValidationError, match="specific configured clinic sites"):
        ClinicPolicy.model_validate(raw)


def test_away_rotation_always_disables_clinic_hours() -> None:
    raw = default_rotations()[0].model_dump(mode="json")
    raw["away"] = True
    raw["no_clinic_hours"] = False

    rotation = Rotation.model_validate(raw)

    assert rotation.away
    assert rotation.no_clinic_hours
    assert rotation.clinic is not None


def test_consecutive_week_limit_defaults_to_four_and_stays_between_one_and_six() -> None:
    raw = default_rotations()[0].model_dump(mode="json")
    raw.pop("max_consecutive_weeks")
    assert Rotation.model_validate(raw).max_consecutive_weeks == 4

    for invalid in (0, 7, None):
        raw["max_consecutive_weeks"] = invalid
        with pytest.raises(ValidationError, match="max_consecutive_weeks"):
            Rotation.model_validate(raw)


def test_total_week_limit_is_optional_and_stays_within_the_academic_year() -> None:
    raw = default_rotations()[0].model_dump(mode="json")
    raw.pop("max_total_weeks")
    assert Rotation.model_validate(raw).max_total_weeks is None

    raw["max_total_weeks"] = 12
    assert Rotation.model_validate(raw).max_total_weeks == 12

    for invalid in (0, 53):
        raw["max_total_weeks"] = invalid
        with pytest.raises(ValidationError, match="max_total_weeks"):
            Rotation.model_validate(raw)


def test_pgy_total_week_limit_is_optional_and_combines_with_the_rotation_limit() -> None:
    raw = default_rotations()[0].model_dump(mode="json")
    rule = raw["pgy_rules"][0]
    rule.pop("max_total_weeks")
    rotation = Rotation.model_validate(raw)
    assert rotation.pgy_rule(rule["pgy"]).max_total_weeks is None

    raw["max_total_weeks"] = 12
    rule["max_total_weeks"] = 8
    rotation = Rotation.model_validate(raw)
    assert rotation.max_total_weeks_for_pgy(rule["pgy"]) == 8

    raw["max_total_weeks"] = 6
    rotation = Rotation.model_validate(raw)
    assert rotation.max_total_weeks_for_pgy(rule["pgy"]) == 6

    for invalid in (0, 53):
        rule["max_total_weeks"] = invalid
        with pytest.raises(ValidationError, match="max_total_weeks"):
            Rotation.model_validate(raw)


def test_academic_year_starts_monday_of_july_1_week() -> None:
    instance = sample_instance()
    assert instance.calendar.first_week_start == date(2026, 6, 29)
    assert instance.calendar.first_week_start == monday_of_week_containing(date(2026, 7, 1))
    assert instance.calendar.block_start_alignment == 1


def test_academic_year_can_be_selected_without_a_fixed_runtime_year() -> None:
    instance = sample_instance(academic_year="2027-2028")

    assert instance.academic_year == "2027-2028"
    assert instance.calendar.first_week_start == date(2027, 6, 28)
    assert {closure.date for closure in instance.clinic_policy.closure_days} == {date(2027, 12, 25)}
    assert "2032-2033" in academic_year_choices(
        instance.academic_year,
        today=date(2032, 8, 1),
    )
    current_choices = academic_year_choices("2032-2033", today=date(2032, 8, 1))
    assert current_choices[0] == "2029-2030"
    assert "2028-2029" not in current_choices
    assert current_sample_instance(today=date(2032, 8, 1)).academic_year == "2032-2033"


def test_rebasing_academic_year_moves_workspace_specific_dates() -> None:
    instance = sample_instance()
    resident = instance.residents[0].model_copy(update={"days_off": [date(2026, 9, 15)]})
    maple = instance.clinic_policy.site("maple")
    shifted_maple = maple.model_copy(
        update={
            "capacity_overrides": [
                ClinicCapacityOverride(
                    date=date(2026, 9, 16),
                    session=Session.MORNING,
                    attendings=2,
                )
            ]
        }
    )
    policy = instance.clinic_policy.model_copy(
        update={
            "sites": [
                shifted_maple if site.id == shifted_maple.id else site
                for site in instance.clinic_policy.sites
            ]
        }
    )
    configured = instance.revised(
        residents=[resident, *instance.residents[1:]],
        clinic_policy=policy,
        locks=[
            *instance.locks,
            instance.locks[0].model_copy(update={"source": "through_today"}),
        ],
    )

    rebased = rebase_academic_year(configured, "2028-2029")

    assert rebased.residents[0].days_off == [date(2028, 9, 15)]
    assert rebased.clinic_policy.site("maple").capacity_overrides[0].date == date(2028, 9, 16)
    assert {closure.date for closure in rebased.clinic_policy.closure_days} == {date(2028, 12, 25)}
    assert all(lock.source == "manual" for lock in rebased.locks)
    assert len(rebased.locks) == len(instance.locks)


def test_gyn_and_ld_are_separate_grouped_rotations_with_level_specific_order() -> None:
    catalog = bundled_catalog()
    rotations = {rotation.id: rotation for rotation in catalog.rotations}

    assert "gyn_ob" not in rotations
    assert rotations["outpatient_gyn"].code == "GYN"
    assert rotations["inpatient_ld"].code == "L&D"
    assert rotations["outpatient_gyn"].configured_durations() == [2]
    assert rotations["inpatient_ld"].configured_durations() == [2]
    assert rotations["outpatient_gyn"].capacity.max_concurrent == 1
    assert rotations["inpatient_ld"].capacity.max_concurrent == 1
    assert all(
        rule.max_concurrent == 1
        for rotation_id in ("outpatient_gyn", "inpatient_ld")
        for rule in rotations[rotation_id].pgy_rules
    )
    assert rotations["inpatient_ld"].pgy_rule(1).prerequisite_rotation_ids == ["outpatient_gyn"]
    assert rotations["outpatient_gyn"].pgy_rule(2).prerequisite_rotation_ids == ["inpatient_ld"]
    assert [(group.pgy, group.rotation_ids) for group in catalog.rotation_groups] == [
        (1, ["outpatient_gyn", "inpatient_ld"]),
        (2, ["outpatient_gyn", "inpatient_ld"]),
    ]


def test_rotation_groups_require_equal_direct_occurrence_counts() -> None:
    raw = bundled_catalog().model_dump(mode="json")
    pgy1 = next(item for item in raw["requirements"] if item["pgy"] == 1)
    inpatient = next(item for item in pgy1["blocks"] if item["rotation_id"] == "inpatient_ld")
    inpatient["count"] = 2

    with pytest.raises(ValidationError, match="must have equal occurrence counts"):
        ConstraintCatalog.model_validate(raw)


def test_rotation_cannot_belong_to_two_groups_at_one_training_level() -> None:
    raw = bundled_catalog().model_dump(mode="json")
    raw["rotation_groups"].append(
        {
            "pgy": 1,
            "rotation_ids": ["outpatient_gyn", "icu"],
        }
    )

    with pytest.raises(ValidationError, match="belongs to more than one rotation group"):
        ConstraintCatalog.model_validate(raw)


def test_blank_instance_has_only_editable_workspace_scaffolding() -> None:
    instance = blank_instance(academic_year="2032-2033")

    assert instance.academic_year == "2032-2033"
    assert instance.residents == []
    assert [rotation.id for rotation in instance.rotations] == ["clinic", "fmed"]
    clinic = instance.rotation("clinic")
    assert clinic.kind is RotationKind.CLINIC
    assert clinic.code == "CLINIC"
    assert clinic.name == "Clinic"
    assert clinic.requires_dedicated_configuration
    assert [rule.pgy for rule in clinic.pgy_rules] == [1]
    assert clinic.allows_duration(2, pgy=1)
    assert clinic.clinic is not None
    assert clinic.clinic.half_days_per_week == 1
    assert clinic.clinic.admin_half_days_per_week == 1
    assert clinic.color == DEFAULT_COLOR_SCHEME.accents[4].color
    fmed = instance.rotation("fmed")
    assert fmed.kind is RotationKind.FMED
    assert fmed.code == "FMED"
    assert fmed.name == "Inpatient"
    assert fmed.requires_dedicated_configuration
    assert [rule.pgy for rule in fmed.pgy_rules] == [1]
    assert fmed.allows_duration(4, pgy=1)
    assert fmed.clinic is not None
    assert fmed.clinic.max_concurrent == 1
    assert fmed.clinic.half_days_per_week == 1
    assert all(
        slot.weekday is not instance.clinic_policy.academic.weekday
        for slot in fmed.clinic.expanded_slots()
    )
    assert fmed.color == DEFAULT_COLOR_SCHEME.secondary.color
    assert instance.rotation_groups == []
    assert instance.electives.rotation_options == []
    assert len(instance.requirements) == 1
    assert instance.requirements[0].blocks == []
    assert len(instance.clinic_policy.sites) == 1
    assert instance.clinic_policy.sites[0].half_days == []
