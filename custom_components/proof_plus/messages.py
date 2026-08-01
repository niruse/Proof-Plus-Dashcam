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

CMD_PING, CMD_ACK, CMD_LOGIN, CMD_MSG = 0, 1, 2, 4
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
        self.connected = False

    def start(self) -> None:
        """Begin listening in the background."""
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
            await ws.send_str(
                json.dumps(
                    [
                        CMD_LOGIN,
                        0,
                        {
                            "token": token,
                            "info": {
                                "ver": "3.1.37",
                                "model": "homeassistant",
                                "sysver": "12",
                                "pid": "ha-proof-messages",
                                "lang": "en_us",
                                "os": "android",
                                "app": "Proof",
                            },
                        },
                    ]
                )
            )
            _LOGGER.debug("Alert socket connected to %s:%s", im_ip, ws_port)
            self.connected = True
            heartbeat = asyncio.ensure_future(self._heartbeat(ws))
            try:
                async for message in ws:
                    if message.type is not aiohttp.WSMsgType.TEXT:
                        break
                    self._handle(message.data)
            finally:
                self.connected = False
                heartbeat.cancel()

    async def _heartbeat(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            while not ws.closed:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await ws.send_str(json.dumps([CMD_PING, 0, None]))
        except (asyncio.CancelledError, ConnectionError):
            pass

    def _handle(self, raw: str) -> None:
        """Record an alert if that is what arrived."""
        try:
            packet = json.loads(raw)
        except ValueError:
            return
        if not isinstance(packet, list) or len(packet) < 3 or packet[0] != CMD_MSG:
            return
        envelope = packet[2]
        if not isinstance(envelope, list) or len(envelope) < 5:
            return
        if envelope[2] != MSG_EVENT or not isinstance(envelope[4], dict):
            return

        payload = envelope[4]
        device_id = self._device_from(envelope[1])
        if device_id is None:
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
        stored.insert(0, message)
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

