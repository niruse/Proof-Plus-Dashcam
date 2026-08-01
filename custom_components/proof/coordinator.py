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
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class ProofCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Polls the Proof cloud and exposes devices keyed by device id."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: ProofApiClient
    ) -> None:
        self.client = client
        scan_interval = entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL)
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
        return {dev["id"]: dev for dev in devices if "id" in dev}
