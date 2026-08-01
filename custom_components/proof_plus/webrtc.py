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
  * The device only understands the legacy data-channel dialect
    (``m=application <p> DTLS/SCTP 5000`` + ``a=sctpmap:``). Offering aiortc's
    modern ``UDP/DTLS/SCTP webrtc-datachannel`` form makes it drop the whole
    offer, so the SDP is rewritten before it is sent.
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
from aiortc.contrib.media import MediaRelay
from aiortc.sdp import candidate_from_sdp
from av import VideoFrame

from .api import ProofApiClient

_LOGGER = logging.getLogger(__name__)

CMD_PING, CMD_ACK, CMD_LOGIN, CMD_MSG = 0, 1, 2, 4
MSG_SIGNAL = 6

HEARTBEAT_INTERVAL = 5
CONNECT_TIMEOUT = 30
DATA_CHANNEL_TIMEOUT = 15


def _to_legacy_sctp(sdp: str) -> str:
    """Rewrite the data-channel media section into the device's dialect."""
    lines = []
    for line in sdp.splitlines():
        if line.startswith("m=application") and "webrtc-datachannel" in line:
            port = line.split()[1]
            lines.append(f"m=application {port} DTLS/SCTP 5000")
        elif line.startswith("a=sctp-port:"):
            lines.append("a=sctpmap:5000 webrtc-datachannel 1024")
        else:
            lines.append(line)
    return "\r\n".join(lines) + "\r\n"


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
        # Lets the browser bridge and the snapshot grabber consume the same
        # device tracks without opening a second session to the dashcam.
        self._relay = MediaRelay()
        self._video_track: Any = None
        self._audio_track: Any = None
        self._data_channel: Any = None
        self._data_channel_open = asyncio.Event()
        self._rpc_id = 0
        self._rpc_waiters: dict[str, asyncio.Future] = {}
        # Unsolicited reports the device pushes, keyed by their "type".
        self.reports: dict[str, Any] = {}
        self._report_waiters: dict[str, asyncio.Event] = {}

    @property
    def latest_frame(self) -> VideoFrame | None:
        """Return the most recently decoded video frame."""
        return self._latest_frame

    @property
    def has_media(self) -> bool:
        """Whether the device has produced any media track yet."""
        return self._video_track is not None

    def subscribe(self) -> tuple[Any, Any]:
        """Return relayed (video, audio) tracks for one consumer."""
        video = self._relay.subscribe(self._video_track) if self._video_track else None
        audio = self._relay.subscribe(self._audio_track) if self._audio_track else None
        return video, audio

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
                self._video_track = track
                # Consume through the relay so browser consumers can subscribe
                # to the same track independently.
                self._tasks.append(
                    asyncio.ensure_future(self._consume(self._relay.subscribe(track)))
                )
            elif track.kind == "audio":
                self._audio_track = track

        # Control channel for device RPCs such as switching camera.
        self._data_channel = self._pc.createDataChannel("proof")

        @self._data_channel.on("open")
        def _on_dc_open() -> None:
            self._data_channel_open.set()

        @self._data_channel.on("message")
        def _on_dc_message(message: Any) -> None:
            try:
                reply = json.loads(message)
            except (TypeError, ValueError):
                return
            waiter = self._rpc_waiters.pop(str(reply.get("msgid")), None)
            if waiter is not None and not waiter.done():
                waiter.set_result(reply)
                return
            # The device also pushes unsolicited reports, keyed by type.
            if (kind := reply.get("type")) :
                self.reports[kind] = reply.get("data")
                if (event := self._report_waiters.pop(kind, None)) is not None:
                    event.set()

        self._pc.addTransceiver("video", direction="recvonly")
        self._pc.addTransceiver("audio", direction="recvonly")
        offer = await self._pc.createOffer()
        await self._pc.setLocalDescription(
            RTCSessionDescription(sdp=_to_legacy_sctp(offer.sdp), type=offer.type)
        )
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

        if self._camera_index:
            await self.async_select_camera(self._camera_index)

    async def async_select_camera(self, index: int, current: int = 0) -> None:
        """Switch the device to the given camera (0 = front)."""
        if index == current:
            return
        try:
            await asyncio.wait_for(
                self._data_channel_open.wait(), timeout=DATA_CHANNEL_TIMEOUT
            )
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Control channel unavailable for %s; staying on the current camera",
                self._device_id,
            )
            return
        # switchCamera cycles through the device's cameras rather than taking an
        # index, so step from where we are to where we want to be.
        for _ in range((index - current) % 2 or 1):
            self._rpc("switchCamera")
            await asyncio.sleep(1)

    def _rpc(self, name: str, params: Any = None) -> str | None:
        """Send an RPC to the device over the control channel."""
        if self._data_channel is None or self._data_channel.readyState != "open":
            return None
        self._rpc_id += 1
        msgid = str(self._rpc_id)
        self._data_channel.send(
            json.dumps(
                {"type": "rpc_req", "name": name, "msgid": msgid, "params": params}
            )
        )
        return msgid

    async def async_rpc(
        self, name: str, params: Any = None, timeout: float = 10
    ) -> dict[str, Any] | None:
        """Send an RPC and wait for the device's reply."""
        try:
            await asyncio.wait_for(
                self._data_channel_open.wait(), timeout=DATA_CHANNEL_TIMEOUT
            )
        except asyncio.TimeoutError:
            _LOGGER.warning("Control channel unavailable for %s", self._device_id)
            return None
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future = loop.create_future()
        msgid = self._rpc(name, params)
        if msgid is None:
            return None
        self._rpc_waiters[msgid] = waiter
        try:
            return await asyncio.wait_for(waiter, timeout=timeout)
        except asyncio.TimeoutError:
            self._rpc_waiters.pop(msgid, None)
            _LOGGER.warning("No reply to %s from %s", name, self._device_id)
            return None

    async def async_request_report(
        self, request: str, report: str, timeout: float = 30
    ) -> dict[str, Any] | None:
        """Ask the device to report something and wait for it to arrive.

        Unlike the RPCs, these are plain ``{"type": ...}`` messages and the
        device answers with its own ``{"type": ..., "data": ...}`` push.
        """
        try:
            await asyncio.wait_for(
                self._data_channel_open.wait(), timeout=DATA_CHANNEL_TIMEOUT
            )
        except asyncio.TimeoutError:
            _LOGGER.warning("Control channel unavailable for %s", self._device_id)
            return None
        event = self._report_waiters.setdefault(report, asyncio.Event())
        self._send_data({"type": request})
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._report_waiters.pop(report, None)
            _LOGGER.debug("No %s from %s", report, self._device_id)
            return None
        return self.reports.get(report)

    async def async_get_storage(self) -> dict[str, Any] | None:
        """Return the dashcam's internal and SD-card capacity, in megabytes."""
        data = await self.async_request_report("report_SDCardInfo", "dev_sdcard_info")
        self._send_data({"type": "stop_SDCardInfo"})
        return data

    def _send_data(self, payload: dict[str, Any]) -> None:
        """Send a plain data-channel message (not an RPC)."""
        if self._data_channel is not None and self._data_channel.readyState == "open":
            self._data_channel.send(json.dumps(payload))

    async def async_get_props(self) -> dict[str, Any] | None:
        """Read the dashcam's settings."""
        reply = await self.async_rpc("getProps", {})
        if reply is None or reply.get("ret") != 0:
            return None
        return reply.get("data")

    async def async_set_props(self, props: dict[str, Any]) -> bool:
        """Write one or more dashcam settings."""
        reply = await self.async_rpc("setProps", props)
        return bool(reply and reply.get("ret") == 0)

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
