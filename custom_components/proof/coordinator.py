"""DataUpdateCoordinator for the Proof Dashcam integration."""
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
        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=scan_interval),
        )

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
