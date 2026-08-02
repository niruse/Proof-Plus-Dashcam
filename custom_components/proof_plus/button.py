"""Buttons for Proof dashcams."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENABLE_LIVE, CONF_ENABLE_SNAPSHOT, DOMAIN
from .coordinator import ProofCoordinator
from .entity import ProofEntity

_LOGGER = logging.getLogger(__name__)

LOCATE = ButtonEntityDescription(
    key="locate",
    translation_key="locate",
    icon="mdi:crosshairs-gps",
)

SELF_CHECK = ButtonEntityDescription(
    key="self_check",
    translation_key="self_check",
    icon="mdi:clipboard-check",
    entity_category=EntityCategory.DIAGNOSTIC,
)

REFRESH_SNAPSHOTS = ButtonEntityDescription(
    key="refresh_snapshots",
    translation_key="refresh_snapshots",
    icon="mdi:camera-retake",
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
        entities.append(ProofSelfCheckButton(coordinator, device_id))
        # Only useful when there are snapshot images to refresh.
        if entry.options.get(CONF_ENABLE_LIVE) and entry.options.get(
            CONF_ENABLE_SNAPSHOT
        ):
            entities.append(ProofRefreshSnapshotsButton(hass, coordinator, device_id))
    async_add_entities(entities)


class ProofRefreshSnapshotsButton(ProofEntity, ButtonEntity):
    """Take a new still from each camera.

    The snapshots are captured once when the dashboard first shows them and
    then left alone, because every capture wakes the dashcam and spends its
    mobile data. This is how the user asks for a newer one.
    """

    def __init__(
        self, hass: HomeAssistant, coordinator: ProofCoordinator, device_id: str
    ) -> None:
        super().__init__(coordinator, device_id)
        self.hass = hass
        self.entity_description = REFRESH_SNAPSHOTS
        self._attr_unique_id = f"{device_id}_refresh_snapshots"

    async def async_press(self) -> None:
        """Recapture every snapshot image belonging to this dashcam."""
        images = self.coordinator.snapshot_images.get(self._device_id) or []
        for entity in images:
            if not await entity.async_capture():
                _LOGGER.warning(
                    "Could not refresh the snapshot for %s", entity.entity_id
                )


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
        """Connect to the dashcam and read its settings and storage.

        Both come over the same session, so read them together rather than
        waking the camera twice.
        """
        if await self.coordinator.async_get_device_props(
            self.hass, self._device_id
        ) is None:
            _LOGGER.warning("Could not read the settings from %s", self._device_id)
            return
        await self.coordinator.async_get_storage(self.hass, self._device_id)


class ProofSelfCheckButton(ProofEntity, ButtonEntity):
    """Run the self-check now."""

    def __init__(self, coordinator: ProofCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self.entity_description = SELF_CHECK
        self._attr_unique_id = f"{device_id}_self_check"

    async def async_press(self) -> None:
        """Check the device's reported state and record the result."""
        self.coordinator.async_run_self_check(self._device_id)
