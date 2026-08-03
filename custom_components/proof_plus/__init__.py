"""The Proof Plus integration."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

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
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRES,
    CONF_SELFCHECK_INTERVAL,
    CONF_SETTINGS_INTERVAL,
    DEFAULT_SELFCHECK_INTERVAL,
    DEFAULT_SETTINGS_INTERVAL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import ProofCoordinator
from .messages import ProofMessageListener


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
    def _store_tokens(tokens: dict[str, Any]) -> None:
        """Persist the current tokens so a restart keeps working."""
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ACCESS_TOKEN: tokens.get("access_token"),
                CONF_REFRESH_TOKEN: tokens.get("refresh_token"),
                CONF_TOKEN_EXPIRES: tokens.get("token_expires_at"),
            },
        )

    client = ProofApiClient(
        async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        on_tokens=_store_tokens,
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        token_expires_at=entry.data.get(CONF_TOKEN_EXPIRES) or 0.0,
    )

    try:
        await client.async_establish()
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

    # Listen for the alerts the cloud pushes (the app's Messages screen). This
    # is a link to the cloud, not to the camera, so it costs the dashcam
    # nothing.
    messages = ProofMessageListener(hass, coordinator)
    messages.start()
    coordinator.message_listener = messages

    @callback
    def _stop_messages() -> None:
        """Close the alert socket when the entry unloads.

        This must return nothing: Home Assistant awaits whatever an unload
        callback hands back, and a Task is not a coroutine.
        """
        hass.async_create_task(messages.async_stop())

    entry.async_on_unload(_stop_messages)

    # Optionally re-read the dashcam's settings on a schedule. Each read
    # connects to the device, so this is off unless the user asks for it.
    hours = entry.options.get(CONF_SETTINGS_INTERVAL, DEFAULT_SETTINGS_INTERVAL)
    if hours:

        async def _async_refresh_settings(_now: datetime) -> None:
            for device_id in coordinator.data:
                await coordinator.async_get_device_props(hass, device_id)

        # Interval only. The settings are restored from disk at startup, and
        # each read wakes the dashcam and uses its mobile data, so there is no
        # reason to fetch them again every restart.
        entry.async_on_unload(
            async_track_time_interval(
                hass, _async_refresh_settings, timedelta(hours=hours)
            )
        )

    # Run the self-check on a schedule. It only reads values already polled,
    # so it costs nothing and defaults to daily.
    check_hours = entry.options.get(
        CONF_SELFCHECK_INTERVAL, DEFAULT_SELFCHECK_INTERVAL
    )
    if check_hours:

        @callback
        def _run_self_check(_now: datetime) -> None:
            for device_id in coordinator.data:
                coordinator.async_run_self_check(device_id)

        # Only on the interval — the last result is restored from disk, so
        # there is no need to re-run it every time Home Assistant restarts.
        entry.async_on_unload(
            async_track_time_interval(
                hass, _run_self_check, timedelta(hours=check_hours)
            )
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
