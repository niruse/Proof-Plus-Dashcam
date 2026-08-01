"""Latest-event snapshot image for Proof dashcams.

Shows the most recent cloud event snapshot (impact/"shake" event). The list is
polled cheaply on the coordinator's interval; the image bytes are only fetched
when Home Assistant actually renders the entity, so nothing streams on its own.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import ProofCoordinator
from .entity import ProofEntity


def _camera_count(device: dict[str, Any]) -> int:
    """Return how many cameras the dashcam exposes."""
    caps = (device.get("stats") or {}).get("caps") or {}
    return 2 if caps.get("bcamera") else 1


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up a latest-snapshot image for each camera on each device."""
    coordinator: ProofCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ProofSnapshotImage(hass, coordinator, device_id, index, count)
        for device_id, device in coordinator.data.items()
        for count in (_camera_count(device),)
        for index in range(count)
    )


_EVENT_KEYS = {0: "last_event_front", 1: "last_event_rear"}


class ProofSnapshotImage(ProofEntity, ImageEntity):
    """The most recent event snapshot from one camera of a dashcam."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: ProofCoordinator,
        device_id: str,
        cam_index: int = 0,
        cam_count: int = 1,
    ) -> None:
        ProofEntity.__init__(self, coordinator, device_id)
        ImageEntity.__init__(self, hass)
        self._cam_index = cam_index
        # A single-camera dashcam keeps the plain "Last event" name.
        self._attr_translation_key = (
            _EVENT_KEYS.get(cam_index, "last_event") if cam_count > 1 else "last_event"
        )
        self._attr_unique_id = (
            f"{device_id}_last_event"
            if cam_index == 0
            else f"{device_id}_last_event_{cam_index}"
        )
        self._fid: str | None = None
        self._loc: list[float] | None = None
        self._update_from_events()

    def _update_from_events(self) -> bool:
        """Point the entity at the newest image event; return True if it changed.

        Every event produces a video clip and an image for each camera, so
        filter on both: images only (otherwise an MP4 would be served as a
        JPEG) and this entity's camera.
        """
        events = self.coordinator.latest_events.get(self._device_id) or []
        newest = next(
            (
                e
                for e in events
                if e.get("ftype") == "image"
                and (e.get("camid") or 0) == self._cam_index
            ),
            None,
        )
        if newest is None:
            return False
        fid = newest.get("fid")
        if not fid or fid == self._fid:
            return False
        self._fid = fid
        self._loc = newest.get("loc")
        if (event_ms := newest.get("time")) is not None:
            self._attr_image_last_updated = dt_util.utc_from_timestamp(event_ms / 1000)
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the event's GPS location."""
        if not self._loc or len(self._loc) != 2:
            return {}
        return {"latitude": self._loc[0], "longitude": self._loc[1]}

    async def async_image(self) -> bytes | None:
        """Download the current snapshot on demand."""
        if self._fid is None:
            return None
        return await self.coordinator.client.async_download(
            self.coordinator.client.file_url(self._fid)
        )

    def _handle_coordinator_update(self) -> None:
        """Refresh the pointer when a newer event appears."""
        if self._update_from_events():
            self._cached_image = None
        super()._handle_coordinator_update()
