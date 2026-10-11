"""The Proof Plus integration."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

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
from .coordinator import (
    ProofCoordinator,
    async_clear_login_notice,
    async_notify_login_needed,
)
from .messages import ProofMessageListener


def _platforms_for(entry: ConfigEntry) -> list[str]:
    """Return the platforms to load, given the entry's opt-in options."""
    platforms = list(PLATFORMS)
    if entry.options.get(CONF_ENABLE_SNAPSHOT):
        platforms.append(Platform.IMAGE)
    if entry.options.get(CONF_ENABLE_LIVE):
        platforms.append(Platform.CAMERA)
    return platforms


@callback
def _async_migrate_alert_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Give the alert switches back the account id in their unique id.

    Since setup started resuming the stored access token instead of logging in,
    the client no longer learnt the account id, and the alert switches were
    registered as ``alert_<key>_None`` — new entities with a ``_2`` suffix,
    leaving the originals behind.
    """
    if not entry.unique_id:
        return
    registry = er.async_get(hass)
    for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg.domain != "switch" or not (
            reg.unique_id.startswith("alert_") and reg.unique_id.endswith("_None")
        ):
            continue
        unique_id = f"{reg.unique_id.removesuffix('_None')}_{entry.unique_id}"
        if registry.async_get_entity_id("switch", DOMAIN, unique_id) is None:
            registry.async_update_entity(reg.entity_id, new_unique_id=unique_id)


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
        # The entry is keyed by the account id the login returned, which a
        # resumed session would otherwise never learn.
        uid=entry.unique_id
        if entry.unique_id != entry.data[CONF_USERNAME]
        else None,
    )

    try:
        await client.async_establish()
    except ProofAuthError as err:
        # Only a fresh SMS login can recover this.
        async_notify_login_needed(hass, entry, None)
        raise ConfigEntryAuthFailed(str(err)) from err
    except ProofConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = ProofCoordinator(hass, entry, client)
    # Restore the dashcam settings read previously, so those controls work
    # straight away instead of waiting for the user to refresh them.
    await coordinator.async_load_stored_props()
    await coordinator.async_config_entry_first_refresh()
    # A re-added integration starts from nothing; pick up what the removed
    # entry knew about the same dashcams.
    await coordinator.async_adopt_previous_state()
    _async_migrate_alert_ids(hass, entry)
    async_clear_login_notice(hass, entry)

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

        entry.async_on_unload(
            async_track_time_interval(
                hass, _run_self_check, timedelta(hours=check_hours)
            )
        )
        # The interval restarts with Home Assistant, so a box restarted more
        # often than that would never get there. Run now if the result
        # restored from disk is already older than the interval.
        for device_id in coordinator.data:
            run = (coordinator.self_check.get(device_id) or {}).get("run")
            last = dt_util.parse_datetime(run) if run else None
            if last is None or dt_util.utcnow() - last >= timedelta(hours=check_hours):
                coordinator.async_run_self_check(device_id)
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
