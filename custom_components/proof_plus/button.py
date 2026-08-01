"""Buttons for Proof dashcams."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ProofCoordinator
from .entity import ProofEntity

_LOGGER = logging.getLogger(__name__)

LOCATE = ButtonEntityDescription(
    key="locate",
    translation_key="locate",
    icon="mdi:crosshairs-gps",
)

REFRESH_SETTINGS = ButtonEntityDescription(
    key="refresh_settings",
    translation_key="refresh_settings",
    icon="mdi:cog-refresh",
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the buttons for each dashcam."""
    coordinator: ProofCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[ButtonEntity] = []
    for device_id in coordinator.data:
        entities.append(ProofLocateButton(hass, coordinator, device_id))
        entities.append(ProofRefreshSettingsButton(hass, coordinator, device_id))
    async_add_entities(entities)


class ProofLocateButton(ProofEntity, ButtonEntity):
    """Wake the dashcam so it reports a fresh position."""

    def __init__(
        self, hass: HomeAssistant, coordinator: ProofCoordinator, device_id: str
    ) -> None:
        super().__init__(coordinator, device_id)
        self.hass = hass
        self.entity_description = LOCATE
        self._attr_unique_id = f"{device_id}_locate"

    async def async_press(self) -> None:
        """Ask the cloud to wake the device, then refresh its position."""
        await self.coordinator.client.async_wake_device(self._device_id)
        await self.coordinator.async_request_refresh()


class ProofRefreshSettingsButton(ProofEntity, ButtonEntity):
    """Read the dashcam's settings from the device."""

    def __init__(
        self, hass: HomeAssistant, coordinator: ProofCoordinator, device_id: str
    ) -> None:
        super().__init__(coordinator, device_id)
        self.hass = hass
        self.entity_description = REFRESH_SETTINGS
        self._attr_unique_id = f"{device_id}_refresh_settings"

    async def async_press(self) -> None:
        """Connect to the dashcam and read its settings."""
        if await self.coordinator.async_get_device_props(
            self.hass, self._device_id
        ) is None:
            _LOGGER.warning(
                "Could not read the settings from %s", self._device_id
            )
