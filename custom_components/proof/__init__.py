"""The Proof Dashcam integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DEFAULT_BASE_URL, ProofApiClient, ProofAuthError, ProofConnectionError
from .const import (
    CONF_BASE_URL,
    CONF_REFRESH_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import ProofCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Proof Dashcam from a config entry."""

    @callback
    def _store_refresh_token(token: str) -> None:
        """Persist a rotated refresh token so restarts keep working."""
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REFRESH_TOKEN: token}
        )

    client = ProofApiClient(
        async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        on_refresh_token=_store_refresh_token,
    )

    try:
        await client.async_refresh()
    except ProofAuthError as err:
        # Only a fresh SMS login can recover this.
        raise ConfigEntryAuthFailed(str(err)) from err
    except ProofConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = ProofCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when the polling interval changes.

    This listener also fires when a rotated refresh token is written back to
    the entry, which must not restart the integration.
    """
    coordinator: ProofCoordinator | None = hass.data[DOMAIN].get(entry.entry_id)
    if coordinator is None or coordinator.update_interval is None:
        return
    interval = entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL)
    if interval != coordinator.update_interval.total_seconds():
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
