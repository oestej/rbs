from rbs.catalog import sample_instance
from rbs.ui.rotations.table import ROTATION_COLUMNS, rotation_rows


def test_rotation_rows_include_core_constraints() -> None:
    instance = sample_instance()
    rows = {row["id"]: row for row in rotation_rows(instance)}
    assert {col["name"] for col in ROTATION_COLUMNS} >= {
        "code",
        "color",
        "clinic",
        "curriculum",
        "grouping",
        "consecutive",
        "away",
        "no_clinic_hours",
        "no_weekend_call",
        "vacation",
        "placement",
        "capacity",
    }
    assert "notes" not in {col["name"] for col in ROTATION_COLUMNS}
    assert all("notes" not in row for row in rows.values())

    icu = rows["icu"]
    assert icu["color"] == instance.rotation("icu").color
    assert icu["clinic"] == "none"
    assert "max 1" in icu["capacity"]
    assert "PGY 1" in icu["placement"]
    assert "after FMED, EM" in icu["placement"]
    assert "from Block B/2" in icu["placement"]
    assert "PGY 1 4wk: yes, max 1 wk" in icu["vacation"]

    clinic = rows["clinic"]
    assert clinic["kind"] == "clinic"
    assert "admin half-day" in clinic["clinic"]
    assert "any M–F half-day" in clinic["clinic"]
    assert "Maple" not in clinic["clinic"]
    assert "max 6 wk" in clinic["consecutive"]

    fmed = rows["fmed"]
    assert "max 1 concurrent" in fmed["clinic"]
    assert "min 2" in fmed["capacity"]
    assert "max 4 wk" in fmed["consecutive"]

    night = rows["night_float"]
    assert night["clinic"] == "none"
    assert "after FMED" in night["placement"]
    assert "PGY 1 2wk: no" in night["vacation"]
    assert "PGY 2 2wk: no" in night["vacation"]

    gyn = rows["outpatient_gyn"]
    labor_and_delivery = rows["inpatient_ld"]
    assert "PGY 1: GYN + L&D" in gyn["grouping"]
    assert "PGY 2: GYN + L&D" in gyn["grouping"]
    assert labor_and_delivery["grouping"] == gyn["grouping"]
    assert "PGY 1 max 1" in gyn["capacity"]
    assert "from Block F/6" in gyn["placement"]
    assert "PGY 2 after L&D" in gyn["placement"]
    assert "PGY 1 after GYN" in labor_and_delivery["placement"]

    population_health = rows["population_health_qi"]
    assert "prefer Tue AM" in population_health["clinic"]

    peds = rows["peds_community"]
    assert peds["away"] == "no"
    assert peds["no_clinic_hours"] == "yes"

    metro = rows["inpatient_peds_metro"]
    assert "2 half-days per week" in metro["clinic"]
    assert "Fri AM Maple" in metro["clinic"]
    assert "Fri PM Cedar" in metro["clinic"]
    assert metro["no_clinic_hours"] == "no"
    assert metro["no_weekend_call"] == "no"


def test_rotation_rows_are_sorted_alphabetically_by_code() -> None:
    rows = rotation_rows(sample_instance())
    codes = [row["code"] for row in rows]
    assert codes == sorted(codes, key=str.casefold)
