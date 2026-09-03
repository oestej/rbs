"""Shared visual hierarchy for application buttons.

Visible labels use sentence case. Quasar's default all-caps transform is also
disabled in the application stylesheet so buttons created outside these shared
variants still follow the product convention.
"""

from __future__ import annotations

PRIMARY_BUTTON_PROPS = "unelevated no-caps"
SECONDARY_BUTTON_PROPS = "outline no-caps"
TERTIARY_BUTTON_PROPS = "flat no-caps"
DESTRUCTIVE_BUTTON_PROPS = "unelevated no-caps color=negative"
ICON_BUTTON_PROPS = "flat round dense"
DESTRUCTIVE_ICON_BUTTON_PROPS = "flat round dense color=negative"


def button_props(variant: str, *extra: str) -> str:
    """Add contextual Quasar properties to one shared button variant."""
    return " ".join((variant, *extra))
