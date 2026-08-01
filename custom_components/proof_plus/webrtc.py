"""WebRTC live client for Proof dashcams.

The camera is WebRTC-only: the app opens the imclient WebSocket, sends an SDP
offer to the device, trickles ICE candidates, and the device answers. Media
flows over a TURN relay. This module reproduces that handshake with aiortc and
keeps the latest decoded video frame available for the camera entity.

Signalling quirks that matter (learned the hard way):
  * The device ignores ICE candidates embedded in the offer SDP — they must be
    trickled as separate messages.
  * The device sends its candidates in a FLAT payload
    ({type:candidate, candidate:"<str>", sdpMid, sdpMLineIndex}) whereas the app
    sends them NESTED (candidate:{...}); both shapes must be handled.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.sdp import candidate_from_sdp
from av import VideoFrame

from .api import ProofApiClient

_LOGGER = logging.getLogger(__name__)

CMD_PING, CMD_ACK, CMD_LOGIN, CMD_MSG = 0, 1, 2, 4
MSG_SIGNAL = 6

HEARTBEAT_INTERVAL = 5
CONNECT_TIMEOUT = 30


class ProofWebRTCClient:
    """Maintains a live WebRTC session with one dashcam and buffers frames."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        client: ProofApiClient,
        device_id: str,
        camera_index: int = 0,
    ) -> None:
        self._session = session
        self._client = client
        self._device_id = device_id
        self._camera_index = camera_index
        self._pc: RTCPeerConnection | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._tasks: list[asyncio.Task] = []
        self._seq = 1
        self._latest_frame: VideoFrame | None = None
        self._connected = asyncio.Event()
        self._closed = False

    @property
    def latest_frame(self) -> VideoFrame | None:
        """Return the most recently decoded video frame."""
        return self._latest_frame

    async def async_start(self) -> None:
        """Open the session and block until the first frame arrives (or fail)."""
        token = await self._client.async_get_access_token()
        ice = await self._client.async_get_ice_servers()
        im_ip, ws_port = await self._client.async_get_im_endpoint()

        self._pc = RTCPeerConnection(
            RTCConfiguration(
                [
                    RTCIceServer(**server)
                    for server in ice
                ]
            )
        )

        @self._pc.on("track")
        def _on_track(track: Any) -> None:
            if track.kind == "video":
                self._tasks.append(asyncio.ensure_future(self._consume(track)))

        self._pc.addTransceiver("video", direction="recvonly")
        self._pc.addTransceiver("audio", direction="recvonly")
        await self._pc.setLocalDescription(await self._pc.createOffer())
        await self._await_ice_gathering()

        ws_url = f"ws://{im_ip}:{ws_port}/imclient?access_token={token}"
        self._ws = await self._session.ws_connect(ws_url, timeout=20)
        await self._send([CMD_LOGIN, 0, {
            "token": token,
            "info": {"ver": "3.1.37", "model": "homeassistant", "sysver": "12",
                     "pid": "ha-proof-live", "lang": "en_us", "os": "android",
                     "app": "Proof"},
        }])
        self._tasks.append(asyncio.ensure_future(self._reader()))
        self._tasks.append(asyncio.ensure_future(self._heartbeat()))

        try:
            await asyncio.wait_for(self._connected.wait(), timeout=CONNECT_TIMEOUT)
        except asyncio.TimeoutError as err:
            await self.async_stop()
            raise TimeoutError("Timed out establishing the live video stream") from err

    async def async_stop(self) -> None:
        """Tear the session down."""
        self._closed = True
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._pc is not None:
            await self._pc.close()
        self._pc = None
        self._ws = None

    async def _await_ice_gathering(self) -> None:
        assert self._pc is not None
        for _ in range(50):
            if self._pc.iceGatheringState == "complete":
                return
            await asyncio.sleep(0.2)

    async def _send(self, packet: list[Any]) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.send_str(json.dumps(packet))

    async def _send_signal(self, payload: dict[str, Any]) -> None:
        packet = [CMD_MSG, self._seq, [0, [f"{self._device_id}#0"], MSG_SIGNAL,
                                       int(time.time() * 1000), payload]]
        self._seq += 1
        await self._send(packet)

    async def _heartbeat(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await self._send([CMD_PING, 0, None])
        except asyncio.CancelledError:
            pass

    async def _consume(self, track: Any) -> None:
        try:
            while not self._closed:
                frame = await track.recv()
                self._latest_frame = frame
                self._connected.set()
        except asyncio.CancelledError:
            pass
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Video track ended for %s: %s", self._device_id, err)

    async def _reader(self) -> None:
        assert self._ws is not None and self._pc is not None
        authed = False
        try:
            async for msg in self._ws:
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    break
                arr = json.loads(msg.data)
                if not isinstance(arr, list) or not arr:
                    continue
                if arr[0] == CMD_ACK and not authed:
                    authed = True
                    await self._start_call()
                elif arr[0] == CMD_MSG and len(arr) >= 3 and isinstance(arr[2], list):
                    await self._handle_signal(arr[2])
        except asyncio.CancelledError:
            pass
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Signalling reader stopped for %s: %s", self._device_id, err)

    async def _start_call(self) -> None:
        assert self._pc is not None
        await self._send_signal(
            {"type": "offer", "sdp": self._pc.localDescription.sdp}
        )
        # Trickle our own candidates as separate messages (device ignores the
        # ones embedded in the offer SDP).
        mline = -1
        mid: str | None = None
        for line in self._pc.localDescription.sdp.splitlines():
            if line.startswith("m="):
                mline += 1
            elif line.startswith("a=mid:"):
                mid = line[6:].strip()
            elif line.startswith("a=candidate:"):
                await self._send_signal({"type": "candidate", "candidate": {
                    "candidate": line[2:], "sdpMLineIndex": mline, "sdpMid": mid}})

    async def _handle_signal(self, inner: list[Any]) -> None:
        assert self._pc is not None
        if len(inner) < 5 or not isinstance(inner[4], dict):
            return
        payload = inner[4]
        ptype = payload.get("type")
        if ptype == "answer":
            await self._pc.setRemoteDescription(
                RTCSessionDescription(sdp=payload["sdp"], type="answer")
            )
        elif ptype == "candidate":
            await self._add_remote_candidate(payload)

    async def _add_remote_candidate(self, payload: dict[str, Any]) -> None:
        assert self._pc is not None
        raw = payload.get("candidate")
        if isinstance(raw, dict):  # nested shape
            cand_str = raw.get("candidate")
            mid = raw.get("sdpMid")
            mline = raw.get("sdpMLineIndex")
        else:  # flat shape (what the device sends)
            cand_str = raw
            mid = payload.get("sdpMid")
            mline = payload.get("sdpMLineIndex")
        if not cand_str:
            return
        candidate = candidate_from_sdp(cand_str.split(":", 1)[1])
        candidate.sdpMid = mid
        candidate.sdpMLineIndex = mline
        await self._pc.addIceCandidate(candidate)
