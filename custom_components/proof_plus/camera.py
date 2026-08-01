"""Live view camera for Proof dashcams.

The camera is streamed on demand only: a WebRTC session to the device is opened
when Home Assistant first reads a frame and torn down a short while after the
last read, so the dashcam never streams (and never uses its cellular data)
unless someone is actually watching.

Home Assistant's base ``Camera`` serves MJPEG by polling ``async_camera_image``
at ``frame_interval``; we feed it the latest decoded WebRTC frame, giving
full-motion live video without re-muxing H.264.
"""
from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_LIVE_KEEPALIVE,
    DEFAULT_LIVE_KEEPALIVE,
    DOMAIN,
)
from .coordinator import ProofCoordinator
from .entity import ProofEntity
from .webrtc import ProofWebRTCClient

_LOGGER = logging.getLogger(__name__)

# ~5 fps MJPEG.
STREAM_INTERVAL = 0.2


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up a live camera per device."""
    coordinator: ProofCoordinator = hass.data[DOMAIN][entry.entry_id]
    keepalive = entry.options.get(CONF_LIVE_KEEPALIVE, DEFAULT_LIVE_KEEPALIVE)
    async_add_entities(
        ProofCamera(hass, coordinator, device_id, keepalive)
        for device_id in coordinator.data
    )


class ProofCamera(ProofEntity, Camera):
    """On-demand live WebRTC view of a Proof dashcam (served as MJPEG)."""

    _attr_translation_key = "live"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: ProofCoordinator,
        device_id: str,
        keepalive: int,
    ) -> None:
        ProofEntity.__init__(self, coordinator, device_id)
        Camera.__init__(self)
        self.hass = hass
        self._keepalive = keepalive
        self._attr_unique_id = f"{device_id}_live"
        self._client: ProofWebRTCClient | None = None
        self._lock = asyncio.Lock()
        self._idle_handle: asyncio.TimerHandle | None = None

    @property
    def frame_interval(self) -> float:
        """MJPEG frame interval."""
        return STREAM_INTERVAL

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the current live frame; opens the session on demand."""
        client = await self._async_ensure_client()
        if client is None:
            return None
        self._reset_idle_timer()
        frame = await self._async_wait_for_frame(client)
        return frame

    async def _async_ensure_client(self) -> ProofWebRTCClient | None:
        async with self._lock:
            if self._client is not None:
                return self._client
            client = ProofWebRTCClient(
                async_get_clientsession(self.hass),
                self.coordinator.client,
                self._device_id,
            )
            try:
                await client.async_start()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Could not start live view for %s: %s", self._device_id, err
                )
                await client.async_stop()
                return None
            self._client = client
            return client

    async def _async_wait_for_frame(
        self, client: ProofWebRTCClient, timeout: float = 10
    ) -> bytes | None:
        deadline = self.hass.loop.time() + timeout
        while self.hass.loop.time() < deadline:
            frame = client.latest_frame
            if frame is not None:
                return await self.hass.async_add_executor_job(_frame_to_jpeg, frame)
            await asyncio.sleep(0.1)
        return None

    def _reset_idle_timer(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        # 0 keepalive means keep the session open until the entity is removed.
        if self._keepalive <= 0:
            return
        self._idle_handle = self.hass.loop.call_later(
            self._keepalive,
            lambda: self.hass.async_create_task(self._async_teardown()),
        )

    async def _async_teardown(self) -> None:
        async with self._lock:
            if self._client is not None:
                await self._client.async_stop()
                self._client = None

    async def async_will_remove_from_hass(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
        await self._async_teardown()


def _frame_to_jpeg(frame: Any) -> bytes:
    """Convert an av.VideoFrame to JPEG bytes (CPU-bound; run in executor)."""
    image = frame.to_image()
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()
