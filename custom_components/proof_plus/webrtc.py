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
from collections.abc import Callable
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

# Receives each complete H.264 access unit as it arrives: (Annex-B bytes,
# 90 kHz timestamp counted from the start of the session).
EncodedSink = Callable[[bytes, int], None]


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
        # Bumped whenever a frame arrives, so a caller can wait for one that is
        # genuinely newer rather than accepting whatever is cached.
        self._frame_seq = 0
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
        # Consumers of the video exactly as the dashcam encoded it (the RTSP
        # stream), and whether tapping it worked on this aiortc.
        self._encoded_sinks: set[EncodedSink] = set()
        self.encoded_tap: bool | None = None
        self._video_receiver: Any = None

    @property
    def latest_frame(self) -> VideoFrame | None:
        """Return the most recently decoded video frame."""
        return self._latest_frame

    @property
    def frame_seq(self) -> int:
        """How many frames have been decoded; use to wait for a fresh one."""
        return self._frame_seq

    def invalidate_frame(self) -> None:
        """Forget the cached frame.

        Called when the device is told to change camera: the frame still in
        hand belongs to the previous camera, and serving it would show the
        wrong view.
        """
        self._latest_frame = None

    @property
    def has_media(self) -> bool:
        """Whether the device has produced any media track yet."""
        return self._video_track is not None

    @property
    def alive(self) -> bool:
        """Whether the session is still up as far as WebRTC can tell."""
        return (
            not self._closed
            and self._pc is not None
            and self._pc.connectionState not in ("failed", "closed")
        )

    def add_encoded_sink(self, sink: EncodedSink) -> None:
        """Receive every H.264 access unit as the dashcam sent it."""
        self._encoded_sinks.add(sink)

    def remove_encoded_sink(self, sink: EncodedSink) -> None:
        """Stop receiving access units."""
        self._encoded_sinks.discard(sink)

    def _tap_encoded(self, track: Any) -> None:
        """Copy each encoded video frame out before aiortc decodes it.

        aiortc only hands out decoded pictures; re-encoding those for RTSP
        would cost a CPU core and lose quality. Its receiver passes every
        reassembled frame to the decoder thread through a queue, so a copy is
        taken there — on the event loop, where that put happens — and the
        decoding the live view and snapshots rely on carries on untouched.
        This reaches into aiortc internals (pinned in the manifest), so if the
        queue is not where it is expected, the tap reports itself off.
        """
        assert self._pc is not None
        receiver = next(
            (
                transceiver.receiver
                for transceiver in self._pc.getTransceivers()
                if transceiver.receiver.track is track
            ),
            None,
        )
        queue = getattr(receiver, "_RTCRtpReceiver__decoder_queue", None)
        if queue is None or not callable(getattr(queue, "put", None)):
            _LOGGER.warning(
                "Cannot tap the encoded video for %s on this aiortc version; "
                "RTSP will show the standby picture only",
                self._device_id,
            )
            self.encoded_tap = False
            return
        put = queue.put
        sinks = self._encoded_sinks

        def _put(item: Any, *args: Any, **kwargs: Any) -> None:
            if item is not None and sinks:
                codec, frame = item
                if getattr(codec, "mimeType", "").lower() == "video/h264":
                    for sink in list(sinks):
                        try:
                            sink(frame.data, frame.timestamp)
                        except Exception:  # noqa: BLE001
                            _LOGGER.exception("Encoded video consumer failed")
            put(item, *args, **kwargs)

        queue.put = _put
        self.encoded_tap = True
        self._video_receiver = receiver

    async def async_request_keyframe(self) -> bool:
        """Ask the dashcam for a keyframe (an RTCP picture-loss indication).

        The dashcam sends one keyframe when the session starts and never
        another unless asked — measured: none in 15 s, then one ~150 ms after
        each request. A consumer that joins later, or an NVR that needs a
        keyframe every few seconds, has to ask.
        """
        receiver = self._video_receiver
        send = getattr(receiver, "_send_rtcp_pli", None)
        ssrcs = list(getattr(receiver, "_RTCRtpReceiver__active_ssrc", None) or ())
        if send is None or not ssrcs:
            return False
        for ssrc in ssrcs:
            try:
                await send(ssrc)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Keyframe request to %s failed", self._device_id)
        return True

    def subscribe(self) -> tuple[Any, Any]:
        """Return relayed (video, audio) tracks for one consumer.

        Unbuffered: each consumer only ever holds the newest frame. A buffered
        relay queues every decoded frame for a consumer that stops reading (a
        phone that went to sleep mid-stream), and that queue grew until the
        kernel killed Home Assistant for running out of memory.
        """
        video = (
            self._relay.subscribe(self._video_track, buffered=False)
            if self._video_track
            else None
        )
        audio = (
            self._relay.subscribe(self._audio_track, buffered=False)
            if self._audio_track
            else None
        )
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
                self._tap_encoded(track)
                # Consume through the relay so browser consumers can subscribe
                # to the same track independently.
                self._tasks.append(
                    asyncio.ensure_future(
                        self._consume(self._relay.subscribe(track, buffered=False))
                    )
                )
            elif track.kind == "audio":
                self._audio_track = track
                # Always drain the audio. aiortc queues every decoded frame on the
                # remote track without limit until someone calls recv(); with no
                # browser listening (an NVR takes video only, the session now
                # lingers between viewers) nothing did, and the queue grew by
                # ~47 AudioFrames/s -- the Core OOM leak, caught with
                # profiler.start_log_objects on 2026-10-17 (+1400 AudioFrame / 30 s,
                # memory +1.2 %/min while a live session was open).
                self._tasks.append(
                    asyncio.ensure_future(
                        self._drain(self._relay.subscribe(track, buffered=False))
                    )
                )

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
                if kind not in self.reports:
                    # Which reports the dashcam pushes over a live session (looking for a
                    # live position: the status GPS block has been frozen since 2026-10-09).
                    _LOGGER.info("Proof %s pushes report %r: %s", self._device_id, kind,
                                 json.dumps(reply.get("data"))[:400])
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
        # Whatever was decoded up to now came from the old camera.
        self.invalidate_frame()

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

    async def _drain(self, track: Any) -> None:
        """Read and drop frames so the source track's queue never grows."""
        try:
            while not self._closed:
                await track.recv()
        except asyncio.CancelledError:
            pass
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Audio track ended for %s: %s", self._device_id, err)

    async def _consume(self, track: Any) -> None:
        try:
            while not self._closed:
                frame = await track.recv()
                self._latest_frame = frame
                self._frame_seq += 1
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
