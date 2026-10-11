"""Binary sensors for Proof dashcams."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, RTSP_MODE_OFF
from .coordinator import ProofCoordinator, gps_fix_fresh
from .entity import ProofEntity


@dataclass(frozen=True, kw_only=True)
class ProofBinarySensorDescription(BinarySensorEntityDescription):
    """Binary sensor description with a value extractor."""

    value_fn: Callable[[dict[str, Any]], bool | None]


def _ignition(dev: dict[str, Any]) -> bool | None:
    acc = (dev.get("stats") or {}).get("acc")
    return None if acc is None else bool(acc)


BINARY_SENSORS: tuple[ProofBinarySensorDescription, ...] = (
    ProofBinarySensorDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda dev: dev.get("online"),
    ),
    ProofBinarySensorDescription(
        key="ignition",
        translation_key="ignition",
        device_class=BinarySensorDeviceClass.POWER,
        icon="mdi:key-variant",
        value_fn=_ignition,
    ),
    ProofBinarySensorDescription(
        key="gps_fix",
        translation_key="gps_fix",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:crosshairs-gps",
        value_fn=gps_fix_fresh,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up binary sensors for every device on the account."""
    coordinator: ProofCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = [
        ProofBinarySensor(coordinator, device_id, description)
        for device_id in coordinator.data
        for description in BINARY_SENSORS
    ]
    entities.extend(
        ProofSdCardSensor(coordinator, device_id) for device_id in coordinator.data
    )
    entities.extend(
        ProofRtspLiveSensor(coordinator, device_id) for device_id in coordinator.data
    )
    async_add_entities(entities)


class ProofRtspLiveSensor(ProofEntity, BinarySensorEntity):
    """Whether the RTSP stream is carrying live video (off = standby picture).

    The attributes hold the URL to give the NVR, what triggered live video and
    until when, and whether an NVR is pulling the stream at all.
    """

    _attr_translation_key = "rtsp_live"
    _attr_icon = "mdi:cctv"

    def __init__(self, coordinator: ProofCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_rtsp_live"

    @property
    def available(self) -> bool:
        """Available while the RTSP output is switched on."""
        rtsp = self.coordinator.rtsp
        return super().available and rtsp is not None and rtsp.mode != RTSP_MODE_OFF

    @property
    def is_on(self) -> bool:
        """Return whether live video is going out right now."""
        return self.coordinator.rtsp.is_live(self._device_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The stream's URL, mode and triggers."""
        return self.coordinator.rtsp.state(self._device_id)


class ProofSdCardSensor(ProofEntity, BinarySensorEntity):
    """Whether the dashcam has an SD card, as the app's self-check reports."""

    _attr_translation_key = "sd_card"
    _attr_icon = "mdi:sd"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ProofCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_sd_card"

    @property
    def available(self) -> bool:
        """Known only once the storage has been read from the dashcam."""
        return super().available and bool(
            self.coordinator.storage.get(self._device_id)
        )

    @property
    def is_on(self) -> bool | None:
        """Return whether a card is present."""
        storage = self.coordinator.storage.get(self._device_id) or {}
        return (storage.get("external_sd") or {}).get("exist")


class ProofBinarySensor(ProofEntity, BinarySensorEntity):
    """A boolean state from a Proof dashcam."""

    entity_description: ProofBinarySensorDescription

    def __init__(
        self,
        coordinator: ProofCoordinator,
        device_id: str,
        description: ProofBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator, device_id)
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return the binary state."""
        if (dev := self.device) is None:
            return None
        return self.entity_description.value_fn(dev)
