import pytest
from pydantic import ValidationError

from rbs.catalog import sample_instance
from rbs.models.color_scheme import (
    ColorScheme,
    _hex_to_oklab,
    _oklab_distance,
    accessible_text_color,
    contrast_ratio,
    contrasting_text_color,
    generate_accent_colors,
)
from rbs.models.instance import SchedulerInput
from rbs.ui.settings.color_scheme import replace_color_scheme


def test_default_color_scheme_matches_bundled_rbs_colors() -> None:
    scheme = ColorScheme()

    assert scheme.name == "RBS Navy & Gold"
    assert scheme.primary.color == "#174A7E"
    assert scheme.secondary.color == "#C58A17"
    assert scheme.neutral.color == "#52606D"
    assert scheme.palette == (
        "#174A7E",
        "#C58A17",
        "#52606D",
        "#0058A2",
        "#006565",
        "#00694A",
        "#326800",
        "#765000",
        "#4F4B9E",
        "#8E3C00",
        "#1978D2",
        "#008988",
        "#008D65",
        "#4B8A1D",
        "#9E6C00",
        "#6D6BC2",
        "#B45B29",
    )


def test_color_scheme_normalizes_hex_values_and_rejects_duplicates() -> None:
    raw = ColorScheme().model_dump(mode="json")
    raw["primary"]["color"] = " #123abc "
    assert ColorScheme.model_validate(raw).primary.color == "#123ABC"

    raw["secondary"]["color"] = "#123ABC"
    with pytest.raises(ValidationError, match="schedule palette colors must be unique"):
        ColorScheme.model_validate(raw)


def test_color_scheme_accepts_legacy_color_names_but_does_not_serialize_them() -> None:
    raw = ColorScheme().model_dump(mode="json")
    for index, token in enumerate(
        (raw["primary"], raw["secondary"], raw["neutral"], *raw["accents"]),
        start=1,
    ):
        token["name"] = f"Legacy color {index}"

    migrated = ColorScheme.model_validate(raw)

    serialized = migrated.model_dump(mode="json")
    assert serialized["name"] == "RBS Navy & Gold"
    assert all(
        "name" not in token
        for token in (
            serialized["primary"],
            serialized["secondary"],
            serialized["neutral"],
            *serialized["accents"],
        )
    )


def test_generated_accents_are_deterministic_distinct_and_visible() -> None:
    anchors = ("#123A67", "#EAAA00", "#5A5D61")

    first = generate_accent_colors(*anchors)
    second = generate_accent_colors(*anchors)

    assert first == second
    assert len(first) == 14
    assert len(set(first)) == len(first)
    assert not set(first) & set(anchors)
    assert all(contrast_ratio(color, "#FFFFFF") >= 3.0 for color in first)
    comparisons = [*anchors, *first]
    for index, color in enumerate(first, start=len(anchors)):
        assert min(
            _oklab_distance(_hex_to_oklab(color), _hex_to_oklab(other))
            for other in comparisons[:index]
        ) >= 0.04


@pytest.mark.parametrize("count", [1, 14, 24])
def test_generated_accents_support_every_scheme_size(count: int) -> None:
    generated = generate_accent_colors("#000000", "#FFFFFF", "#777777", count=count)

    assert len(generated) == count
    assert len(set(generated)) == count


def test_generated_accents_reject_invalid_counts_and_duplicate_anchors() -> None:
    with pytest.raises(ValueError, match="between 1 and 24"):
        generate_accent_colors("#123456", "#654321", "#777777", count=0)
    with pytest.raises(ValueError, match="must be unique"):
        generate_accent_colors("#123456", "#123456", "#777777")


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
    raw["primary"] = {"color": "#123A67"}
    revised = replace_color_scheme(instance, ColorScheme.model_validate(raw))

    assert revised.color_scheme.primary.color == "#123A67"
    assert all(
        rotation.color == "#123A67"
        for rotation in revised.rotations
        if instance.rotation(rotation.id).color == "#174A7E"
    )
    assert revised.clinic_policy.site("cedar").color == "#123A67"
    assert revised.clinic_policy.site("maple").color == instance.clinic_policy.site("maple").color


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
    raw["primary"] = {"color": "#123A67"}
    revised = replace_color_scheme(instance, ColorScheme.model_validate(raw))
    store = Store(tmp_path / "rbs.sqlite")
    store.init()

    workspace = store.create("Institutional workspace", revised)
    restored = store.get(workspace.id).instance

    assert restored.color_scheme == revised.color_scheme
    assert restored.rotation("clinic").color == revised.rotation("clinic").color
