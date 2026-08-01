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

from .const import DOMAIN
from .coordinator import ProofCoordinator
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
        value_fn=lambda dev: ((dev.get("status") or {}).get("gps") or {}).get("valid"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up binary sensors for every device on the account."""
    coordinator: ProofCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ProofBinarySensor(coordinator, device_id, description)
        for device_id in coordinator.data
        for description in BINARY_SENSORS
    )


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
