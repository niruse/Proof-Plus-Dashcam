"""Device tracker for Proof dashcams."""
from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import ProofCoordinator, gps_fix_fresh
from .entity import ProofEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up trackers for every device on the account."""
    coordinator: ProofCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ProofDeviceTracker(coordinator, device_id) for device_id in coordinator.data
    )


class ProofDeviceTracker(ProofEntity, TrackerEntity):
    """GPS position of a Proof dashcam."""

    _attr_name = None  # entity takes the device name

    def __init__(self, coordinator: ProofCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_tracker"

    @property
    def _gps(self) -> dict[str, Any]:
        return ((self.device or {}).get("status") or {}).get("gps") or {}

    @property
    def source_type(self) -> SourceType:
        """Position comes from the device GPS."""
        return SourceType.GPS

    @property
    def _current(self) -> bool:
        """Whether the position is a current fix.

        A dashcam that has lost GPS keeps resending its last position, which
        on the map is somewhere the car no longer is. Report no position then
        (the tracker reads unknown); the last one stays in the attributes.
        """
        return gps_fix_fresh(self.device or {}) is not False

    @property
    def latitude(self) -> float | None:
        """Latitude of the device, while the fix is current."""
        return self._gps.get("lat") if self._current else None

    @property
    def longitude(self) -> float | None:
        """Longitude of the device, while the fix is current."""
        return self._gps.get("lng") if self._current else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the raw fix details."""
        gps = self._gps
        attrs: dict[str, Any] = {
            "altitude": gps.get("alt"),
            "heading": gps.get("heading"),
            "speed": gps.get("speed"),
            "gps_valid": gps.get("valid"),
            # False when the dashcam is repeating an old position because it
            # cannot see satellites; the tracker then shows no position.
            "gps_fix_fresh": gps_fix_fresh(self.device or {}),
            "satellites": gps.get("satu"),
        }
        if gps.get("time"):
            attrs["gps_time"] = dt_util.utc_from_timestamp(gps["time"]).isoformat()
        if not self._current:
            attrs["last_known_latitude"] = gps.get("lat")
            attrs["last_known_longitude"] = gps.get("lng")
        return attrs
