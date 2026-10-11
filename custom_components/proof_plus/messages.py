"""Receive the alerts the Proof cloud pushes for a dashcam.

The mobile app's Messages screen is not fetched over HTTP — the cloud pushes
each alert down the imclient WebSocket as an event, e.g.::

    {"msg": "Your car is with ACC ON.", "topic": "ACC ON", "type": "acc",
     "loc": [32.0, 34.8], "time": 1767225600000}

Holding that socket open costs nothing on the dashcam: it is a link between
Home Assistant and the cloud, and it never wakes the camera.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import ProofCoordinator

_LOGGER = logging.getLogger(__name__)

CMD_PING, CMD_ACK, CMD_LOGIN, CMD_MSG, CMD_REQUEST = 0, 1, 2, 4, 5
MSG_EVENT = 7

HEARTBEAT_INTERVAL = 30
RECONNECT_DELAY = 30
MAX_MESSAGES = 50

# Fired on the Home Assistant bus for every alert, so automations can use them.
EVENT_MESSAGE = f"{DOMAIN}_message"


class ProofMessageListener:
    """Keeps the alert socket open and records what arrives."""

    def __init__(
        self, hass: HomeAssistant, coordinator: ProofCoordinator
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._task: asyncio.Task | None = None
        self._closed = False
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._seq = 0
        # Counters, surfaced as sensor attributes. This box keeps no log file,
        # so they are the only way to tell a quiet socket from a dead one.
        self.connected = False
        self.connected_since: str | None = None
        self.frames = 0
        self.replies = 0
        self.alerts = 0
        self.last_frame: str | None = None
        self.session_id: str | None = None
        # The cloud queues undelivered alerts per client id and replays them to
        # a client it has not seen before — that is how a freshly installed app
        # arrives with its list already filled. Derive ours from the config
        # entry so it stays the same across restarts.
        self._pid = f"ha{coordinator.config_entry.entry_id[:14]}"
        # Alert ids already recorded, so a replay cannot duplicate the list.
        self._seen: set[str] = set()

    def start(self) -> None:
        """Begin listening in the background."""
        # Remember what was already recorded before this restart, so a replay
        # of the cloud's history does not duplicate it.
        for stored in self._coordinator.messages.values():
            self._seen.update(
                message["id"] for message in stored if message.get("id")
            )
        self._task = self._hass.async_create_background_task(
            self._run(), f"{DOMAIN}_messages"
        )

    async def async_stop(self) -> None:
        """Stop listening."""
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        """Stay connected, reconnecting whenever the socket drops."""
        while not self._closed:
            try:
                await self._listen()
            except asyncio.CancelledError:
                return
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Alert socket dropped: %s", err)
            if self._closed:
                return
            await asyncio.sleep(RECONNECT_DELAY)

    async def _listen(self) -> None:
        client = self._coordinator.client
        session = async_get_clientsession(self._hass)
        token = await client.async_get_access_token()
        im_ip, ws_port = await client.async_get_im_endpoint()

        async with session.ws_connect(
            f"ws://{im_ip}:{ws_port}/imclient?access_token={token}", timeout=20
        ) as ws:
            self._ws = ws
            self._seq = 0
            await self._send(
                [
                    CMD_LOGIN,
                    0,
                    {
                        "token": token,
                        "info": {
                            "ver": "3.1.37",
                            "model": "homeassistant",
                            "sysver": "12",
                            "pid": self._pid,
                            "lang": "en_us",
                            "os": "android",
                            "app": "Proof",
                        },
                    },
                ]
            )
            _LOGGER.debug("Alert socket connected to %s:%s", im_ip, ws_port)
            self.connected = True
            self.connected_since = dt_util.utcnow().isoformat()
            heartbeat = asyncio.ensure_future(self._heartbeat(ws))
            # Ask the server something it must answer. If a reply comes back the
            # socket is genuinely live and two-way, which separates "connected
            # but no alerts happened" from "connected to nothing".
            probe = asyncio.ensure_future(self._probe())
            try:
                async for message in ws:
                    if message.type is not aiohttp.WSMsgType.TEXT:
                        break
                    self.frames += 1
                    self.last_frame = dt_util.utcnow().isoformat()
                    self._handle(message.data)
            finally:
                self.connected = False
                probe.cancel()
                heartbeat.cancel()
                self._ws = None

    async def _send(self, packet: list[Any]) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.send_str(json.dumps(packet))

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def _probe(self) -> None:
        """Ask the server for the devices' status, as the app does on start."""
        await asyncio.sleep(2)
        devices = list(self._coordinator.data)
        if devices:
            await self._send(
                [CMD_REQUEST, self._next_seq(), ["u.getdevstatus", devices]]
            )

    async def _heartbeat(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            while not ws.closed:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                # The app's ping carries an incrementing sequence and a 1, and
                # the server is picky about the shape.
                await self._send([CMD_PING, self._next_seq(), 1])
        except (asyncio.CancelledError, ConnectionError):
            pass

    def _handle(self, raw: str) -> None:
        """Record an alert if that is what arrived."""
        try:
            packet = json.loads(raw)
        except ValueError:
            return
        if not isinstance(packet, list) or len(packet) < 3:
            return
        if packet[0] == CMD_ACK:
            self.replies += 1
            # The login reply carries our session id. Worth keeping: it names
            # the address the cloud delivers to, which is what to compare if
            # alerts ever go to the phone instead.
            if isinstance(packet[-1], dict) and packet[-1].get("sid"):
                self.session_id = packet[-1]["sid"]
            return
        if packet[0] != CMD_MSG:
            return

        # The server holds a message as undelivered until it is acknowledged,
        # and stops sending more without it.
        self._hass.async_create_task(
            self._send([CMD_ACK, packet[1], 0, []])
        )

        envelope = packet[2]
        if not isinstance(envelope, list) or len(envelope) < 5:
            return
        if envelope[2] != MSG_EVENT or not isinstance(envelope[4], dict):
            return

        payload = envelope[4]
        device_id = self._device_from(envelope[1])
        if device_id is None:
            return

        alert_id = str(envelope[0]) if envelope[0] else None
        if alert_id and alert_id in self._seen:
            return

        loc = payload.get("loc") or []
        when = payload.get("time")
        message = {
            "topic": payload.get("topic"),
            "text": payload.get("msg") or payload.get("content"),
            "type": payload.get("type"),
            "time": dt_util.utc_from_timestamp(when / 1000).isoformat()
            if when
            else dt_util.utcnow().isoformat(),
        }
        if len(loc) == 2:
            message["latitude"], message["longitude"] = loc[0], loc[1]

        stored = self._coordinator.messages.setdefault(device_id, [])
        if alert_id:
            self._seen.add(alert_id)
            message["id"] = alert_id
        self.alerts += 1
        # Replayed history arrives oldest-first, so place by time rather than
        # always at the front.
        stored.insert(0, message)
        stored.sort(key=lambda m: m.get("time") or "", reverse=True)
        del stored[MAX_MESSAGES:]
        self._coordinator.save_state()
        self._coordinator.async_update_listeners()
        self._hass.bus.async_fire(
            EVENT_MESSAGE, {"device_id": device_id, **message}
        )

    def _device_from(self, targets: Any) -> str | None:
        """Pick the dashcam out of the message's address list."""
        if not isinstance(targets, list):
            return None
        for target in targets:
            candidate = str(target).split("#", 1)[0]
            if candidate in self._coordinator.data:
                return candidate
        return None

