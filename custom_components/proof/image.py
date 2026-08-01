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


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up a latest-snapshot image per device."""
    coordinator: ProofCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ProofSnapshotImage(hass, coordinator, device_id)
        for device_id in coordinator.data
    )


class ProofSnapshotImage(ProofEntity, ImageEntity):
    """The most recent event snapshot for one dashcam."""

    _attr_translation_key = "last_event"

    def __init__(
        self, hass: HomeAssistant, coordinator: ProofCoordinator, device_id: str
    ) -> None:
        ProofEntity.__init__(self, coordinator, device_id)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"{device_id}_last_event"
        self._fid: str | None = None
        self._loc: list[float] | None = None
        self._update_from_events()

    def _update_from_events(self) -> bool:
        """Point the entity at the newest image event; return True if it changed.

        Each event yields both a video clip and image snapshots, so filter to
        the images — otherwise the entity would serve an MP4 as a JPEG.
        """
        events = self.coordinator.latest_events.get(self._device_id) or []
        newest = next((e for e in events if e.get("ftype") == "image"), None)
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
