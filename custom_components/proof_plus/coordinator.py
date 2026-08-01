"""DataUpdateCoordinator for the Proof Plus integration."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ProofApiClient, ProofAuthError, ProofError
from .const import (
    CONF_ENABLE_MEDIA_BROWSER,
    CONF_ENABLE_SNAPSHOT,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class ProofCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Polls the Proof cloud and exposes devices keyed by device id."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: ProofApiClient
    ) -> None:
        self.client = client
        # Platforms this entry set up; filled in by async_setup_entry so the
        # unload path can match it even after the options change.
        self.platforms: list[str] = []
        # Snapshot of the options this setup was built with, so the update
        # listener can tell a real change from a refresh-token write.
        self.options_snapshot = dict(entry.options)
        self._fetch_events = entry.options.get(
            CONF_ENABLE_SNAPSHOT
        ) or entry.options.get(CONF_ENABLE_MEDIA_BROWSER)
        # Newest-first event metadata per device (no image bytes).
        self.latest_events: dict[str, list[dict[str, Any]]] = {}
        # One shared live session per dashcam (see camera.DeviceLiveSession).
        self._live_sessions: dict[str, Any] = {}
        # Last known device settings per dashcam, read on demand over the
        # control channel (reading them requires connecting to the device).
        self.device_props: dict[str, dict[str, Any]] = {}
        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=scan_interval),
        )

    async def async_get_device_props(
        self, hass: HomeAssistant, device_id: str
    ) -> dict[str, Any] | None:
        """Read the dashcam's own settings (needs a session to the device)."""
        client = await self.live_session(hass, device_id).async_acquire(0)
        if client is None:
            return None
        props = await client.async_get_props()
        if props is not None:
            self.device_props[device_id] = props
            self.async_update_listeners()
        return props

    async def async_set_device_props(
        self, hass: HomeAssistant, device_id: str, props: dict[str, Any]
    ) -> bool:
        """Change dashcam settings, then refresh the cached values."""
        client = await self.live_session(hass, device_id).async_acquire(0)
        if client is None:
            return False
        if not await client.async_set_props(props):
            return False
        # Reflect the change immediately, then confirm from the device.
        self.device_props.setdefault(device_id, {}).update(props)
        self.async_update_listeners()
        if (fresh := await client.async_get_props()) is not None:
            self.device_props[device_id] = fresh
            self.async_update_listeners()
        return True

    def live_session(self, hass: HomeAssistant, device_id: str) -> Any:
        """Return the shared live session for a dashcam, creating it on first use."""
        if device_id not in self._live_sessions:
            from .camera import DeviceLiveSession

            self._live_sessions[device_id] = DeviceLiveSession(hass, self, device_id)
        return self._live_sessions[device_id]

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            devices = await self.client.async_get_devices()
        except ProofAuthError as err:
            # Recoverable only by a new SMS login, so ask the user for one.
            raise ConfigEntryAuthFailed(str(err)) from err
        except ProofError as err:
            # Connection issues, a stale signature (clock skew) or a transient
            # server error — all retryable.
            raise UpdateFailed(str(err)) from err
        if not devices:
            raise UpdateFailed("Proof returned no devices for this account")

        result = {dev["id"]: dev for dev in devices if "id" in dev}
        if self._fetch_events:
            await self._async_refresh_events(result)
        return result

    async def _async_refresh_events(self, devices: dict[str, dict[str, Any]]) -> None:
        """Fetch recent event metadata; failures here must not fail the update."""
        for device_id in devices:
            try:
                self.latest_events[device_id] = await self.client.async_get_files(
                    device_id, "shake", size=20
                )
            except ProofError as err:
                _LOGGER.debug("Could not list events for %s: %s", device_id, err)
