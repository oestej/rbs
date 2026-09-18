import csv
from datetime import date
from io import StringIO

import pytest

from rbs.catalog import sample_instance
from rbs.models.instance import SchedulerInput
from rbs.ui.rotations.csv_export import (
    build_rotations_csv,
    rotation_csv_columns,
    rotations_csv_filename,
)
from rbs.ui.rotations.editor import render_rotations_tab
from rbs.ui.rotations.table import ROTATION_COLUMNS


def _exported_rows(instance) -> list[dict[str, str]]:
    return list(csv.DictReader(StringIO(build_rotations_csv(instance))))


def test_rotations_csv_covers_every_rotation_in_display_order() -> None:
    instance = sample_instance()

    columns = rotation_csv_columns()
    assert [field for field, _label in columns] == [
        *(column["field"] for column in ROTATION_COLUMNS),
        "max_total_weeks",
        "max_total_weeks_by_training_level",
    ]

    rows = _exported_rows(instance)
    assert len(rows) == len(instance.rotations)
    assert list(rows[0]) == [label for _field, label in columns]
    codes = [row["Code"] for row in rows]
    assert codes == sorted(codes, key=str.casefold)


def test_rotations_csv_surfaces_configurable_parameters() -> None:
    instance = sample_instance()

    rows = {row["Code"]: row for row in _exported_rows(instance)}
    night_float = rows["NF"]
    assert night_float["Kind"] == "standard"
    assert night_float["Away"] == "no"
    assert night_float["No clinic"] == "yes"
    assert night_float["Clinic"] == "none"
    assert night_float["Capacity"] == "max 1"
    assert night_float["Max total weeks"] == ""
    assert night_float["Max total weeks by training level"] == ""

    clinic = rows["CLINIC"]
    assert clinic["No clinic"] == "no"
    assert "1 half-day per week" in clinic["Clinic"]


def test_rotations_csv_reports_total_week_limits() -> None:
    instance = sample_instance()
    raw = instance.model_dump(mode="json")
    for entry in raw["rotations"]:
        if entry["code"] == "NF":
            entry["max_total_weeks"] = 8
            entry["pgy_rules"][0]["max_total_weeks"] = 4
    instance = SchedulerInput.model_validate(raw)

    rows = {row["Code"]: row for row in _exported_rows(instance)}
    assert rows["NF"]["Max total weeks"] == "8"
    assert "max 4 wk" in rows["NF"]["Max total weeks by training level"]


def test_rotations_csv_filename_identifies_the_academic_year() -> None:
    assert (
        rotations_csv_filename("2026-2027", exported_on=date(2026, 8, 23))
        == "rotations-2026-2027-exported-2026-08-23.csv"
    )


@pytest.mark.parametrize("value", ["=1+1", "+1+1", "-1+1", "@SUM(1,1)", "＝1+1"])
def test_rotations_csv_neutralizes_formula_names_and_training_level_labels(value: str) -> None:
    raw = sample_instance().model_dump(mode="json")
    rotation = next(entry for entry in raw["rotations"] if entry["id"] == "night_float")
    rotation["name"] = value
    raw["requirements"][0]["label"] = value
    instance = SchedulerInput.model_validate(raw)

    row = next(row for row in _exported_rows(instance) if row["Code"] == "NF")

    assert row["Name"] == "'" + value
    assert row["Weeks"].startswith("'" + value)
    assert instance.rotation("night_float").name == value
    assert instance.requirements[0].display_label == value


@pytest.mark.parametrize("value", ['Name,\"=1+1\"', "Name\r=1+1", "Name\n=1+1"])
def test_rotations_csv_keeps_separators_and_line_breaks_inside_one_cell(value: str) -> None:
    instance = sample_instance()
    instance = instance.revised(
        rotations=[
            rotation.revised(name=value) if rotation.id == "night_float" else rotation
            for rotation in instance.rotations
        ]
    )

    rows = list(csv.DictReader(StringIO(build_rotations_csv(instance), newline="")))

    assert len(rows) == len(instance.rotations)
    assert all(None not in row and None not in row.values() for row in rows)
    assert next(row for row in rows if row["Code"] == "NF")["Name"] == value


def test_rotations_csv_neutralizes_formula_rotation_codes() -> None:
    instance = sample_instance()
    instance = instance.revised(
        rotations=[
            rotation.revised(code="=1+1") if rotation.id == "night_float" else rotation
            for rotation in instance.rotations
        ]
    )

    row = next(row for row in _exported_rows(instance) if row["Name"] == "Night Float")

    assert row["Code"] == "'=1+1"
    assert instance.rotation("night_float").code == "=1+1"


def test_rotations_tab_exposes_export_csv_button() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)
    render_rotations_tab(
        instance,
        selected_rotation_id=None,
        on_select=lambda _rotation_id: None,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    button_labels = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Button"
    }
    assert "Export CSV" in button_labels
