"""Numeric dashcam settings (speaker volume)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ProofCoordinator
from .entity import ProofEntity


@dataclass(frozen=True, kw_only=True)
class ProofNumberDescription(NumberEntityDescription):
    """A dashcam setting that holds a number."""

    prop: str


NUMBERS: tuple[ProofNumberDescription, ...] = (
    ProofNumberDescription(
        key="volume",
        translation_key="volume",
        prop="voice",
        icon="mdi:volume-high",
        native_min_value=0,
        native_max_value=15,
        native_step=1,
        mode=NumberMode.SLIDER,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the numeric settings for each dashcam."""
    coordinator: ProofCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ProofSettingNumber(hass, coordinator, device_id, description)
        for device_id in coordinator.data
        for description in NUMBERS
    )


class ProofSettingNumber(ProofEntity, NumberEntity):
    """One numeric dashcam setting."""

    entity_description: ProofNumberDescription

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: ProofCoordinator,
        device_id: str,
        description: ProofNumberDescription,
    ) -> None:
        super().__init__(coordinator, device_id)
        self.hass = hass
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def available(self) -> bool:
        """Available once the device's settings have been read."""
        return super().available and self.native_value is not None

    @property
    def native_value(self) -> float | None:
        """Return the current value."""
        props = self.coordinator.device_props.get(self._device_id) or {}
        value = props.get(self.entity_description.prop)
        return None if value is None else float(value)

    async def async_set_native_value(self, value: float) -> None:
        """Write the new value to the dashcam."""
        await self.coordinator.async_set_device_props(
            self.hass, self._device_id, {self.entity_description.prop: int(value)}
        )
