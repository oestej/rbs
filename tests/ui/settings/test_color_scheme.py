import pytest
from pydantic import ValidationError

from rbs.catalog import sample_instance
from rbs.models.color_scheme import (
    ColorScheme,
    accessible_text_color,
    contrast_ratio,
    contrasting_text_color,
)
from rbs.models.instance import SchedulerInput
from rbs.ui.settings.color_scheme import replace_color_scheme


def test_default_color_scheme_matches_bundled_rbs_colors() -> None:
    scheme = ColorScheme()

    assert scheme.name == "RBS Navy & Gold"
    assert scheme.primary.name == "Navy"
    assert scheme.primary.color == "#174A7E"
    assert scheme.secondary.name == "Goldenrod"
    assert scheme.secondary.color == "#C58A17"
    assert scheme.neutral.name == "Slate"
    assert scheme.neutral.color == "#52606D"
    assert "#3971B8" in scheme.palette


def test_color_scheme_normalizes_hex_values_and_rejects_duplicates() -> None:
    raw = ColorScheme().model_dump(mode="json")
    raw["primary"]["color"] = " #123abc "
    assert ColorScheme.model_validate(raw).primary.color == "#123ABC"

    raw["secondary"]["color"] = "#123ABC"
    with pytest.raises(ValidationError, match="schedule palette colors must be unique"):
        ColorScheme.model_validate(raw)


def test_palette_colors_choose_a_readable_foreground() -> None:
    assert contrasting_text_color("#174A7E") == "#FFFFFF"
    assert contrasting_text_color("#C58A17") == "#262626"
    for background in ("#174A7E", "#C58A17", "#777777", "#FFFFFF", "#000000"):
        foreground = contrasting_text_color(background)
        assert contrast_ratio(foreground, background) >= 4.5


def test_theme_text_roles_fall_back_when_a_workspace_color_is_too_light() -> None:
    assert accessible_text_color("#174A7E") == "#174A7E"
    assert accessible_text_color("#C58A17") == "#262626"


def test_replacing_scheme_updates_assignments_by_palette_slot() -> None:
    instance = sample_instance()
    raw = instance.color_scheme.model_dump(mode="json")
    raw["primary"] = {"name": "Institution Blue", "color": "#123A67"}
    revised = replace_color_scheme(instance, ColorScheme.model_validate(raw))

    assert revised.color_scheme.primary.name == "Institution Blue"
    assert revised.color_scheme.primary.color == "#123A67"
    assert all(
        rotation.color == "#123A67"
        for rotation in revised.rotations
        if instance.rotation(rotation.id).color == "#174A7E"
    )
    assert revised.clinic_policy.site("cedar").color == "#123A67"
    assert revised.clinic_policy.site("maple").color == "#963C5A"


def test_replacing_scheme_preserves_colors_outside_the_previous_palette() -> None:
    instance = sample_instance()
    raw = instance.model_dump(mode="json")
    raw["clinic_policy"]["sites"][0]["color"] = "#123456"
    customized = SchedulerInput.model_validate(raw)
    scheme_raw = customized.color_scheme.model_dump(mode="json")
    scheme_raw["primary"]["color"] = "#654321"

    revised = replace_color_scheme(customized, ColorScheme.model_validate(scheme_raw))

    assert revised.clinic_policy.sites[0].color == "#123456"


def test_workspace_store_round_trips_the_color_scheme(tmp_path) -> None:
    from rbs.store import Store

    instance = sample_instance()
    raw = instance.color_scheme.model_dump(mode="json")
    raw["name"] = "Example University"
    raw["primary"] = {"name": "Institution Blue", "color": "#123A67"}
    revised = replace_color_scheme(instance, ColorScheme.model_validate(raw))
    store = Store(tmp_path / "rbs.sqlite")
    store.init()

    workspace = store.create("Institutional workspace", revised)
    restored = store.get(workspace.id).instance

    assert restored.color_scheme == revised.color_scheme
    assert restored.rotation("clinic").color == revised.rotation("clinic").color
