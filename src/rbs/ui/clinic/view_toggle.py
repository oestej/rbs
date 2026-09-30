"""Clinic view selection with immediate browser state and retained calendars."""

from collections.abc import Mapping

from nicegui.element import Element
from nicegui.elements.mixins.value_element import ValueElement

from rbs.ui.clinic.projection import ClinicScheduleView


class ClinicViewToggle(
    ValueElement[ClinicScheduleView], component="../static/clinic_view_toggle.js",
):
    """Switch prepared calendars locally while recording the selection in the session."""

    LOOPBACK = None

    def __init__(self, value: ClinicScheduleView) -> None:
        super().__init__(value=value)

    @property
    def browser_selection(self) -> bool:
        """Whether the current value callback came from the local browser control."""
        return not self._send_update_on_value_change

    def attach_views(
        self,
        panels: Mapping[str, Element],
        legend: Element,
        legends: Mapping[str, str],
    ) -> None:
        self._props["panel_ids"] = {name: f"c{panel.id}" for name, panel in panels.items()}
        self._props["legend_id"] = f"c{legend.id}"
        self._props["legends"] = dict(legends)
