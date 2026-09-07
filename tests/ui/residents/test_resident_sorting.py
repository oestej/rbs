"""Resident display ordering."""

from rbs.catalog import sample_instance
from rbs.models.resident import Resident, resident_display_sort_key
from rbs.ui.residents.tab import render_residents_tab


def test_resident_display_sort_key_uses_last_name_or_single_name() -> None:
    residents = [
        Resident(id="zoe", name="Zoe Anderson", pgy=1),
        Resident(id="chris", name="Chris Anderson", pgy=1),
        Resident(id="alex", name="Alex Baker", pgy=1),
        Resident(id="cher", name="Cher", pgy=1),
    ]

    assert [resident.id for resident in sorted(residents, key=resident_display_sort_key)] == [
        "chris",
        "zoe",
        "alex",
        "cher",
    ]


def test_resident_directory_orders_each_training_level_by_last_name() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)

    render_residents_tab(
        instance,
        selected_resident_id=None,
        on_select=lambda _resident_id: None,
        on_save=lambda _instance, _resident_id: None,
    )

    resident_names = {resident.name for resident in instance.residents}
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    displayed_names = [
        element._text
        for element in created
        if element.__class__.__name__ == "ItemLabel" and element._text in resident_names
    ]

    assert displayed_names == [
        "Quinn Brooks",
        "Avery Chen",
        "Harper Diaz",
        "Morgan Ellis",
        "Riley Nguyen",
        "Casey Okonkwo",
        "Jordan Patel",
        "Sam Rivera",
        "Jamie Alvarez",
        "Skyler Bennett",
        "Drew Hassan",
        "Taylor Kim",
        "Reese Nakamura",
        "Peyton Ortiz",
        "Alex Rahman",
        "Cameron Walsh",
        "Finley Adams",
        "Rowan Baker",
        "Sydney Cho",
        "Emerson Cole",
        "Robin Ford",
        "Devon Park",
        "Hayden Ross",
        "Charlie Singh",
    ]
