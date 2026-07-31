"""Sensors for Proof dashcams."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    DEGREE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfLength,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import ProofCoordinator
from .entity import ProofEntity


def _gps(dev: dict[str, Any]) -> dict[str, Any]:
    return (dev.get("status") or {}).get("gps") or {}


def _status_stats(dev: dict[str, Any]) -> dict[str, Any]:
    return (dev.get("status") or {}).get("stats") or {}


def _last_seen(dev: dict[str, Any]) -> datetime | None:
    stime = (dev.get("status") or {}).get("stime")
    return dt_util.utc_from_timestamp(stime) if stime else None


def _total_distance(dev: dict[str, Any]) -> float | None:
    dis = (dev.get("stats") or {}).get("curDis")
    return round(dis / 1000, 1) if isinstance(dis, (int, float)) else None


@dataclass(frozen=True, kw_only=True)
class ProofSensorDescription(SensorEntityDescription):
    """Sensor description with a value extractor."""

    value_fn: Callable[[dict[str, Any]], Any]


SENSORS: tuple[ProofSensorDescription, ...] = (
    ProofSensorDescription(
        key="speed",
        translation_key="speed",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda dev: _gps(dev).get("speed"),
    ),
    ProofSensorDescription(
        key="altitude",
        translation_key="altitude",
        native_unit_of_measurement=UnitOfLength.METERS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:altimeter",
        value_fn=lambda dev: _gps(dev).get("alt"),
    ),
    ProofSensorDescription(
        key="heading",
        translation_key="heading",
        native_unit_of_measurement=DEGREE,
        icon="mdi:compass",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda dev: _gps(dev).get("heading"),
    ),
    ProofSensorDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda dev: _status_stats(dev).get("temp"),
    ),
    ProofSensorDescription(
        key="signal_strength",
        translation_key="signal_strength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda dev: _status_stats(dev).get("sig"),
    ),
    ProofSensorDescription(
        key="total_distance",
        translation_key="total_distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:map-marker-distance",
        value_fn=_total_distance,
    ),
    ProofSensorDescription(
        key="last_seen",
        translation_key="last_seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_last_seen,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up sensors for every device on the account."""
    coordinator: ProofCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ProofSensor(coordinator, device_id, description)
        for device_id in coordinator.data
        for description in SENSORS
    )


class ProofSensor(ProofEntity, SensorEntity):
    """A single value from a Proof dashcam."""

    entity_description: ProofSensorDescription

    def __init__(
        self,
        coordinator: ProofCoordinator,
        device_id: str,
        description: ProofSensorDescription,
    ) -> None:
        super().__init__(coordinator, device_id)
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        if (dev := self.device) is None:
            return None
        return self.entity_description.value_fn(dev)
