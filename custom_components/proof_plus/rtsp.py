"""RTSP output, so an NVR can record the dashcam.

    dashcam --WebRTC--> ProofWebRTCClient --encoded H.264--> ProofRtspFeed
      --MPEG-TS over http://127.0.0.1:<feed>/feed/<device>/<camera>--> go2rtc
      --rtsp://<home-assistant>:<port>/proof_<device>[_rear]--> NVR

go2rtc is the copy Home Assistant ships (/bin/go2rtc), run as a second instance
with its own configuration: Home Assistant's own instance only listens on
localhost, where an NVR cannot reach it. go2rtc pulls a feed only while an RTSP
client is connected, so with no NVR connected nothing here touches the dashcam.

A feed carries either the dashcam's video, passed through exactly as the
dashcam encoded it, or the standby picture (see standby.py). Which one depends
on the mode:

* always — live video whenever the feed is pulled;
* trigger — live video while a trigger is active: an alert of a chosen type,
  the ignition being on (plus a grace period), the "RTSP live" switch, or the
  proof_plus.rtsp_trigger action. The standby picture the rest of the time.

The dashcam streams one camera at a time over one session, shared with the
camera entities and the settings controls — it ignores an offer for a second
video track or for its "#1" address. So with both cameras on the stream each
gets its own RTSP path and they take turns while live, each showing its last
picture between turns. A viewer who opens a camera in Home Assistant keeps
the dashcam on that camera for as long as they watch.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import tempfile
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import (
    CONF_RTSP_ALERT_MINUTES,
    CONF_RTSP_ALERTS,
    CONF_RTSP_ALTERNATE,
    CONF_RTSP_CAMERA,
    CONF_RTSP_DRIVING,
    CONF_RTSP_DRIVING_GRACE,
    CONF_RTSP_MODE,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_PORT,
    CONF_RTSP_USERNAME,
    DEFAULT_RTSP_ALERT_MINUTES,
    DEFAULT_RTSP_ALERTS,
    DEFAULT_RTSP_ALTERNATE,
    DEFAULT_RTSP_CAMERA,
    DEFAULT_RTSP_DRIVING,
    DEFAULT_RTSP_DRIVING_GRACE,
    DEFAULT_RTSP_MODE,
    DEFAULT_RTSP_PORT,
    DOMAIN,
    RTSP_MODE_ALWAYS,
    RTSP_MODE_OFF,
    RTSP_MODE_TRIGGER,
)
from .coordinator import last_report
from .messages import EVENT_MESSAGE
from .mpegts import TSMuxer
from .standby import (
    DEFAULT_SIZE,
    STANDBY_FPS,
    encode_standby,
    is_keyframe,
    render_standby,
    sps_profile,
)

if TYPE_CHECKING:
    from .coordinator import ProofCoordinator

_LOGGER = logging.getLogger(__name__)

# No live frame for this long while live means the session has stalled.
STALL_SECONDS = 8
# Keyframe requests on a live-but-silent connection before reconnecting.
MAX_NUDGES = 3
# A fresh session that has not produced a keyframe by then is restarted.
FIRST_KEYFRAME_SECONDS = 30
# The dashcam only sends a keyframe when asked. Ask this often while waiting
# for one, and while live so the NVR gets a keyframe every couple of seconds
# (to start recording segments, and to heal after a lost packet).
KEYFRAME_RETRY_SECONDS = 1
KEYFRAME_INTERVAL_SECONDS = 2
# Live timestamps follow the dashcam's frame-to-frame steps but never stray
# from the wall clock by more than this (90 kHz ticks).
MAX_FRAME_STEP = 90000
MAX_LEAD = 90000
MAX_LAG = 3 * 90000
# Waits between attempts to reach a dashcam that would not answer.
RETRY_BACKOFF = (15, 30, 60, 120, 300)
# Silence longer than this, then a report: the dashcam was off the network.
DROP_SECONDS = 60
# How often to retry putting the dashcam on the feed's camera.
RECLAIM_SECONDS = 1
# Frames queued for the feed before newer ones are dropped.
LIVE_QUEUE = 300
# Chunks queued for one reader (go2rtc) before it is cut off.
READER_QUEUE = 600

SESSION_KEY = "rtsp"
GO2RTC_BINARY = "/bin/go2rtc"
GO2RTC_RESTART_SECONDS = 10
GO2RTC_API_OFFSET = 10000


class RtspError(Exception):
    """The RTSP server could not be started."""


def _port_free(port: int) -> bool:
    """Whether a TCP port can be listened on, on all interfaces."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def _build_standby(
    still: bytes | None,
    size: tuple[int, int] | None,
    caption: str,
    profile: str,
    level: str,
) -> list[bytes]:
    """Render and encode a standby picture (runs in an executor)."""
    if size is None and still:
        from PIL import Image
        import io

        try:
            size = Image.open(io.BytesIO(still)).size
        except (OSError, ValueError):
            size = None
    width, height = size or DEFAULT_SIZE
    size = (width - width % 2, height - height % 2)
    picture = render_standby(still, size, caption)
    return encode_standby(picture, size, profile, level)


def _frame_to_jpeg(frame: Any) -> bytes:
    """A decoded frame as JPEG (runs in an executor)."""
    import io

    buffer = io.BytesIO()
    frame.to_image().save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


class ProofRtspFeed:
    """The stream go2rtc pulls for one camera of one dashcam."""

    def __init__(self, rtsp: ProofRtsp, device_id: str, camera: int) -> None:
        self._rtsp = rtsp
        self._hass = rtsp.hass
        self._device_id = device_id
        self._camera = camera
        self._name = "rear" if camera else "front"
        # Not ending in a digit: those claims belong to viewers of a camera.
        self._key = f"{device_id}:{SESSION_KEY}-{self._name}"
        self._readers: set[asyncio.Queue[bytes | None]] = set()
        self._task: asyncio.Task | None = None
        # True while live video (not the standby picture) is going out.
        self.live = False
        self.last_error: str | None = None
        # What the standby picture is built from, and the build.
        self._picture: bytes | None = None
        self._size: tuple[int, int] | None = None
        self._profile: tuple[str, str] = ("main", "3.1")
        self._standby: tuple[tuple, list[bytes]] | None = None
        self._building: asyncio.Task | None = None
        # The decoded picture behind the newest live frame sent.
        self._last_view: Any = None

    @property
    def readers(self) -> int:
        """How many readers (go2rtc connections) are pulling the feed."""
        return len(self._readers)

    async def async_serve(self, writer: asyncio.StreamWriter) -> None:
        """Stream to one reader until it goes away."""
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=READER_QUEUE)
        self._readers.add(queue)
        if self._task is None or self._task.done():
            self._task = self._hass.async_create_background_task(
                self._produce(), f"{DOMAIN} rtsp feed {self._device_id} {self._name}"
            )
        self._rtsp.async_notify()
        try:
            while (chunk := await queue.get()) is not None:
                writer.write(chunk)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self._readers.discard(queue)
            self._rtsp.async_notify()

    def _send(self, data: bytes) -> None:
        for queue in list(self._readers):
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                # A reader this far behind is stuck; cut it off and let go2rtc
                # reconnect rather than hold everything back.
                self._readers.discard(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)

    async def async_stop(self) -> None:
        """End the feed for every reader."""
        for queue in list(self._readers):
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)
        self._readers.clear()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._building is not None:
            self._building.cancel()

    # --- standby picture ------------------------------------------------------

    def _still(self) -> bytes | None:
        return self._picture or self._rtsp.cached_still(self._device_id, self._camera)

    def _standby_signature(self) -> tuple:
        still = self._still()
        return (
            id(still) if still else None,
            self._size,
            self._profile,
            self._rtsp.standby_caption(self._device_id, self._camera),
        )

    def _start_standby_build(self) -> asyncio.Task:
        signature = self._standby_signature()
        still = self._still()
        profile, level = self._profile

        async def _build() -> list[bytes]:
            units = await self._hass.async_add_executor_job(
                _build_standby, still, self._size, signature[3], profile, level
            )
            self._standby = (signature, units)
            return units

        self._building = self._hass.async_create_background_task(
            _build(), f"{DOMAIN} rtsp standby {self._device_id} {self._name}"
        )
        return self._building

    async def _standby_units(self, wait: bool) -> list[bytes] | None:
        """The standby picture's access units, rebuilding when it has changed.

        With ``wait`` False a rebuild runs in the background and the previous
        picture is returned meanwhile, so the stream never pauses for it.
        """
        current = self._standby
        if current is not None and current[0] == self._standby_signature():
            return current[1]
        task = self._building
        if task is None or task.done():
            task = self._start_standby_build()
        if wait or current is None:
            try:
                return await task
            except Exception as err:  # noqa: BLE001
                self.last_error = f"Could not build the standby picture: {err}"
                _LOGGER.error(self.last_error)
                raise
        return current[1]

    async def _remember_view(self) -> None:
        """Make the last live picture the next standby picture.

        Taken from the frame kept while live rather than from the session's
        newest one: by the time this camera's turn ends, the session may
        already be showing the other camera.
        """
        frame = self._last_view
        if frame is None:
            return
        self._size = (frame.width, frame.height)
        try:
            self._picture = await self._hass.async_add_executor_job(_frame_to_jpeg, frame)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not keep the last live picture", exc_info=True)

    # --- the stream -----------------------------------------------------------

    async def _produce(self) -> None:
        """Send live video or the standby picture for as long as anyone reads."""
        hass = self._hass
        loop = hass.loop
        device_id = self._device_id
        session = self._rtsp.coordinator.live_session(hass, device_id)
        camera = self._camera
        other = 1 - camera
        muxer = TSMuxer()
        start = loop.time()
        frames: asyncio.Queue[tuple[bytes, int]] = asyncio.Queue(maxsize=LIVE_QUEUE)

        def sink(data: bytes, timestamp: int) -> None:
            # Called on the event loop for every frame the dashcam sends.
            # Frames from the other camera stay out.
            if session.current_camera != camera:
                return
            try:
                frames.put_nowait((data, timestamp))
            except asyncio.QueueFull:
                pass

        last_pts = -1

        def emit(unit: bytes, keyframe: bool, pts: int) -> None:
            nonlocal last_pts
            pts = max(pts, last_pts + 1)
            last_pts = pts
            self._send(muxer.mux(unit, pts, keyframe, pts // 90))

        def clock() -> int:
            return int((loop.time() - start) * 90000)

        # The dashcam's own timestamps are only trusted frame to frame. aiortc
        # reads any timestamp that goes backwards (a reordered or retransmitted
        # packet on a moving mobile link) as a 32-bit wrap and moves every
        # later frame ~13 hours ahead; with timestamps forced to increase, one
        # such frame froze the NVR's timeline for the rest of the session.
        prev_ts: int | None = None
        prev_pts = 0
        prev_arrival = 0

        def live_pts(timestamp: int) -> int:
            nonlocal prev_ts, prev_pts, prev_arrival
            arrival = clock()
            if prev_ts is None:
                pts = arrival
            else:
                step = timestamp - prev_ts
                if not 0 < step <= MAX_FRAME_STEP:
                    step = min(max(arrival - prev_arrival, 1), MAX_FRAME_STEP)
                pts = prev_pts + step
                if not arrival - MAX_LAG <= pts <= arrival + MAX_LEAD:
                    pts = arrival
            prev_ts, prev_pts, prev_arrival = timestamp, pts, arrival
            return pts

        units: list[bytes] = []
        claimed = False
        acquiring: asyncio.Future | None = None
        attached: Any = None
        attached_at = 0.0
        retry_at = 0.0
        failures = 0
        waiting_key = True
        last_frame_at = 0.0
        last_reclaim = 0.0
        last_keyframe_at = 0.0
        keyframe_asked_at = 0.0
        had_turn = False
        tick = 0
        next_tick = loop.time()

        def back_off() -> None:
            nonlocal retry_at, failures
            retry_at = loop.time() + RETRY_BACKOFF[min(failures, len(RETRY_BACKOFF) - 1)]
            failures += 1

        async def to_standby() -> None:
            """Back to the standby picture, starting on its keyframe."""
            nonlocal waiting_key, tick, next_tick, units
            _LOGGER.debug("RTSP %s %s: standby picture", device_id, self._name)
            self.live = False
            self._rtsp.async_notify()
            waiting_key = True
            await self._remember_view()
            # Wait for the new picture: on a turn change it is shown for only
            # a few seconds, so the previous one must not stand in for it.
            units = await self._standby_units(wait=True) or units
            tick = 0
            next_tick = loop.time()

        nudges = 0

        async def restart(reason: str) -> None:
            """Get the picture going again -- without reconnecting if possible.

            Every reconnect is announced out loud by the dashcam ("remote
            connection disconnected / connected"). While the WebRTC connection
            is still alive, ask for a keyframe on the same session instead; only a dead connection, or one that stayed
            silent through MAX_NUDGES nudges, is closed and reopened.
            """
            nonlocal nudges
            _LOGGER.debug("RTSP %s %s: %s", device_id, self._name, reason)
            self.last_error = reason
            client = session.client
            if client is not None and client.alive and nudges < MAX_NUDGES:
                nudges += 1
                # (Not a camera re-select: the dashcam's select is a "next
                # camera" toggle, so re-selecting would flip to the wrong one.)
                await client.async_request_keyframe()
                return
            nudges = 0
            await session.async_restart()
            back_off()

        last_kick = self._rtsp.kicked_at(device_id)
        try:
            units = await self._standby_units(wait=True) or []
            while self._readers:
                now = loop.time()
                if (kick := self._rtsp.kicked_at(device_id)) != last_kick:
                    # The dashcam is back after a drop: reconnect now.
                    last_kick = kick
                    retry_at, failures = 0.0, 0
                want = self._rtsp.want_live(device_id)
                # With both cameras on the stream they take turns; a camera
                # somebody is watching in Home Assistant keeps the turn.
                my_turn = want and self._rtsp.active_camera(device_id) == camera
                if my_turn and not had_turn:
                    attached_at = now
                    last_reclaim = 0.0
                had_turn = my_turn

                # Hold the dashcam session while live video is wanted, and
                # (re)open it when there is none — at first, after a failure,
                # or after a restart.
                if want and not claimed:
                    session.add_watcher(self._key)
                    claimed = True
                if not want and claimed and acquiring is None:
                    if self.live:
                        await to_standby()
                    if attached is not None:
                        attached.remove_encoded_sink(sink)
                        attached = None
                    claimed = False
                    await session.async_release(self._key)
                current = session.client
                if (
                    claimed
                    and my_turn
                    and acquiring is None
                    and (current is None or not current.alive)
                    and now >= retry_at
                ):
                    # Open it on our camera, unless somebody is watching the
                    # other one: then take it as it is and wait.
                    target = None if session.watching_camera(other, self._key) else camera
                    acquiring = asyncio.ensure_future(session.async_acquire(target))
                if acquiring is not None and acquiring.done():
                    result = None
                    if not acquiring.cancelled() and acquiring.exception() is None:
                        result = acquiring.result()
                    acquiring = None
                    _LOGGER.debug(
                        "RTSP %s %s: session %s", device_id, self._name,
                        "up" if result else "failed",
                    )
                    if result is None:
                        self.last_error = "Could not reach the dashcam"
                        back_off()
                    elif result.encoded_tap is False:
                        self.last_error = (
                            "This aiortc version cannot pass the video through"
                        )

                # Follow the session's connection: a restart replaces it.
                client = session.client if claimed and acquiring is None else None
                if client is not attached:
                    if attached is not None:
                        attached.remove_encoded_sink(sink)
                    if client is not None:
                        client.add_encoded_sink(sink)
                    attached = client
                    attached_at = now
                    waiting_key = True
                    while not frames.empty():
                        frames.get_nowait()

                # On our turn, put the dashcam on our camera — unless somebody
                # is watching the other one in Home Assistant.
                if (
                    my_turn
                    and client is not None
                    and session.current_camera != camera
                    and now - last_reclaim > RECLAIM_SECONDS
                ):
                    last_reclaim = now
                    attached_at = now
                    if not session.watching_camera(other, self._key):
                        hass.async_create_task(session.async_acquire(camera))

                on_our_camera = client is not None and session.current_camera == camera

                # Ask for a keyframe: at once when waiting for one, then every
                # couple of seconds while live.
                if (
                    my_turn
                    and on_our_camera
                    and now - keyframe_asked_at
                    > (KEYFRAME_RETRY_SECONDS if waiting_key else KEYFRAME_INTERVAL_SECONDS)
                    and (waiting_key or now - last_keyframe_at > KEYFRAME_INTERVAL_SECONDS)
                ):
                    keyframe_asked_at = now
                    hass.async_create_task(client.async_request_keyframe())

                # Next live frame, or time for the next standby frame.
                wait = 0.25 if self.live else max(0.0, min(0.25, next_tick - now))
                try:
                    data, timestamp = await asyncio.wait_for(frames.get(), wait)
                except TimeoutError:
                    data = None
                now = loop.time()

                if data is not None and my_turn and client is not None:
                    keyframe = is_keyframe(data)
                    if keyframe:
                        last_keyframe_at = now
                    if waiting_key:
                        if not keyframe:
                            continue
                        waiting_key = False
                        prev_ts = None
                        if (profile := sps_profile(data)) is not None:
                            self._profile = profile
                        if not self.live:
                            _LOGGER.debug("RTSP %s %s: live video", device_id, self._name)
                            nudges = 0
                            self.live = True
                            self.last_error = None
                            failures = 0
                            self._rtsp.async_notify()
                    emit(data, keyframe, live_pts(timestamp))
                    self._last_view = client.latest_frame or self._last_view
                    last_frame_at = now
                    continue

                if self.live and not my_turn:
                    # The other camera's turn (or no longer wanted).
                    await to_standby()
                elif self.live and now - last_frame_at > STALL_SECONDS:
                    await to_standby()
                    if on_our_camera:
                        # Our camera, yet nothing for a while: the session is
                        # dead even if WebRTC has not noticed.
                        await restart("The live video stalled; reconnecting")
                elif (
                    my_turn
                    and not self.live
                    and on_our_camera
                    and now - attached_at > FIRST_KEYFRAME_SECONDS
                ):
                    attached_at = now
                    await restart("The dashcam sent no picture; reconnecting")

                if not self.live and now >= next_tick and units:
                    index = tick % len(units)
                    if index == 0:
                        # Only swap pictures at a keyframe.
                        units = await self._standby_units(wait=False) or units
                    emit(units[index], index == 0, clock())
                    tick += 1
                    next_tick += 1 / STANDBY_FPS
                    if next_tick < now:
                        next_tick = now + 1 / STANDBY_FPS
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            self.last_error = str(err)
            _LOGGER.exception("RTSP feed for %s %s failed", device_id, self._name)
        finally:
            if acquiring is not None:
                acquiring.cancel()
            if attached is not None:
                attached.remove_encoded_sink(sink)
            if claimed:
                hass.async_create_task(session.async_release(self._key))
            self.live = False
            for queue in list(self._readers):
                queue.put_nowait(None)
            self._readers.clear()
            self._rtsp.async_notify()


class ProofGo2rtc:
    """A go2rtc process of our own, restarted if it dies."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._config = Path(tempfile.gettempdir()) / "proof_plus_go2rtc" / f"{entry_id}.yaml"
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.output: deque[str] = deque(maxlen=40)
        self.error: str | None = None

    @property
    def running(self) -> bool:
        """Whether the process is up."""
        return self._process is not None and self._process.returncode is None

    def _kill_strays(self) -> int:
        """End a go2rtc of ours left behind by an earlier run."""
        marker = str(self._config).encode()
        proc = Path("/proc")
        killed = 0
        if not proc.is_dir():
            return killed
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if marker in cmdline and b"go2rtc" in cmdline:
                try:
                    os.kill(int(entry.name), signal.SIGTERM)
                    killed += 1
                except OSError:
                    pass
        return killed

    async def async_kill_strays(self) -> None:
        """End a go2rtc of ours left behind, so its port is free again."""
        if await self._hass.async_add_executor_job(self._kill_strays):
            await asyncio.sleep(1)

    async def async_start(self, config: dict[str, Any]) -> None:
        """Write the configuration and start the process."""
        binary = shutil.which("go2rtc") or (
            GO2RTC_BINARY if os.path.exists(GO2RTC_BINARY) else None
        )
        if binary is None:
            raise RtspError(
                "go2rtc was not found (Home Assistant ships it from 2024.11)"
            )

        def _prepare() -> None:
            self._config.parent.mkdir(parents=True, exist_ok=True)
            # YAML is a superset of JSON, so go2rtc reads this as it is.
            self._config.write_text(json.dumps(config, indent=1), encoding="utf-8")
            os.chmod(self._config, 0o600)
            self._kill_strays()

        await self._hass.async_add_executor_job(_prepare)
        self._stopping = False
        self.error = None
        self._task = self._hass.async_create_background_task(
            self._run(binary), f"{DOMAIN} go2rtc"
        )

    async def _run(self, binary: str) -> None:
        delay = GO2RTC_RESTART_SECONDS
        while not self._stopping:
            started = time.monotonic()
            try:
                self._process = await asyncio.create_subprocess_exec(
                    binary,
                    "-c",
                    str(self._config),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except OSError as err:
                self.error = f"Could not start go2rtc: {err}"
                _LOGGER.error(self.error)
                return
            assert self._process.stdout is not None
            async for raw in self._process.stdout:
                line = raw.decode(errors="replace").rstrip()
                self.output.append(line)
                if " ERR " in line or " FTL " in line:
                    self.error = line
                    _LOGGER.warning("go2rtc: %s", line)
            code = await self._process.wait()
            if self._stopping:
                return
            self.error = f"go2rtc exited with code {code}"
            _LOGGER.warning("%s; restarting in %s s", self.error, delay)
            if time.monotonic() - started > 60:
                delay = GO2RTC_RESTART_SECONDS
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

    async def async_stop(self) -> None:
        """Stop the process."""
        self._stopping = True
        process = self._process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._process = None


class ProofRtsp:
    """The RTSP output for one account: mode, triggers, feeds and go2rtc."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, coordinator: ProofCoordinator
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        options = entry.options
        self.mode: str = options.get(CONF_RTSP_MODE, DEFAULT_RTSP_MODE)
        self.port: int = int(options.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT))
        self._camera_mode: str = options.get(CONF_RTSP_CAMERA, DEFAULT_RTSP_CAMERA)
        self._alternate = int(options.get(CONF_RTSP_ALTERNATE, DEFAULT_RTSP_ALTERNATE))
        # Per dashcam with both cameras on: whose turn it is, and since when.
        self._turns: dict[str, tuple[int, float]] = {}
        self._username: str = options.get(CONF_RTSP_USERNAME) or ""
        self._password: str = options.get(CONF_RTSP_PASSWORD) or ""
        self._alerts: set[str] = set(options.get(CONF_RTSP_ALERTS, DEFAULT_RTSP_ALERTS))
        self._alert_seconds = 60 * int(
            options.get(CONF_RTSP_ALERT_MINUTES, DEFAULT_RTSP_ALERT_MINUTES)
        )
        self._driving: bool = options.get(CONF_RTSP_DRIVING, DEFAULT_RTSP_DRIVING)
        self._grace_seconds = 60 * int(
            options.get(CONF_RTSP_DRIVING_GRACE, DEFAULT_RTSP_DRIVING_GRACE)
        )
        # Per dashcam: why live video is wanted, and until when (None = until
        # that reason ends by itself, e.g. the ignition going off).
        self._windows: dict[str, dict[str, float | None]] = {}
        self._ignition: dict[str, bool] = {}
        self._polled_ignition: dict[str, bool] = {}
        # Auto-renew: when a dashcam reports again after a drop (or its ignition
        # comes on), the feeds reconnect at once instead of sitting out their
        # back-off, which reaches 5 min after a few failed attempts.
        self._kicks: dict[str, float] = {}
        self._reports: dict[str, float] = {}
        self._feeds: dict[tuple[str, int], ProofRtspFeed] = {}
        self._server: asyncio.Server | None = None
        self._go2rtc = ProofGo2rtc(hass, entry.entry_id)
        # go2rtc proves itself to the feed with this; the feed only listens on
        # localhost, but other add-ons share that.
        self._key = secrets.token_urlsafe(18)
        self._unsubs: list[Any] = []
        self._expiry: Any = None
        self.host: str | None = None
        self.error: str | None = None
        self.running = False

    # --- lifecycle --------------------------------------------------------------

    async def async_setup(self) -> None:
        """Start listening for triggers, and the server unless RTSP is off."""
        self._unsubs.append(self.hass.bus.async_listen(EVENT_MESSAGE, self._on_message))
        self._unsubs.append(self.coordinator.async_add_listener(self._on_update))
        self._on_update()
        try:
            from homeassistant.components import network

            self.host = await network.async_get_source_ip(self.hass)
        except Exception:  # noqa: BLE001
            self.host = None
        if self.mode != RTSP_MODE_OFF:
            await self._async_start_server()

    async def async_shutdown(self) -> None:
        """Stop everything."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._expiry is not None:
            self._expiry()
            self._expiry = None
        await self._async_stop_server()

    async def async_set_mode(self, mode: str) -> None:
        """Switch mode on the fly, and remember it in the entry's options."""
        if mode == self.mode:
            return
        self.mode = mode
        if self.entry.options.get(CONF_RTSP_MODE) != mode:
            # The update listener sees an option that is applied on the fly
            # and does not reload the entry for it.
            self.hass.config_entries.async_update_entry(
                self.entry, options={**self.entry.options, CONF_RTSP_MODE: mode}
            )
        if mode == RTSP_MODE_OFF:
            await self._async_stop_server()
        else:
            await self._async_start_server()
        self.async_notify()

    async def _async_start_server(self) -> None:
        if self.running:
            return
        try:
            await self._go2rtc.async_kill_strays()
            if not await self.hass.async_add_executor_job(_port_free, self.port):
                raise RtspError(f"Port {self.port} is already in use")
            api_port = self.port + GO2RTC_API_OFFSET
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
            feed_port = self._server.sockets[0].getsockname()[1]
            rtsp: dict[str, Any] = {"listen": f":{self.port}", "default_query": "video"}
            if self._username:
                rtsp.update(username=self._username, password=self._password)
            config = {
                "api": {"listen": f"127.0.0.1:{api_port}"},
                "rtsp": rtsp,
                "webrtc": {"listen": ""},
                "srtp": {"listen": ""},
                "rtmp": {"listen": ""},
                "log": {"format": "text", "level": "info"},
                "streams": {
                    self.stream_name(device_id, camera): (
                        f"http://127.0.0.1:{feed_port}/feed/{device_id}/{camera}"
                        f"?key={self._key}"
                    )
                    for device_id in self.coordinator.data
                    for camera in self.cameras(device_id)
                },
            }
            await self._go2rtc.async_start(config)
        except (RtspError, OSError) as err:
            self.error = str(err)
            _LOGGER.error("RTSP output not started: %s", err)
            if self._server is not None:
                self._server.close()
                self._server = None
            self.async_notify()
            return
        self.error = None
        self.running = True
        self.async_notify()

    async def _async_stop_server(self) -> None:
        for feed in self._feeds.values():
            await feed.async_stop()
        self._feeds.clear()
        await self._go2rtc.async_stop()
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 5)
            except TimeoutError:
                pass
            self._server = None
        self.running = False

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Serve one feed request from go2rtc."""
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            parts = request.split(b"\r\n", 1)[0].decode(errors="replace").split(" ")
            target = urlsplit(parts[1] if len(parts) > 1 else "")
            segments = target.path.strip("/").split("/")
            key = (parse_qs(target.query).get("key") or [""])[0]
            device_id = camera = None
            if len(segments) == 3 and segments[0] == "feed" and segments[2].isdigit():
                device_id, camera = segments[1], int(segments[2])
            if (
                device_id not in self.coordinator.data
                or camera not in self.cameras(device_id)
                or not secrets.compare_digest(key, self._key)
            ):
                writer.write(
                    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n"
                    b"Connection: close\r\n\r\n"
                )
                await writer.drain()
                return
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            await self.feed(device_id, camera).async_serve(writer)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    # --- what the stream shows --------------------------------------------------

    def feed(self, device_id: str, camera: int) -> ProofRtspFeed:
        """The feed for one camera of a dashcam, created on first use."""
        if (device_id, camera) not in self._feeds:
            self._feeds[(device_id, camera)] = ProofRtspFeed(self, device_id, camera)
        return self._feeds[(device_id, camera)]

    @property
    def go2rtc_log(self) -> list[str]:
        """The last lines go2rtc printed (for diagnostics)."""
        return list(self._go2rtc.output)

    def _feeds_of(self, device_id: str) -> list[ProofRtspFeed]:
        return [
            feed
            for camera in self.cameras(device_id)
            if (feed := self._feeds.get((device_id, camera))) is not None
        ]

    def is_live(self, device_id: str) -> bool:
        """Whether live video (not the standby picture) is going out."""
        return any(feed.live for feed in self._feeds_of(device_id))

    def has_rear(self, device_id: str) -> bool:
        """Whether the dashcam has a rear camera."""
        device = self.coordinator.data.get(device_id) or {}
        return bool(((device.get("stats") or {}).get("caps") or {}).get("bcamera"))

    def cameras(self, device_id: str) -> list[int]:
        """The cameras that get a stream (0 = front, 1 = rear)."""
        if not self.has_rear(device_id):
            return [0]
        return {"front": [0], "rear": [1]}.get(self._camera_mode, [0, 1])

    def stream_name(self, device_id: str, camera: int) -> str:
        """The RTSP path an NVR asks for.

        The first stream keeps the plain name, so switching between one
        camera and both does not move the NVR channel already set up.
        """
        if camera == self.cameras(device_id)[0]:
            return f"proof_{device_id}"
        return f"proof_{device_id}_rear"

    def active_camera(self, device_id: str) -> int | None:
        """Which camera's stream gets the live video right now.

        A camera somebody is watching in Home Assistant keeps it. Otherwise,
        with both streams being pulled, the cameras take turns.
        """
        pulled = [feed._camera for feed in self._feeds_of(device_id) if feed.readers]
        if len(pulled) <= 1:
            return pulled[0] if pulled else None
        viewer = self.coordinator.live_session(self.hass, device_id).viewer_camera()
        if viewer in pulled:
            return viewer
        now = time.monotonic()
        camera, since = self._turns.get(device_id, (pulled[0], now))
        if now - since >= self._alternate:
            camera, since = pulled[(pulled.index(camera) + 1) % len(pulled)], now
        self._turns[device_id] = (camera, since)
        return camera

    def cached_still(self, device_id: str, camera: int) -> bytes | None:
        """The latest still Home Assistant already holds for that camera."""
        return (self.coordinator.stills.get(device_id) or {}).get(camera)

    def standby_caption(self, device_id: str, camera: int) -> str:
        """The line written on a camera's standby picture."""
        if self.want_live(device_id) and len(self.cameras(device_id)) > 1:
            if self.active_camera(device_id) not in (camera, None):
                other = "front" if camera else "rear"
                return f"Paused - the dashcam is showing the {other} camera"
        if self.mode == RTSP_MODE_ALWAYS:
            return "Waiting for the dashcam - reconnecting"
        return "Standby - live video starts when triggered"

    def want_live(self, device_id: str) -> bool:
        """Whether the stream should be carrying live video right now."""
        if self.mode == RTSP_MODE_ALWAYS:
            return True
        if self.mode != RTSP_MODE_TRIGGER:
            return False
        return bool(self.reasons(device_id))

    def reasons(self, device_id: str) -> dict[str, float | None]:
        """The triggers currently active for a dashcam, pruning expired ones."""
        windows = self._windows.get(device_id) or {}
        now = time.time()
        for reason in [r for r, until in windows.items() if until is not None and until <= now]:
            del windows[reason]
        return dict(windows)

    def live_until(self, device_id: str) -> datetime | None:
        """When the triggers run out, if they all have an end."""
        reasons = self.reasons(device_id)
        if not reasons or any(until is None for until in reasons.values()):
            return None
        return dt_util.utc_from_timestamp(max(reasons.values()))  # type: ignore[type-var]

    def stream_url(self, device_id: str, camera: int | None = None) -> str:
        """The URL to give the NVR for a camera (the first stream by default)."""
        if camera is None:
            camera = self.cameras(device_id)[0]
        host = self.host or "<home-assistant-ip>"
        return f"rtsp://{host}:{self.port}/{self.stream_name(device_id, camera)}"

    # --- triggers ---------------------------------------------------------------

    def manual(self, device_id: str) -> bool:
        """Whether the RTSP live switch holds live video on."""
        return "manual" in (self._windows.get(device_id) or {})

    @callback
    def set_manual(self, device_id: str, on: bool) -> None:
        """The RTSP live switch."""
        windows = self._windows.setdefault(device_id, {})
        if on:
            windows["manual"] = None
        else:
            windows.pop("manual", None)
        self._changed()

    @callback
    def trigger(self, device_id: str, minutes: float | None = None) -> None:
        """Live video for a while (the proof_plus.rtsp_trigger action)."""
        seconds = 60 * minutes if minutes else self._alert_seconds
        self._extend(device_id, "action", seconds)

    @callback
    def stop(self, device_id: str) -> None:
        """End every trigger now (the next ignition change can start one)."""
        self._windows.pop(device_id, None)
        self._changed()

    @callback
    def _extend(self, device_id: str, reason: str, seconds: float) -> None:
        windows = self._windows.setdefault(device_id, {})
        until = time.time() + seconds
        current = windows.get(reason, 0.0)
        if reason in windows and current is None:
            return
        windows[reason] = max(until, current or 0.0)
        self._changed()

    @callback
    def _on_message(self, event: Event) -> None:
        """An alert arrived from the cloud."""
        device_id = event.data.get("device_id")
        if device_id not in self.coordinator.data:
            return
        kind = str(event.data.get("type") or "").lower()
        if kind == "acc":
            topic = str(event.data.get("topic") or "").upper()
            self._set_ignition(device_id, topic.endswith("ON"))
            return
        if not self._alert_wanted(kind):
            return
        # The cloud replays undelivered alerts at login; an old one is no
        # reason to start streaming now.
        when = dt_util.parse_datetime(str(event.data.get("time") or ""))
        if when is not None and (dt_util.utcnow() - when).total_seconds() > self._alert_seconds:
            return
        self._extend(device_id, "alert", self._alert_seconds)

    def _alert_wanted(self, kind: str) -> bool:
        for chosen in self._alerts:
            if chosen == "overspeed" and "speed" in kind:
                return True
            if kind.startswith(chosen):
                return True
        return False

    def kicked_at(self, device_id: str) -> float:
        return self._kicks.get(device_id, 0.0)

    @callback
    def _kick(self, device_id: str) -> None:
        self._kicks[device_id] = time.monotonic()

    @callback
    def _on_update(self) -> None:
        """Follow the ignition as polled, in case its alert was missed.

        Only a change in the polled value counts. This runs whenever any
        entity is refreshed, and the polled value lags the ignition alert by
        up to a poll interval — acting on it every time would undo an "ACC ON"
        alert with the stale "off" from before it.
        """
        for device_id, device in (self.coordinator.data or {}).items():
            # Back after a drop: a report after more than a minute of silence.
            report = last_report(device)
            before = self._reports.get(device_id)
            if report:
                self._reports[device_id] = report
                if before and report - before > DROP_SECONDS:
                    self._kick(device_id)
            acc = (device.get("stats") or {}).get("acc")
            if acc is None or self._polled_ignition.get(device_id) == bool(acc):
                continue
            self._polled_ignition[device_id] = bool(acc)
            self._set_ignition(device_id, bool(acc))

    @callback
    def _set_ignition(self, device_id: str, on: bool) -> None:
        before = self._ignition.get(device_id)
        self._ignition[device_id] = on
        if on and before is False:
            self._kick(device_id)  # a new drive: no leftover back-off
        if not self._driving or before == on:
            return
        windows = self._windows.setdefault(device_id, {})
        if on:
            windows["driving"] = None
        elif before:
            windows["driving"] = time.time() + self._grace_seconds
        else:
            return
        self._changed()

    @callback
    def _changed(self) -> None:
        """Re-arm the expiry timer and refresh the entities."""
        if self._expiry is not None:
            self._expiry()
            self._expiry = None
        ends = [
            until
            for windows in self._windows.values()
            for until in windows.values()
            if until is not None
        ]
        if ends:
            delay = max(0.0, min(ends) - time.time()) + 0.5

            @callback
            def _expired(_now: Any) -> None:
                self._expiry = None
                self._changed()

            self._expiry = async_call_later(self.hass, delay, _expired)
        self.async_notify()

    @callback
    def async_notify(self) -> None:
        """Let the entities show the new state."""
        self.coordinator.async_update_listeners()

    def state(self, device_id: str) -> dict[str, Any]:
        """Everything an entity shows about the stream."""
        feeds = self._feeds_of(device_id)
        cameras = self.cameras(device_id)
        until = self.live_until(device_id)
        enabled = self.mode != RTSP_MODE_OFF
        live = next((feed for feed in feeds if feed.live), None)
        return {
            "mode": self.mode,
            "url": self.stream_url(device_id, cameras[0]) if enabled else None,
            "rear_url": (
                self.stream_url(device_id, 1) if enabled and len(cameras) > 1 else None
            ),
            "cameras": ["rear" if camera else "front" for camera in cameras],
            "live_camera": live._name if live else None,
            "live_wanted": self.want_live(device_id),
            "triggers": sorted(self.reasons(device_id)) if self.mode == RTSP_MODE_TRIGGER else [],
            "live_until": until.isoformat() if until else None,
            "nvr_connected": any(feed.readers for feed in feeds),
            "error": self.error
            or self._go2rtc.error
            or next((feed.last_error for feed in feeds if feed.last_error), None),
        }
