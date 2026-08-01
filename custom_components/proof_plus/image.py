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

from .const import CONF_EVENT_IMAGES, DEFAULT_EVENT_IMAGES, DOMAIN
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
    entities: list[ImageEntity] = [
        ProofSnapshotImage(hass, coordinator, device_id, index, count)
        for device_id, device in coordinator.data.items()
        for count in (_camera_count(device),)
        for index in range(count)
    ]
    # A handful of recent event snapshots, so a dashboard can lay them out as
    # a grid of thumbnails that open full screen with their details.
    recent = entry.options.get(CONF_EVENT_IMAGES, DEFAULT_EVENT_IMAGES)
    entities.extend(
        ProofEventImage(hass, coordinator, device_id, position)
        for device_id in coordinator.data
        for position in range(recent)
    )
    async_add_entities(entities)


class ProofEventImage(ProofEntity, ImageEntity):
    """One of the most recent event snapshots, newest first."""

    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: ProofCoordinator,
        device_id: str,
        position: int,
    ) -> None:
        ProofEntity.__init__(self, coordinator, device_id)
        ImageEntity.__init__(self, hass)
        self._position = position
        self._attr_name = f"Event {position + 1}"
        self._attr_unique_id = f"{device_id}_event_{position + 1}"
        self._fid: str | None = None
        self._event: dict[str, Any] = {}
        self._sync()

    def _images(self) -> list[dict[str, Any]]:
        events = self.coordinator.latest_events.get(self._device_id) or []
        return [e for e in events if e.get("ftype") == "image" and e.get("fid")]

    def _sync(self) -> bool:
        """Point at the event for this position; True if it changed."""
        images = self._images()
        event = images[self._position] if self._position < len(images) else None
        fid = (event or {}).get("fid")
        if fid == self._fid:
            return False
        self._fid = fid
        self._event = event or {}
        if (event_ms := self._event.get("time")) is not None:
            self._attr_image_last_updated = dt_util.utc_from_timestamp(event_ms / 1000)
        return True

    @property
    def available(self) -> bool:
        """Available once there is an event in this position."""
        return super().available and self._fid is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The details behind the picture."""
        loc = self._event.get("loc") or []
        attrs: dict[str, Any] = {
            "camera": "Rear" if self._event.get("camid") else "Front",
            "event_type": self._event.get("type"),
        }
        if (event_ms := self._event.get("time")) is not None:
            attrs["taken"] = dt_util.as_local(
                dt_util.utc_from_timestamp(event_ms / 1000)
            ).isoformat()
        if len(loc) == 2:
            attrs["latitude"], attrs["longitude"] = loc[0], loc[1]
        return attrs

    async def async_image(self) -> bytes | None:
        """Download this snapshot on demand."""
        if self._fid is None:
            return None
        return await self.coordinator.client.async_download(
            self.coordinator.client.file_url(self._fid)
        )

    def _handle_coordinator_update(self) -> None:
        if self._sync():
            self._cached_image = None
        super()._handle_coordinator_update()


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
