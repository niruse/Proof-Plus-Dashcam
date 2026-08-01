"""The Proof Plus integration."""
from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .api import ProofApiClient, ProofAuthError, ProofConnectionError
from .const import (
    CONF_ENABLE_LIVE,
    CONF_ENABLE_SNAPSHOT,
    CONF_REFRESH_TOKEN,
    CONF_SETTINGS_INTERVAL,
    DEFAULT_SETTINGS_INTERVAL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import ProofCoordinator


def _platforms_for(entry: ConfigEntry) -> list[str]:
    """Return the platforms to load, given the entry's opt-in options."""
    platforms = list(PLATFORMS)
    if entry.options.get(CONF_ENABLE_SNAPSHOT):
        platforms.append(Platform.IMAGE)
    if entry.options.get(CONF_ENABLE_LIVE):
        platforms.append(Platform.CAMERA)
    return platforms


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Proof Plus from a config entry."""

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
    # Restore the dashcam settings read previously, so those controls work
    # straight away instead of waiting for the user to refresh them.
    await coordinator.async_load_stored_props()
    await coordinator.async_config_entry_first_refresh()

    # Remember exactly which platforms were set up, so that a later options
    # change (which alters _platforms_for) unloads the right ones.
    coordinator.platforms = _platforms_for(entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, coordinator.platforms)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Optionally re-read the dashcam's settings on a schedule. Each read
    # connects to the device, so this is off unless the user asks for it.
    hours = entry.options.get(CONF_SETTINGS_INTERVAL, DEFAULT_SETTINGS_INTERVAL)
    if hours:

        async def _async_refresh_settings(_now: datetime) -> None:
            for device_id in coordinator.data:
                await coordinator.async_get_device_props(hass, device_id)

        entry.async_on_unload(
            async_track_time_interval(
                hass, _async_refresh_settings, timedelta(hours=hours)
            )
        )
        # Fill the values in soon after startup rather than waiting a full period.
        entry.async_on_unload(
            async_call_later(hass, 60, _async_refresh_settings)
        )
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when the user changes options.

    This listener also fires when a rotated refresh token is written back to
    the entry; comparing the options snapshot avoids a needless reload then.
    """
    coordinator: ProofCoordinator | None = hass.data[DOMAIN].get(entry.entry_id)
    if coordinator is None:
        return
    if dict(entry.options) != coordinator.options_snapshot:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: ProofCoordinator | None = hass.data[DOMAIN].get(entry.entry_id)
    # Unload the platforms that were actually set up, not the ones the current
    # options would imply (they may have just changed).
    platforms = coordinator.platforms if coordinator else _platforms_for(entry)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
