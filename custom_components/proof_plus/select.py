"""Dashcam settings that pick from a list of values."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ProofCoordinator
from .entity import ProofEntity

# The device stores sensitivity as a threshold, so a HIGHER number means it
# takes a bigger jolt to trigger — i.e. a LESS sensitive setting. The scales
# differ between motion and collision; both are taken from the app.
MOTION_SENSITIVITY = {"more": 8, "normal": 12, "less": 16}
COLLISION_SENSITIVITY = {
    "off": -1,
    "most": 1,
    "more": 4,
    "normal": 8,
    "less": 12,
    "least": 15,
}
IDLE_MINUTES = {"off": -1, "20": 20, "30": 30, "40": 40, "60": 60}
RECORD_HOURS = {"1": 1, "2": 2, "4": 4, "8": 8}


@dataclass(frozen=True, kw_only=True)
class ProofSelectDescription(SelectEntityDescription):
    """A dashcam setting chosen from a fixed set of values."""

    prop: str
    values: dict[str, int]


SELECTS: tuple[ProofSelectDescription, ...] = (
    ProofSelectDescription(
        key="motion_level",
        translation_key="motion_level",
        prop="vibl",
        values=MOTION_SENSITIVITY,
        options=list(MOTION_SENSITIVITY),
        icon="mdi:motion-sensor",
    ),
    ProofSelectDescription(
        key="accident_level",
        translation_key="accident_level",
        prop="collision_level",
        values=COLLISION_SENSITIVITY,
        options=list(COLLISION_SENSITIVITY),
        icon="mdi:car-emergency",
    ),
    ProofSelectDescription(
        key="idle_timing",
        translation_key="idle_timing",
        prop="neutral_time",
        values=IDLE_MINUTES,
        options=list(IDLE_MINUTES),
        icon="mdi:timer-sand",
    ),
    ProofSelectDescription(
        key="recording_duration",
        translation_key="recording_duration",
        prop="cm_time",
        values=RECORD_HOURS,
        options=list(RECORD_HOURS),
        icon="mdi:record-rec",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the list settings for each dashcam."""
    coordinator: ProofCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ProofSettingSelect(hass, coordinator, device_id, description)
        for device_id in coordinator.data
        for description in SELECTS
    )


class ProofSettingSelect(ProofEntity, SelectEntity):
    """One dashcam setting chosen from a list."""

    entity_description: ProofSelectDescription

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: ProofCoordinator,
        device_id: str,
        description: ProofSelectDescription,
    ) -> None:
        super().__init__(coordinator, device_id)
        self.hass = hass
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def available(self) -> bool:
        """Available once the device's settings have been read."""
        return super().available and self.current_option is not None

    @property
    def current_option(self) -> str | None:
        """Return the selected option, if the device's value maps to one."""
        props = self.coordinator.device_props.get(self._device_id) or {}
        value = props.get(self.entity_description.prop)
        if value is None:
            return None
        for option, option_value in self.entity_description.values.items():
            if option_value == value:
                return option
        return None

    async def async_select_option(self, option: str) -> None:
        """Write the chosen value to the dashcam."""
        value = self.entity_description.values.get(option)
        if value is None:
            return
        await self.coordinator.async_set_device_props(
            self.hass, self._device_id, {self.entity_description.prop: value}
        )
