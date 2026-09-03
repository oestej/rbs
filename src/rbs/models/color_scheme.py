"""Workspace-owned application and schedule color schemes."""

from __future__ import annotations

import re

from pydantic import Field, field_validator, model_validator

from rbs.models.common import StrictModel

_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")

DEFAULT_PRIMARY_COLOR = "#174A7E"
DEFAULT_SECONDARY_COLOR = "#C58A17"
DEFAULT_NEUTRAL_COLOR = "#52606D"
DEFAULT_INK_COLOR = "#262626"


def normalize_hex_color(value: str) -> str:
    """Normalize one CSS hex color and reject unsupported formats."""
    normalized = value.strip().upper()
    if not _HEX_COLOR.fullmatch(normalized):
        raise ValueError("color must use #RRGGBB format")
    return normalized


def relative_luminance(value: str) -> float:
    """Return the WCAG relative luminance of a normalized hex color."""
    color = normalize_hex_color(value)
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(foreground: str, background: str) -> float:
    """Return the WCAG contrast ratio between two hex colors."""
    foreground_luminance = relative_luminance(foreground)
    background_luminance = relative_luminance(background)
    lighter = max(foreground_luminance, background_luminance)
    darker = min(foreground_luminance, background_luminance)
    return (lighter + 0.05) / (darker + 0.05)


def accessible_text_color(
    value: str,
    *,
    background: str = "#FFFFFF",
    fallback: str = DEFAULT_INK_COLOR,
) -> str:
    """Use a requested text color when readable, otherwise a safe foreground."""
    color = normalize_hex_color(value)
    background = normalize_hex_color(background)
    fallback = normalize_hex_color(fallback)
    if contrast_ratio(color, background) >= 4.5:
        return color
    if contrast_ratio(fallback, background) >= 4.5:
        return fallback
    return contrasting_text_color(background)


def contrasting_text_color(value: str) -> str:
    """Return a foreground that meets WCAG AA for normal text."""
    color = normalize_hex_color(value)
    candidates = ("#FFFFFF", DEFAULT_INK_COLOR)
    best = max(candidates, key=lambda candidate: contrast_ratio(candidate, color))
    if contrast_ratio(best, color) >= 4.5:
        return best
    # There is a narrow mid-luminance band where the softer RBS ink and white
    # both miss 4.5:1. Pure black is the accessible last-resort foreground.
    return "#000000"


class SchemeColor(StrictModel):
    """One named color in an application scheme."""

    name: str = Field(min_length=1, max_length=40)
    color: str

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("color name cannot be empty")
        return normalized

    @field_validator("color")
    @classmethod
    def normalize_color(cls, value: str) -> str:
        return normalize_hex_color(value)


def _default_accents() -> list[SchemeColor]:
    return [
        SchemeColor(name="Ocean", color="#2B6F8A"),
        SchemeColor(name="Denim", color="#355C7D"),
        SchemeColor(name="Indigo", color="#4B4E9A"),
        SchemeColor(name="Purple", color="#6B4C8A"),
        SchemeColor(name="Plum", color="#7A3E65"),
        SchemeColor(name="Terracotta", color="#9B4535"),
        SchemeColor(name="Burnt Orange", color="#A65323"),
        SchemeColor(name="Ochre", color="#8A6418"),
        SchemeColor(name="Olive", color="#5F6B2F"),
        SchemeColor(name="Teal", color="#1E6F72"),
        SchemeColor(name="Steel", color="#536878"),
        SchemeColor(name="Brown", color="#76543E"),
        SchemeColor(name="Berry", color="#963C5A"),
        SchemeColor(name="Clinic Blue", color="#3971B8"),
    ]


class ColorScheme(StrictModel):
    """Application page theme and the palette offered by schedule selectors."""

    name: str = Field(default="RBS Navy & Gold", min_length=1, max_length=80)
    primary: SchemeColor = Field(
        default_factory=lambda: SchemeColor(name="Navy", color=DEFAULT_PRIMARY_COLOR)
    )
    secondary: SchemeColor = Field(
        default_factory=lambda: SchemeColor(name="Goldenrod", color=DEFAULT_SECONDARY_COLOR)
    )
    neutral: SchemeColor = Field(
        default_factory=lambda: SchemeColor(name="Slate", color=DEFAULT_NEUTRAL_COLOR)
    )
    accents: list[SchemeColor] = Field(
        default_factory=_default_accents,
        min_length=1,
        max_length=24,
    )

    @field_validator("name")
    @classmethod
    def normalize_scheme_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("color scheme name cannot be empty")
        return normalized

    @model_validator(mode="after")
    def palette_entries_are_unique(self) -> ColorScheme:
        colors = [item.color for item in self.selectable_colors]
        if len(colors) != len(set(colors)):
            raise ValueError("schedule palette colors must be unique")
        names = [item.name.casefold() for item in self.selectable_colors]
        if len(names) != len(set(names)):
            raise ValueError("schedule palette names must be unique")
        return self

    @property
    def selectable_colors(self) -> tuple[SchemeColor, ...]:
        """Colors shown in rotation and clinic assignment selectors."""
        return (self.primary, self.secondary, self.neutral, *self.accents)

    @property
    def palette(self) -> dict[str, str]:
        """Map normalized hex values to their user-facing scheme names."""
        return {item.color: item.name for item in self.selectable_colors}


DEFAULT_COLOR_SCHEME = ColorScheme()

__all__ = [
    "ColorScheme",
    "DEFAULT_COLOR_SCHEME",
    "DEFAULT_INK_COLOR",
    "DEFAULT_NEUTRAL_COLOR",
    "DEFAULT_PRIMARY_COLOR",
    "DEFAULT_SECONDARY_COLOR",
    "SchemeColor",
    "accessible_text_color",
    "contrast_ratio",
    "contrasting_text_color",
    "normalize_hex_color",
    "relative_luminance",
]
