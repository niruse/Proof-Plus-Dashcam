"""Live view camera for Proof dashcams.

The dashcam speaks WebRTC, and so does the Home Assistant frontend, so this
entity bridges the two: the browser's offer is answered locally by aiortc and
the device's H.264 video and Opus audio tracks are forwarded through untouched.
Nothing is transcoded, which keeps CPU use low and — unlike an MJPEG stream —
carries sound.

The session to the dashcam is opened on demand and closed again once nobody is
watching (after the configurable keep-alive), so the camera only uses its
cellular data while someone is actually looking at it.
"""
from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    WebRTCAnswer,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_LIVE_KEEPALIVE, DEFAULT_LIVE_KEEPALIVE, DOMAIN
from .coordinator import ProofCoordinator
from .entity import ProofEntity
from .webrtc import ProofWebRTCClient

_LOGGER = logging.getLogger(__name__)

FRAME_WAIT_TIMEOUT = 15


def _camera_count(device: dict[str, Any]) -> int:
    """Return how many cameras the dashcam exposes."""
    caps = (device.get("stats") or {}).get("caps") or {}
    return 2 if caps.get("bcamera") else 1


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up a live camera for each camera on each device."""
    coordinator: ProofCoordinator = hass.data[DOMAIN][entry.entry_id]
    keepalive = entry.options.get(CONF_LIVE_KEEPALIVE, DEFAULT_LIVE_KEEPALIVE)
    async_add_entities(
        ProofCamera(hass, coordinator, device_id, keepalive, index, count)
        for device_id, device in coordinator.data.items()
        for count in (_camera_count(device),)
        for index in range(count)
    )


_CAMERA_KEYS = {0: "live_front", 1: "live_rear"}


class DeviceLiveSession:
    """One live session per dashcam.

    The device streams a single camera at a time over one WebRTC session and
    refuses a second one, so the front and rear entities share this session and
    ask it to switch camera as needed.
    """

    def __init__(
        self, hass: HomeAssistant, coordinator: ProofCoordinator, device_id: str
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._device_id = device_id
        self._client: ProofWebRTCClient | None = None
        self._current_camera = 0
        self._lock = asyncio.Lock()

    @property
    def client(self) -> ProofWebRTCClient | None:
        """The running client, if any."""
        return self._client

    async def async_acquire(self, cam_index: int) -> ProofWebRTCClient | None:
        """Return a client streaming the requested camera, starting it if needed."""
        async with self._lock:
            if self._client is None:
                client = ProofWebRTCClient(
                    async_get_clientsession(self._hass),
                    self._coordinator.client,
                    self._device_id,
                    camera_index=cam_index,
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
                self._current_camera = cam_index
            elif cam_index != self._current_camera:
                await self._client.async_select_camera(cam_index, self._current_camera)
                self._current_camera = cam_index
            return self._client

    async def async_stop(self) -> None:
        """Close the session to the dashcam."""
        async with self._lock:
            if self._client is not None:
                await self._client.async_stop()
                self._client = None


class ProofCamera(ProofEntity, Camera):
    """Live WebRTC view of one camera on a Proof dashcam, with audio."""

    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: ProofCoordinator,
        device_id: str,
        keepalive: int,
        cam_index: int = 0,
        cam_count: int = 1,
    ) -> None:
        ProofEntity.__init__(self, coordinator, device_id)
        Camera.__init__(self)
        self.hass = hass
        self._keepalive = keepalive
        self._cam_index = cam_index
        # A single-camera dashcam keeps the plain "Live view" name.
        self._attr_translation_key = (
            _CAMERA_KEYS.get(cam_index, "live") if cam_count > 1 else "live"
        )
        self._attr_unique_id = (
            f"{device_id}_live" if cam_index == 0 else f"{device_id}_live_{cam_index}"
        )
        self._idle_handle: asyncio.TimerHandle | None = None
        # Browser peer connections, keyed by Home Assistant's session id.
        self._sessions: dict[str, Any] = {}
        self._session = coordinator.live_session(hass, device_id)

    # --- device session lifecycle ------------------------------------------

    async def _async_ensure_client(self) -> ProofWebRTCClient | None:
        """Get the shared device session, streaming this entity's camera."""
        return await self._session.async_acquire(self._cam_index)

    def _reset_idle_timer(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        # A keep-alive of 0 means hold the stream open until it is closed.
        if self._keepalive <= 0 or self._sessions:
            return
        self._idle_handle = self.hass.loop.call_later(
            self._keepalive,
            lambda: self.hass.async_create_task(self._async_teardown()),
        )

    async def _async_teardown(self) -> None:
        """Close the shared device session once nobody is watching it."""
        if self._sessions:
            return
        await self._session.async_stop()

    # --- still images -------------------------------------------------------

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a current still frame (thumbnails, snapshots, automations)."""
        client = await self._async_ensure_client()
        if client is None:
            return None
        self._reset_idle_timer()
        deadline = self.hass.loop.time() + FRAME_WAIT_TIMEOUT
        while self.hass.loop.time() < deadline:
            if (frame := client.latest_frame) is not None:
                return await self.hass.async_add_executor_job(_frame_to_jpeg, frame)
            await asyncio.sleep(0.1)
        return None

    # --- native WebRTC ------------------------------------------------------

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Answer the frontend's offer with the dashcam's own audio and video."""
        from aiortc import RTCPeerConnection, RTCSessionDescription

        client = await self._async_ensure_client()
        if client is None or not client.has_media:
            send_message(WebRTCError("proof_plus", "Could not reach the dashcam"))
            return

        pc = RTCPeerConnection()
        self._sessions[session_id] = pc
        self._reset_idle_timer()

        @pc.on("connectionstatechange")
        async def _on_state() -> None:
            if pc.connectionState in ("failed", "closed"):
                await self._async_close_session(session_id)

        video, audio = client.subscribe()
        if video is not None:
            pc.addTrack(video)
        if audio is not None:
            pc.addTrack(audio)

        try:
            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=offer_sdp, type="offer")
            )
            await pc.setLocalDescription(await pc.createAnswer())
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("WebRTC negotiation failed for %s: %s", self._device_id, err)
            await self._async_close_session(session_id)
            send_message(WebRTCError("proof_plus", "Could not start the live stream"))
            return

        send_message(WebRTCAnswer(pc.localDescription.sdp))

    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: Any
    ) -> None:
        """Add a trickled ICE candidate from the frontend."""
        from aiortc.sdp import candidate_from_sdp

        pc = self._sessions.get(session_id)
        if pc is None:
            return
        raw = getattr(candidate, "candidate", None)
        if not raw:
            return
        try:
            parsed = candidate_from_sdp(raw.split(":", 1)[1])
            parsed.sdpMid = getattr(candidate, "sdp_mid", None)
            parsed.sdpMLineIndex = getattr(candidate, "sdp_m_line_index", None)
            await pc.addIceCandidate(parsed)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Ignoring bad ICE candidate: %s", err)

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """Called by Home Assistant when the viewer goes away."""
        self.hass.async_create_task(self._async_close_session(session_id))

    async def _async_close_session(self, session_id: str) -> None:
        pc = self._sessions.pop(session_id, None)
        if pc is not None:
            try:
                await pc.close()
            except Exception:  # noqa: BLE001
                pass
        self._reset_idle_timer()

    async def async_will_remove_from_hass(self) -> None:
        """Close everything when the entity goes away."""
        if self._idle_handle is not None:
            self._idle_handle.cancel()
        for session_id in list(self._sessions):
            await self._async_close_session(session_id)
        await self._session.async_stop()


def _frame_to_jpeg(frame: Any) -> bytes:
    """Convert an av.VideoFrame to JPEG bytes (CPU-bound; run in executor)."""
    image = frame.to_image()
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()
