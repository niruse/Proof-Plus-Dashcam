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
    UnitOfInformation,
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
    ProofSensorDescription(
        key="free_memory",
        translation_key="free_memory",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.KILOBYTES,
        suggested_unit_of_measurement=UnitOfInformation.MEGABYTES,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:memory",
        value_fn=lambda dev: _status_stats(dev).get("mem"),
    ),
    ProofSensorDescription(
        key="sim",
        translation_key="sim",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:sim",
        value_fn=lambda dev: (dev.get("stats") or {}).get("iccid"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up sensors for every device on the account."""
    coordinator: ProofCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        ProofSensor(coordinator, device_id, description)
        for device_id in coordinator.data
        for description in SENSORS
    ]
    for device_id in coordinator.data:
        entities.append(ProofSelfCheckSensor(coordinator, device_id))
        entities.append(ProofMessageSensor(coordinator, device_id))
        entities.append(ProofSelfCheckRunSensor(coordinator, device_id))
        for key, store, field in (
            ("sd_card_free", "external_sd", "free"),
            ("sd_card_total", "external_sd", "total"),
            ("internal_free", "internal_sd", "free"),
        ):
            entities.append(
                ProofStorageSensor(coordinator, device_id, key, store, field)
            )
    async_add_entities(entities)


class ProofSelfCheckSensor(ProofEntity, SensorEntity):
    """The outcome of the last self-check."""

    _attr_translation_key = "self_check"
    _attr_icon = "mdi:clipboard-check"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["ok", "problem", "unknown"]

    def __init__(self, coordinator: ProofCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_self_check"

    @property
    def _result(self) -> dict[str, Any]:
        return self.coordinator.self_check.get(self._device_id) or {}

    @property
    def native_value(self) -> str | None:
        """Return ok, problem, or unknown if it has never run."""
        result = self._result
        if not result:
            return None
        return "problem" if result.get("problems") else "ok"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose each checked item, like the app's self-check screen."""
        result = self._result
        if not result:
            return {}
        return {
            **result.get("items", {}),
            "gsm_quality": result.get("gsm_quality"),
            "gsm_signal_dbm": result.get("gsm_signal_dbm"),
            "problems": result.get("problems", []),
        }


class ProofMessageSensor(ProofEntity, SensorEntity):
    """The latest alert the cloud pushed, with the recent ones attached."""

    _attr_translation_key = "last_message"
    _attr_icon = "mdi:message-alert"

    def __init__(self, coordinator: ProofCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_last_message"

    @property
    def _messages(self) -> list[dict[str, Any]]:
        return self.coordinator.messages.get(self._device_id) or []

    @property
    def native_value(self) -> str | None:
        """Return the newest alert's headline."""
        if not self._messages:
            return None
        newest = self._messages[0]
        return (newest.get("topic") or newest.get("text") or "")[:255] or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the newest alert's detail and a short history."""
        listener = self.coordinator.message_listener
        attrs: dict[str, Any] = {
            # Alerts only arrive while this is true; useful for spotting a
            # dropped connection when no message has come in for a while.
            "listening": bool(listener and listener.connected),
        }
        if not self._messages:
            return attrs
        newest = self._messages[0]
        attrs.update(
            {
                "text": newest.get("text"),
                "type": newest.get("type"),
                "time": newest.get("time"),
                "latitude": newest.get("latitude"),
                "longitude": newest.get("longitude"),
                "messages": self._messages[:20],
            }
        )
        return attrs


class ProofStorageSensor(ProofEntity, SensorEntity):
    """A capacity figure the dashcam reports, in megabytes."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = UnitOfInformation.MEGABYTES
    _attr_suggested_unit_of_measurement = UnitOfInformation.GIGABYTES
    _attr_icon = "mdi:sd"

    def __init__(
        self,
        coordinator: ProofCoordinator,
        device_id: str,
        key: str,
        store: str,
        field: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_translation_key = key
        self._attr_unique_id = f"{device_id}_{key}"
        self._store = store
        self._field = field

    @property
    def native_value(self) -> float | None:
        """Return the capacity, once the storage has been read."""
        storage = self.coordinator.storage.get(self._device_id) or {}
        return (storage.get(self._store) or {}).get(self._field)


class ProofSelfCheckRunSensor(ProofEntity, SensorEntity):
    """When the self-check last ran."""

    _attr_translation_key = "self_check_run"
    _attr_icon = "mdi:clock-check"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: ProofCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_self_check_run"

    @property
    def native_value(self) -> datetime | None:
        """Return the time of the last run."""
        run = (self.coordinator.self_check.get(self._device_id) or {}).get("run")
        return dt_util.parse_datetime(run) if run else None


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
