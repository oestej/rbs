from rbs.catalog import sample_instance
from rbs.models.instance import SchedulerInput
from rbs.models.resident import Resident
from rbs.store import Store
from rbs.training_levels import add_training_level, reorder_training_levels
from rbs.ui.clinic.tab import _open_clinic_block_rules_dialog
from rbs.ui.grid import render_grid_html
from rbs.ui.rotations.editor import (
    _open_fmed_pgy_rules_dialog,
    _rotation_summary_html,
    render_rotations_tab,
)
from rbs.ui.rotations.table import rotation_rows
from rbs.ui.settings.training_levels import _open_add_dialog
from rbs.ui.settings.view import _settings_tab


def _with_sports_medicine_fellow(*, resident: bool = False) -> SchedulerInput:
    instance = add_training_level(
        sample_instance(),
        code="SMF",
        label="Sports Medicine Fellow",
    )
    if not resident:
        return instance
    raw = instance.model_dump(mode="json")
    raw["residents"].append(
        Resident(id="fellow-001", name="Finley Fellow", pgy=4).model_dump(mode="json")
    )
    return SchedulerInput.model_validate(raw)


def _created_elements(ui, before: set[int]):
    return [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]


def test_settings_aligns_reorderable_five_character_codes_and_full_names(tmp_path) -> None:
    from nicegui import ui

    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("Tracks", _with_sports_medicine_fellow())
    before = set(ui.context.client.elements)

    _settings_tab(
        store,
        workspace,
        {},
        persist_instance=lambda *_args, **_kwargs: None,
        redraw=lambda: None,
        active_section="settings_training_levels",
    )

    created = _created_elements(ui, before)
    short_codes = [
        element
        for element in created
        if element.__class__.__name__ == "Input"
        and element._props.get("label") == "Short code"
    ]
    full_names = [
        element.value
        for element in created
        if element.__class__.__name__ == "Input"
        and element._props.get("label") == "Full name"
    ]
    buttons = {
        element._props.get("label")
        for element in created
        if element.__class__.__name__ == "Button"
    }
    cards = [
        element
        for element in created
        if "rbs-training-level-card" in getattr(element, "_classes", [])
    ]
    editor_grids = [
        element
        for element in created
        if "rbs-training-level-editor-grid" in getattr(element, "_classes", [])
    ]
    drag_handles = [
        element
        for element in created
        if "rbs-training-level-drag-handle" in getattr(element, "_classes", [])
    ]
    delete_labels = {
        element._props.get("aria-label")
        for element in created
        if element.__class__.__name__ == "Button"
        and element._props.get("icon") == "delete_outline"
    }

    assert [element.value for element in short_codes] == ["PGY1", "PGY2", "PGY3", "SMF"]
    assert all(int(element._props["maxlength"]) == 5 for element in short_codes)
    assert all("counter" not in element._props for element in short_codes)
    assert "Sports Medicine Fellow" in full_names
    assert "Add training level" in buttons
    assert [int(card._props["data-training-level"]) for card in cards] == [1, 2, 3, 4]
    assert len(editor_grids) == 4
    assert len(drag_handles) == 4
    assert all(handle._props["draggable"] == "true" for handle in drag_handles)
    assert {"Delete PGY 1", "Delete PGY 2", "Delete PGY 3"} <= delete_labels
    assert "Delete Sports Medicine Fellow" in delete_labels


def test_add_dialog_explains_that_new_levels_start_empty() -> None:
    from nicegui import ui

    before = set(ui.context.client.elements)
    _open_add_dialog(
        sample_instance(),
        lambda *_args, **_kwargs: None,
        schedule_is_current=False,
    )

    created = _created_elements(ui, before)
    labels = {getattr(element, "_text", None) for element in created}
    select_labels = {
        element._props.get("label")
        for element in created
        if element.__class__.__name__ == "Select"
    }
    assert any(
        isinstance(label, str) and "starts with no curriculum or inherited rules" in label
        for label in labels
    )
    assert "Copy curriculum and rules from" not in select_labels


def test_fellowship_code_drives_compact_schedules_and_descriptive_editors() -> None:
    from nicegui import ui

    instance = _with_sports_medicine_fellow(resident=True)

    block_grid = render_grid_html(instance, None)
    rotation_summary = _rotation_summary_html(instance)
    assert "<span>SMF</span>" in block_grid
    assert "<span>SMF</span>" in rotation_summary
    assert "Sports Medicine Fellow" not in block_grid
    assert "Sports Medicine Fellow" not in rotation_summary
    before = set(ui.context.client.elements)
    _open_fmed_pgy_rules_dialog(
        instance,
        "fmed",
        selected_rotation_id=None,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = _created_elements(ui, before)
    text = {getattr(element, "_text", None) for element in created}
    labels = {element._props.get("label") for element in created}
    assert "Available to Sports Medicine Fellow" in text
    assert "Maximum Sports Medicine Fellow residents in clinic at one time" in labels

    before = set(ui.context.client.elements)
    _open_clinic_block_rules_dialog(
        instance,
        "clinic",
        on_save=lambda _instance, _rotation_id: None,
    )
    created = _created_elements(ui, before)
    tabs = {
        element._props.get("label")
        for element in created
        if element.__class__.__name__ == "Tab"
    }
    text = {getattr(element, "_text", None) for element in created}
    assert "SMF" in tabs
    assert "Clinic required for Sports Medicine Fellow" in text


def test_rotation_details_use_configured_full_training_level_names() -> None:
    from nicegui import ui

    raw = sample_instance().model_dump(mode="json")
    raw["requirements"][0]["code"] = "R1"
    raw["requirements"][0]["label"] = "First-year resident"
    instance = SchedulerInput.model_validate(raw)
    before = set(ui.context.client.elements)

    render_rotations_tab(
        instance,
        selected_rotation_id="icu",
        on_select=lambda _rotation_id: None,
        on_save=lambda _instance, _rotation_id: None,
        active_section="standard_rotations",
    )

    created = _created_elements(ui, before)
    text = {getattr(element, "_text", None) for element in created}
    assert "First-year resident" in text
    assert any(
        isinstance(label, str) and label.startswith("First-year resident ·")
        for label in text
    )


def test_training_level_order_drives_schedule_groups_and_rotation_summaries() -> None:
    raw = sample_instance().model_dump(mode="json")
    fmed = next(rotation for rotation in raw["rotations"] if rotation["id"] == "fmed")
    fmed["clinic"]["max_concurrent_by_pgy"] = {"1": 1, "2": 2}
    instance = reorder_training_levels(
        SchedulerInput.model_validate(raw),
        (2, 1, 3),
    )

    block_grid = render_grid_html(instance, None)
    rotation_summary = _rotation_summary_html(instance)
    fmed_row = next(row for row in rotation_rows(instance) if row["id"] == "fmed")

    assert block_grid.index("<span>PGY2</span>") < block_grid.index("<span>PGY1</span>")
    assert rotation_summary.index("<span>PGY2</span>") < rotation_summary.index(
        "<span>PGY1</span>"
    )
    assert fmed_row["duration"].index("PGY 2") < fmed_row["duration"].index("PGY 1")
    assert fmed_row["clinic"].index("PGY 2") < fmed_row["clinic"].index("PGY 1")
