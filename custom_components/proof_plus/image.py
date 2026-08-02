"""Latest-event snapshot image for Proof dashcams.

Shows the most recent cloud event snapshot (impact/"shake" event). The list is
polled cheaply on the coordinator's interval; the image bytes are only fetched
when Home Assistant actually renders the entity, so nothing streams on its own.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENABLE_LIVE,
    CONF_EVENT_IMAGES,
    CONF_MESSAGE_IMAGES,
    DEFAULT_EVENT_IMAGES,
    DEFAULT_MESSAGE_IMAGES,
    DOMAIN,
)
from .coordinator import ProofCoordinator
from .entity import ProofEntity

_LOGGER = logging.getLogger(__name__)


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
    # Photos belonging to alerts, newest first.
    keep = entry.options.get(CONF_MESSAGE_IMAGES, DEFAULT_MESSAGE_IMAGES)
    entities.extend(
        ProofMessagePhoto(hass, coordinator, device_id, position)
        for device_id in coordinator.data
        for position in range(keep)
    )

    # A still from each camera, captured once when the card first renders and
    # then left alone. Needs the live session, so only when live view is on.
    if entry.options.get(CONF_ENABLE_LIVE):
        entities.extend(
            ProofLiveSnapshotImage(hass, coordinator, device_id, index)
            for device_id, device in coordinator.data.items()
            for index in range(_camera_count(device))
        )
    async_add_entities(entities)


class ProofMessagePhoto(ProofEntity, ImageEntity):
    """One of the photos the dashcam saved for a recent alert.

    Only some alerts leave pictures — anti-theft, vibration and collision do,
    ignition does not — so the newest photos across all alerts are flattened
    into a list and this entity shows one position in it.
    """

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
        self._attr_name = f"Alert photo {position + 1}"
        self._attr_unique_id = f"{device_id}_message_photo_{position + 1}"
        self._fid: str | None = None
        self._photo: dict[str, Any] = {}
        self._message: dict[str, Any] = {}
        self._sync()

    def _photos(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Every alert photo, newest first, paired with its alert."""
        return [
            (photo, message)
            for message in (self.coordinator.messages.get(self._device_id) or [])
            for photo in (message.get("photos") or [])
        ]

    def _sync(self) -> bool:
        """Point at the photo for this position; True if it changed."""
        photos = self._photos()
        photo, message = (
            photos[self._position] if self._position < len(photos) else ({}, {})
        )
        fid = photo.get("fid")
        if fid == self._fid:
            return False
        self._fid, self._photo, self._message = fid, photo, message
        if when := message.get("time"):
            self._attr_image_last_updated = dt_util.parse_datetime(when)
        return True

    @property
    def available(self) -> bool:
        """Available once an alert with a photo has arrived."""
        return super().available and self._fid is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Say which alert this picture belongs to."""
        if not self._fid:
            return {}
        attrs: dict[str, Any] = {
            "camera": self._photo.get("camera"),
            "alert": self._message.get("topic"),
            "text": self._message.get("text"),
            "type": self._message.get("type"),
            # Lets a dashboard put each picture under the alert it belongs to.
            "message_id": self._message.get("id"),
        }
        if when := self._message.get("time"):
            attrs["time"] = dt_util.as_local(dt_util.parse_datetime(when)).isoformat()
        for key in ("latitude", "longitude"):
            if key in self._message:
                attrs[key] = self._message[key]
        return attrs

    async def async_image(self) -> bytes | None:
        """Download this picture on demand."""
        if self._fid is None:
            return None
        return await self.coordinator.client.async_download(
            self.coordinator.client.file_url(self._fid)
        )

    def _handle_coordinator_update(self) -> None:
        if self._sync():
            self._cached_image = None
        super()._handle_coordinator_update()


_SNAPSHOT_KEYS = {0: "front_snapshot", 1: "rear_snapshot"}
# How long to wait for a frame from the camera we just asked for. The first
# capture after a restart has to negotiate WebRTC from cold — ICE, TURN and the
# device's own wake-up — so this has to be generous or it gives up too early.
_CAPTURE_TIMEOUT = 45


class ProofLiveSnapshotImage(ProofEntity, ImageEntity):
    """A still from one camera, taken on demand rather than on a timer.

    Home Assistant asks an image entity for its picture when the card is first
    shown and then caches it until the entity says it changed. That is exactly
    the behaviour wanted here: one capture when the dashboard loads, and no
    further cellular traffic until the refresh button is pressed.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: ProofCoordinator,
        device_id: str,
        cam_index: int,
    ) -> None:
        ProofEntity.__init__(self, coordinator, device_id)
        ImageEntity.__init__(self, hass)
        self._cam_index = cam_index
        self._attr_translation_key = _SNAPSHOT_KEYS.get(cam_index, "front_snapshot")
        self._attr_unique_id = f"{device_id}_snapshot_{cam_index}"
        self._data: bytes | None = None
        # Surfaced as an attribute: this box keeps no log file, so a failed
        # capture would otherwise be invisible.
        self._last_error: str | None = None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Say whether the last capture worked, and why not if it did not."""
        return {
            "camera": "Rear" if self._cam_index else "Front",
            "has_image": self._data is not None,
            "last_error": self._last_error,
        }

    @property
    def cam_index(self) -> int:
        """Which camera this snapshot belongs to."""
        return self._cam_index

    @property
    def data(self) -> bytes | None:
        """The last captured still, if there is one."""
        return self._data or (
            self.coordinator.stills.get(self._device_id) or {}
        ).get(self._cam_index)

    async def async_added_to_hass(self) -> None:
        """Make this snapshot reachable by the refresh button."""
        await super().async_added_to_hass()
        self.coordinator.snapshot_images.setdefault(self._device_id, []).append(self)

    async def async_will_remove_from_hass(self) -> None:
        """Stop advertising this snapshot."""
        images = self.coordinator.snapshot_images.get(self._device_id) or []
        if self in images:
            images.remove(self)
        await super().async_will_remove_from_hass()

    async def async_capture(self) -> bool:
        """Take a fresh still from this camera. True if one was captured."""
        try:
            captured = await self._async_capture()
        except Exception as err:  # noqa: BLE001
            # Never let this reach the image endpoint as a 500; a dashcam that
            # is asleep or busy is a normal condition, not a server fault.
            self._last_error = f"{type(err).__name__}: {err}"
            _LOGGER.debug("Snapshot failed for %s: %s", self.entity_id, err)
            return False
        self._last_error = None if captured else "no frame arrived in time"
        return captured

    async def _async_capture(self) -> bool:
        from .camera import _frame_to_jpeg

        session = self.coordinator.live_session(self.hass, self._device_id)
        key = f"snapshot:{self._device_id}:{self._cam_index}"
        session.add_watcher(key)
        try:
            existing = session.client
            seq_before = existing.frame_seq if existing is not None else 0
            client = await session.async_acquire(self._cam_index)
            if client is None:
                return False
            deadline = self.hass.loop.time() + _CAPTURE_TIMEOUT
            while self.hass.loop.time() < deadline:
                frame = client.latest_frame
                # Only accept a frame decoded after the camera was selected,
                # otherwise this is the other camera's picture.
                if frame is not None and client.frame_seq > seq_before:
                    self._data = await self.hass.async_add_executor_job(
                        _frame_to_jpeg, frame
                    )
                    # Share it, so the camera card shows this new picture too.
                    self.coordinator.stills.setdefault(self._device_id, {})[
                        self._cam_index
                    ] = self._data
                    self._attr_image_last_updated = dt_util.utcnow()
                    self.async_write_ha_state()
                    return True
                await asyncio.sleep(0.1)
            return False
        finally:
            # Let the dashcam go back to sleep once nothing else is watching.
            await session.async_release(key)

    async def async_image(self) -> bytes | None:
        """Serve the still, capturing one the first time it is asked for."""
        if self.data is None:
            await self.async_capture()
        return self.data


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
