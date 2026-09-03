"""Workspace-owned application and schedule color schemes."""

from __future__ import annotations

import math
import re
from typing import Any

from pydantic import Field, field_validator, model_validator

from rbs.models.common import StrictModel

_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")

DEFAULT_PRIMARY_COLOR = "#174A7E"
DEFAULT_SECONDARY_COLOR = "#C58A17"
DEFAULT_NEUTRAL_COLOR = "#52606D"
DEFAULT_INK_COLOR = "#262626"

_MIN_CHROMATIC_CHROMA = 0.02
_MIN_ACCENT_CHROMA = 0.13
_MAX_ACCENT_CHROMA = 0.22
_ACCENT_CHROMA_BOOST = 1.35
_TARGET_ACCENT_DISTANCE = 0.04
_GOLDEN_ANGLE = 137.50776405003785


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


def _srgb_to_linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(channel: float) -> float:
    if channel <= 0.0031308:
        return 12.92 * channel
    return 1.055 * channel ** (1 / 2.4) - 0.055


def _hex_to_oklab(value: str) -> tuple[float, float, float]:
    color = normalize_hex_color(value)
    red, green, blue = (
        _srgb_to_linear(int(color[index : index + 2], 16) / 255)
        for index in (1, 3, 5)
    )
    long = 0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue
    medium = 0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue
    short = 0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue
    long_root = math.copysign(abs(long) ** (1 / 3), long)
    medium_root = math.copysign(abs(medium) ** (1 / 3), medium)
    short_root = math.copysign(abs(short) ** (1 / 3), short)
    return (
        0.2104542553 * long_root + 0.7936177850 * medium_root - 0.0040720468 * short_root,
        1.9779984951 * long_root - 2.4285922050 * medium_root + 0.4505937099 * short_root,
        0.0259040371 * long_root + 0.7827717662 * medium_root - 0.8086757660 * short_root,
    )


def _oklab_to_linear_rgb(
    lightness: float,
    green_red: float,
    blue_yellow: float,
) -> tuple[float, float, float]:
    long_root = lightness + 0.3963377774 * green_red + 0.2158037573 * blue_yellow
    medium_root = lightness - 0.1055613458 * green_red - 0.0638541728 * blue_yellow
    short_root = lightness - 0.0894841775 * green_red - 1.2914855480 * blue_yellow
    long = long_root**3
    medium = medium_root**3
    short = short_root**3
    return (
        4.0767416621 * long - 3.3077115913 * medium + 0.2309699292 * short,
        -1.2684380046 * long + 2.6097574011 * medium - 0.3413193965 * short,
        -0.0041960863 * long - 0.7034186147 * medium + 1.7076147010 * short,
    )


def _oklab_to_oklch(value: tuple[float, float, float]) -> tuple[float, float, float]:
    lightness, green_red, blue_yellow = value
    chroma = math.hypot(green_red, blue_yellow)
    hue = math.degrees(math.atan2(blue_yellow, green_red)) % 360
    return lightness, chroma, hue


def _oklch_to_oklab(
    lightness: float,
    chroma: float,
    hue: float,
) -> tuple[float, float, float]:
    radians = math.radians(hue)
    return lightness, chroma * math.cos(radians), chroma * math.sin(radians)


def _in_srgb_gamut(rgb: tuple[float, float, float]) -> bool:
    return all(-1e-9 <= channel <= 1 + 1e-9 for channel in rgb)


def _gamut_mapped_hex(value: tuple[float, float, float]) -> str:
    """Convert OKLab to sRGB, reducing chroma until it fits the gamut."""
    lightness, chroma, hue = _oklab_to_oklch(value)
    lightness = min(1.0, max(0.0, lightness))
    low = 0.0
    high = chroma
    for _ in range(24):
        candidate = _oklch_to_oklab(lightness, high, hue)
        if _in_srgb_gamut(_oklab_to_linear_rgb(*candidate)):
            low = high
            break
        midpoint = (low + high) / 2
        candidate = _oklch_to_oklab(lightness, midpoint, hue)
        if _in_srgb_gamut(_oklab_to_linear_rgb(*candidate)):
            low = midpoint
        else:
            high = midpoint
    fitted = _oklch_to_oklab(lightness, low, hue)
    channels = _oklab_to_linear_rgb(*fitted)
    encoded = [min(1.0, max(0.0, _linear_to_srgb(channel))) for channel in channels]
    return "#" + "".join(f"{round(channel * 255):02X}" for channel in encoded)


def _hue_distance(left: float, right: float) -> float:
    return abs((left - right + 180) % 360 - 180)


def _signed_hue_delta(start: float, end: float) -> float:
    return (end - start + 180) % 360 - 180


def _oklab_distance(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> float:
    return math.sqrt(
        sum((first - second) ** 2 for first, second in zip(left, right, strict=True))
    )


def _hue_families(
    primary: tuple[float, float, float],
    secondary: tuple[float, float, float],
    count: int,
) -> list[tuple[float, float]]:
    _, primary_chroma, primary_hue = _oklab_to_oklch(primary)
    _, secondary_chroma, secondary_hue = _oklab_to_oklch(secondary)
    if primary_chroma < _MIN_CHROMATIC_CHROMA:
        primary_hue = secondary_hue if secondary_chroma >= _MIN_CHROMATIC_CHROMA else 250.0
    if secondary_chroma < _MIN_CHROMATIC_CHROMA:
        secondary_hue = primary_hue if primary_chroma >= _MIN_CHROMATIC_CHROMA else 70.0

    delta = _signed_hue_delta(primary_hue, secondary_hue)
    midpoint = (primary_hue + delta / 2) % 360
    direction = 1.0 if delta >= 0 else -1.0
    candidates = [
        (primary_hue, 1.0),
        ((primary_hue + delta / 3) % 360, 0.95),
        (midpoint, 0.90),
        ((primary_hue + 2 * delta / 3) % 360, 0.95),
        (secondary_hue, 1.0),
        ((primary_hue - direction * 30) % 360, 0.82),
        ((secondary_hue + direction * 30) % 360, 0.82),
        ((midpoint + 150) % 360, 0.68),
        ((midpoint - 150) % 360, 0.68),
    ]
    candidates.extend(
        ((midpoint + index * _GOLDEN_ANGLE) % 360, 0.65)
        for index in range(1, 97)
    )

    selected: list[tuple[float, float]] = []
    minimum_hue_distance = min(28.0, 360.0 / max(count, 1) * 0.8)
    for candidate in candidates:
        if all(_hue_distance(candidate[0], hue) >= minimum_hue_distance for hue, _ in selected):
            selected.append(candidate)
            if len(selected) == count:
                return selected
    while len(selected) < count:
        hue = max(
            range(360),
            key=lambda candidate: min(
                (_hue_distance(float(candidate), existing) for existing, _ in selected),
                default=360.0,
            ),
        )
        selected.append((float(hue), 0.65))
    return selected


def _display_color(
    hue: float,
    lightness: float,
    chroma: float,
) -> str:
    candidate = _oklch_to_oklab(lightness, chroma, hue)
    color = _gamut_mapped_hex(candidate)
    while contrast_ratio(color, "#FFFFFF") < 3.0 and candidate[0] > 0.2:
        candidate = (candidate[0] - 0.01, candidate[1], candidate[2])
        color = _gamut_mapped_hex(candidate)
    return color


def _distinct_display_color(
    *,
    hue: float,
    lightness: float,
    chroma: float,
    reserved: set[str],
    comparison_colors: list[tuple[float, float, float]],
) -> str:
    hue_offsets = (
        0.0,
        8.0,
        -8.0,
        16.0,
        -16.0,
        24.0,
        -24.0,
        36.0,
        -36.0,
        _GOLDEN_ANGLE,
        -_GOLDEN_ANGLE,
        2 * _GOLDEN_ANGLE,
        -2 * _GOLDEN_ANGLE,
    )
    lightness_offsets = (0.0, -0.025, 0.025, -0.05, 0.05, -0.075, 0.075, -0.1, 0.1)
    chroma_scales = (1.0, 0.85, 1.15, 0.7)
    best: tuple[float, str] | None = None
    for hue_offset in hue_offsets:
        for lightness_offset in lightness_offsets:
            for chroma_scale in chroma_scales:
                color = _display_color(
                    (hue + hue_offset) % 360,
                    min(0.68, max(0.36, lightness + lightness_offset)),
                    chroma * chroma_scale,
                )
                if color in reserved:
                    continue
                lab = _hex_to_oklab(color)
                distance = min(
                    (_oklab_distance(lab, other) for other in comparison_colors),
                    default=1.0,
                )
                if distance >= _TARGET_ACCENT_DISTANCE:
                    return color
                if best is None or distance > best[0]:
                    best = (distance, color)
    if best is None:  # pragma: no cover - all valid colors cannot be reserved
        raise RuntimeError("could not generate a unique accent color")
    return best[1]


def generate_accent_colors(
    primary: str,
    secondary: str,
    neutral: str,
    *,
    count: int = 14,
) -> tuple[str, ...]:
    """Generate a deterministic, perceptually spaced palette from brand colors."""
    if not 1 <= count <= 24:
        raise ValueError("accent color count must be between 1 and 24")
    anchors = tuple(normalize_hex_color(value) for value in (primary, secondary, neutral))
    if len(set(anchors)) != len(anchors):
        raise ValueError("primary, secondary, and neutral colors must be unique")

    primary_lab, secondary_lab, neutral_lab = map(_hex_to_oklab, anchors)
    primary_chroma = _oklab_to_oklch(primary_lab)[1]
    secondary_chroma = _oklab_to_oklch(secondary_lab)[1]
    base_chroma = min(
        _MAX_ACCENT_CHROMA,
        max(
            _MIN_ACCENT_CHROMA,
            (primary_chroma + secondary_chroma) / 2 * _ACCENT_CHROMA_BOOST,
        ),
    )

    family_count = (count + 1) // 2
    families = _hue_families(primary_lab, secondary_lab, family_count)
    deep = [(hue, 0.46, base_chroma * strength) for hue, strength in families]
    bright = [
        (hue, 0.57, base_chroma * strength)
        for hue, strength in families[: count - family_count]
    ]
    reserved = set(anchors)
    comparison_colors = [primary_lab, secondary_lab, neutral_lab]
    generated: list[str] = []
    for hue, lightness, chroma in (*deep, *bright):
        color = _distinct_display_color(
            hue=hue,
            lightness=lightness,
            chroma=chroma,
            reserved=reserved,
            comparison_colors=comparison_colors,
        )
        generated.append(color)
        reserved.add(color)
        comparison_colors.append(_hex_to_oklab(color))
    return tuple(generated)


class SchemeColor(StrictModel):
    """One color in an application scheme."""

    color: str

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_name(cls, value: Any) -> Any:
        if isinstance(value, dict) and "name" in value:
            migrated = dict(value)
            migrated.pop("name", None)
            return migrated
        return value

    @field_validator("color")
    @classmethod
    def normalize_color(cls, value: str) -> str:
        return normalize_hex_color(value)


def _default_accents() -> list[SchemeColor]:
    return [
        SchemeColor(color=color)
        for color in generate_accent_colors(
            DEFAULT_PRIMARY_COLOR,
            DEFAULT_SECONDARY_COLOR,
            DEFAULT_NEUTRAL_COLOR,
        )
    ]


class ColorScheme(StrictModel):
    """Application page theme and the palette offered by schedule selectors."""

    name: str = Field(default="RBS Navy & Gold", min_length=1, max_length=80)
    primary: SchemeColor = Field(
        default_factory=lambda: SchemeColor(color=DEFAULT_PRIMARY_COLOR)
    )
    secondary: SchemeColor = Field(
        default_factory=lambda: SchemeColor(color=DEFAULT_SECONDARY_COLOR)
    )
    neutral: SchemeColor = Field(
        default_factory=lambda: SchemeColor(color=DEFAULT_NEUTRAL_COLOR)
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
        return self

    @property
    def selectable_colors(self) -> tuple[SchemeColor, ...]:
        """Colors shown in rotation and clinic assignment selectors."""
        return (self.primary, self.secondary, self.neutral, *self.accents)

    @property
    def palette(self) -> tuple[str, ...]:
        """Return normalized colors in their schedule-selector order."""
        return tuple(item.color for item in self.selectable_colors)


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
    "generate_accent_colors",
    "normalize_hex_color",
    "relative_luminance",
]
