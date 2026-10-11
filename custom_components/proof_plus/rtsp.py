"""RTSP output, so an NVR can record the dashcam.

    dashcam --WebRTC--> ProofWebRTCClient --encoded H.264--> ProofRtspFeed
      --MPEG-TS over http://127.0.0.1:<feed>/feed/<device>--> go2rtc
      --rtsp://<home-assistant>:<port>/proof_<device>--> NVR

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
camera entities and the settings controls. A viewer who switches it to the
other camera wins for as long as they watch; the feed shows the standby
picture meanwhile and takes the camera back once they leave.
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
    CONF_RTSP_CAMERA,
    CONF_RTSP_DRIVING,
    CONF_RTSP_DRIVING_GRACE,
    CONF_RTSP_MODE,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_PORT,
    CONF_RTSP_USERNAME,
    DEFAULT_RTSP_ALERT_MINUTES,
    DEFAULT_RTSP_ALERTS,
    DEFAULT_RTSP_DRIVING,
    DEFAULT_RTSP_DRIVING_GRACE,
    DEFAULT_RTSP_MODE,
    DEFAULT_RTSP_PORT,
    DOMAIN,
    RTSP_MODE_ALWAYS,
    RTSP_MODE_OFF,
    RTSP_MODE_TRIGGER,
)
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
# A fresh session that has not produced a keyframe by then is restarted.
FIRST_KEYFRAME_SECONDS = 30
# The dashcam only sends a keyframe when asked. Ask this often while waiting
# for one, and while live so the NVR gets a keyframe every couple of seconds
# (to start recording segments, and to heal after a lost packet).
KEYFRAME_RETRY_SECONDS = 1
KEYFRAME_INTERVAL_SECONDS = 2
# Waits between attempts to reach a dashcam that would not answer.
RETRY_BACKOFF = (15, 30, 60, 120, 300)
# How often to check whether the feed's camera can be taken back.
RECLAIM_SECONDS = 5
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


def stream_name(device_id: str) -> str:
    """The RTSP path an NVR asks for."""
    return f"proof_{device_id}"


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
    """The stream go2rtc pulls for one dashcam."""

    def __init__(self, rtsp: ProofRtsp, device_id: str) -> None:
        self._rtsp = rtsp
        self._hass = rtsp.hass
        self._device_id = device_id
        self._key = f"{device_id}:{SESSION_KEY}"
        self._readers: set[asyncio.Queue[bytes | None]] = set()
        self._task: asyncio.Task | None = None
        # True while live video (not the standby picture) is going out.
        self.live = False
        self.last_error: str | None = None
        # Per camera: what the standby picture is built from, and the build.
        self._pictures: dict[int, bytes] = {}
        self._sizes: dict[int, tuple[int, int]] = {}
        self._profiles: dict[int, tuple[str, str]] = {}
        self._standby: dict[int, tuple[tuple, list[bytes]]] = {}
        self._building: dict[int, asyncio.Task] = {}

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
                self._produce(), f"{DOMAIN} rtsp feed {self._device_id}"
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
        for task in self._building.values():
            task.cancel()

    # --- standby picture ------------------------------------------------------

    def _standby_signature(self, camera: int) -> tuple:
        still = self._pictures.get(camera) or self._rtsp.cached_still(
            self._device_id, camera
        )
        return (
            id(still) if still else None,
            self._sizes.get(camera),
            self._profiles.get(camera, ("main", "3.1")),
            self._rtsp.standby_caption(),
        )

    def _start_standby_build(self, camera: int) -> asyncio.Task:
        signature = self._standby_signature(camera)
        still = self._pictures.get(camera) or self._rtsp.cached_still(
            self._device_id, camera
        )
        profile, level = self._profiles.get(camera, ("main", "3.1"))

        async def _build() -> list[bytes]:
            units = await self._hass.async_add_executor_job(
                _build_standby,
                still,
                self._sizes.get(camera),
                signature[3],
                profile,
                level,
            )
            self._standby[camera] = (signature, units)
            return units

        task = self._hass.async_create_background_task(
            _build(), f"{DOMAIN} rtsp standby {self._device_id}"
        )
        self._building[camera] = task
        return task

    async def _standby_units(self, camera: int, wait: bool) -> list[bytes] | None:
        """The standby picture's access units, rebuilding when it has changed.

        With ``wait`` False a rebuild runs in the background and the previous
        picture is returned meanwhile, so the stream never pauses for it.
        """
        current = self._standby.get(camera)
        if current is not None and current[0] == self._standby_signature(camera):
            return current[1]
        task = self._building.get(camera)
        if task is None or task.done():
            task = self._start_standby_build(camera)
        if wait or current is None:
            try:
                return await task
            except Exception as err:  # noqa: BLE001
                self.last_error = f"Could not build the standby picture: {err}"
                _LOGGER.error(self.last_error)
                raise
        return current[1]

    async def _remember_view(self, camera: int, client: Any) -> None:
        """Make the last live picture the next standby picture."""
        frame = getattr(client, "latest_frame", None)
        if frame is None:
            return
        self._sizes[camera] = (frame.width, frame.height)
        try:
            self._pictures[camera] = await self._hass.async_add_executor_job(
                _frame_to_jpeg, frame
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not keep the last live picture", exc_info=True)

    # --- the stream -----------------------------------------------------------

    async def _produce(self) -> None:
        """Send live video or the standby picture for as long as anyone reads."""
        hass = self._hass
        loop = hass.loop
        session = self._rtsp.coordinator.live_session(hass, self._device_id)
        camera = self._rtsp.camera_index(self._device_id)
        other = 1 - camera
        muxer = TSMuxer()
        start = loop.time()
        frames: asyncio.Queue[tuple[bytes, int]] = asyncio.Queue(maxsize=LIVE_QUEUE)

        def sink(data: bytes, timestamp: int) -> None:
            # Called on the event loop for every frame the dashcam sends.
            # Frames from the other camera (a viewer switched it) stay out.
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

        claimed = False
        acquiring: asyncio.Future | None = None
        attached: Any = None
        attached_at = 0.0
        retry_at = 0.0
        failures = 0
        waiting_key = True
        offset = 0
        last_frame_at = 0.0
        last_reclaim = 0.0
        last_keyframe_at = 0.0
        keyframe_asked_at = 0.0
        tick = 0
        next_tick = loop.time()

        def back_off() -> None:
            nonlocal retry_at, failures
            retry_at = loop.time() + RETRY_BACKOFF[min(failures, len(RETRY_BACKOFF) - 1)]
            failures += 1

        async def to_standby() -> None:
            """Back to the standby picture, starting on its keyframe."""
            nonlocal waiting_key, tick, next_tick
            _LOGGER.debug("RTSP %s: standby picture", self._device_id)
            self.live = False
            self._rtsp.async_notify()
            waiting_key = True
            tick = 0
            next_tick = loop.time()
            if attached is not None:
                await self._remember_view(camera, attached)

        async def restart(reason: str) -> None:
            """Drop a session that delivers nothing; try again after a pause."""
            _LOGGER.debug("RTSP %s: %s", self._device_id, reason)
            self.last_error = reason
            await session.async_restart()
            back_off()

        try:
            units = await self._standby_units(camera, wait=True)
            while self._readers:
                now = loop.time()
                want = self._rtsp.want_live(self._device_id)

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
                    and acquiring is None
                    and (current is None or not current.alive)
                    and now >= retry_at
                ):
                    # Open it on our camera, unless somebody is watching the
                    # other one: then take it as it is and wait our turn.
                    target = None if session.watching_camera(other, self._key) else camera
                    acquiring = asyncio.ensure_future(session.async_acquire(target))
                if acquiring is not None and acquiring.done():
                    result = None
                    if not acquiring.cancelled() and acquiring.exception() is None:
                        result = acquiring.result()
                    acquiring = None
                    _LOGGER.debug("RTSP %s: session %s", self._device_id, "up" if result else "failed")
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

                # Take the camera back once nobody is watching the other one.
                if (
                    client is not None
                    and session.current_camera != camera
                    and now - last_reclaim > RECLAIM_SECONDS
                ):
                    last_reclaim = now
                    attached_at = now
                    if not session.watching_camera(other, self._key):
                        hass.async_create_task(session.async_acquire(camera))

                # Ask for a keyframe: at once when waiting for one, then every
                # couple of seconds while live.
                if (
                    client is not None
                    and want
                    and session.current_camera == camera
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

                if data is not None and want and client is not None:
                    keyframe = is_keyframe(data)
                    if keyframe:
                        last_keyframe_at = now
                    if waiting_key:
                        if not keyframe:
                            continue
                        waiting_key = False
                        offset = clock() - timestamp
                        if (profile := sps_profile(data)) is not None:
                            self._profiles[camera] = profile
                        if not self.live:
                            _LOGGER.debug("RTSP %s: live video", self._device_id)
                            self.live = True
                            self.last_error = None
                            failures = 0
                            self._rtsp.async_notify()
                    emit(data, keyframe, timestamp + offset)
                    last_frame_at = now
                    continue

                on_our_camera = client is not None and session.current_camera == camera
                if self.live and now - last_frame_at > STALL_SECONDS:
                    await to_standby()
                    if on_our_camera:
                        # Same camera, yet nothing for a while: the session
                        # is dead even if WebRTC has not noticed.
                        await restart("The live video stalled; reconnecting")
                elif (
                    want
                    and not self.live
                    and on_our_camera
                    and now - attached_at > FIRST_KEYFRAME_SECONDS
                ):
                    attached_at = now
                    await restart("The dashcam sent no picture; reconnecting")

                if not self.live and now >= next_tick:
                    index = tick % len(units)
                    if index == 0:
                        # Only swap pictures at a keyframe.
                        units = await self._standby_units(camera, wait=False) or units
                    emit(units[index], index == 0, clock())
                    tick += 1
                    next_tick += 1 / STANDBY_FPS
                    if next_tick < now:
                        next_tick = now + 1 / STANDBY_FPS
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            self.last_error = str(err)
            _LOGGER.exception("RTSP feed for %s failed", self._device_id)
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
        self._camera = 1 if options.get(CONF_RTSP_CAMERA) == "rear" else 0
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
        self._feeds: dict[str, ProofRtspFeed] = {}
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
                    stream_name(device_id): (
                        f"http://127.0.0.1:{feed_port}/feed/{device_id}?key={self._key}"
                    )
                    for device_id in self.coordinator.data
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
            device_id = segments[1] if len(segments) == 2 and segments[0] == "feed" else None
            if (
                device_id not in self.coordinator.data
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
            await self.feed(device_id).async_serve(writer)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    # --- what the stream shows --------------------------------------------------

    def feed(self, device_id: str) -> ProofRtspFeed:
        """The feed for a dashcam, created on first use."""
        if device_id not in self._feeds:
            self._feeds[device_id] = ProofRtspFeed(self, device_id)
        return self._feeds[device_id]

    @property
    def go2rtc_log(self) -> list[str]:
        """The last lines go2rtc printed (for diagnostics)."""
        return list(self._go2rtc.output)

    def is_live(self, device_id: str) -> bool:
        """Whether live video (not the standby picture) is going out."""
        feed = self._feeds.get(device_id)
        return bool(feed and feed.live)

    def camera_index(self, device_id: str) -> int:
        """Which camera the stream carries (front unless rear chosen and present)."""
        device = self.coordinator.data.get(device_id) or {}
        has_rear = bool(((device.get("stats") or {}).get("caps") or {}).get("bcamera"))
        return self._camera if has_rear else 0

    def cached_still(self, device_id: str, camera: int) -> bytes | None:
        """The latest still Home Assistant already holds for that camera."""
        return (self.coordinator.stills.get(device_id) or {}).get(camera)

    def standby_caption(self) -> str:
        """The line written on the standby picture."""
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

    def stream_url(self, device_id: str) -> str:
        """The URL to give the NVR."""
        host = self.host or "<home-assistant-ip>"
        return f"rtsp://{host}:{self.port}/{stream_name(device_id)}"

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

    @callback
    def _on_update(self) -> None:
        """Follow the ignition as polled, in case its alert was missed.

        Only a change in the polled value counts. This runs whenever any
        entity is refreshed, and the polled value lags the ignition alert by
        up to a poll interval — acting on it every time would undo an "ACC ON"
        alert with the stale "off" from before it.
        """
        for device_id, device in (self.coordinator.data or {}).items():
            acc = (device.get("stats") or {}).get("acc")
            if acc is None or self._polled_ignition.get(device_id) == bool(acc):
                continue
            self._polled_ignition[device_id] = bool(acc)
            self._set_ignition(device_id, bool(acc))

    @callback
    def _set_ignition(self, device_id: str, on: bool) -> None:
        before = self._ignition.get(device_id)
        self._ignition[device_id] = on
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
        feed = self._feeds.get(device_id)
        until = self.live_until(device_id)
        return {
            "mode": self.mode,
            "url": self.stream_url(device_id) if self.mode != RTSP_MODE_OFF else None,
            "camera": "rear" if self.camera_index(device_id) else "front",
            "live_wanted": self.want_live(device_id),
            "triggers": sorted(self.reasons(device_id)) if self.mode == RTSP_MODE_TRIGGER else [],
            "live_until": until.isoformat() if until else None,
            "nvr_connected": bool(feed and feed.readers),
            "error": self.error or self._go2rtc.error or (feed.last_error if feed else None),
        }
