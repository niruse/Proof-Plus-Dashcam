"""The Proof Plus integration."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_DEVICE_ID,
    CONF_PASSWORD,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
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
    CONF_RTSP_MODE,
    CONF_SELFCHECK_INTERVAL,
    CONF_SETTINGS_INTERVAL,
    DEFAULT_RTSP_MODE,
    DEFAULT_SELFCHECK_INTERVAL,
    DEFAULT_SETTINGS_INTERVAL,
    DOMAIN,
    PLATFORMS,
    RTSP_MODE_TRIGGER,
    RUNTIME_OPTIONS,
)
from .coordinator import (
    ProofCoordinator,
    async_clear_login_notice,
    async_notify_login_needed,
)
from .deps import async_ensure_aiortc
from .messages import EVENT_MESSAGE, ProofMessageListener
from .rtsp import ProofRtsp

SERVICE_RTSP_TRIGGER = "rtsp_trigger"
SERVICE_RTSP_STOP = "rtsp_stop"
ATTR_MINUTES = "minutes"

_DEVICE_IDS = vol.All(cv.ensure_list, [cv.string])
RTSP_TRIGGER_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_DEVICE_ID): _DEVICE_IDS,
        vol.Optional(ATTR_MINUTES): vol.All(
            vol.Coerce(float), vol.Range(min=0.5, max=1440)
        ),
    }
)
RTSP_STOP_SCHEMA = vol.Schema({vol.Optional(ATTR_DEVICE_ID): _DEVICE_IDS})


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


def _rtsp_targets(hass: HomeAssistant, call: ServiceCall) -> list[tuple[ProofRtsp, str]]:
    """The (RTSP output, dashcam) pairs an action is aimed at."""
    wanted: set[str] | None = None
    if call.data.get(ATTR_DEVICE_ID):
        registry = dr.async_get(hass)
        wanted = set()
        for device_id in call.data[ATTR_DEVICE_ID]:
            if (device := registry.async_get(device_id)) is not None:
                wanted.update(
                    ident for domain, ident in device.identifiers if domain == DOMAIN
                )
    return [
        (coordinator.rtsp, dashcam)
        for coordinator in hass.data.get(DOMAIN, {}).values()
        if coordinator.rtsp is not None
        for dashcam in coordinator.data
        if wanted is None or dashcam in wanted
    ]


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register the RTSP actions (once, for every entry)."""
    if hass.services.has_service(DOMAIN, SERVICE_RTSP_TRIGGER):
        return

    @callback
    def _trigger(call: ServiceCall) -> None:
        targets = [
            (rtsp, dashcam)
            for rtsp, dashcam in _rtsp_targets(hass, call)
            if rtsp.mode == RTSP_MODE_TRIGGER
        ]
        if not targets:
            raise ServiceValidationError(
                "No Proof dashcam has RTSP set to 'Only when triggered'"
            )
        for rtsp, dashcam in targets:
            rtsp.trigger(dashcam, call.data.get(ATTR_MINUTES))

    @callback
    def _stop(call: ServiceCall) -> None:
        for rtsp, dashcam in _rtsp_targets(hass, call):
            rtsp.stop(dashcam)

    hass.services.async_register(
        DOMAIN, SERVICE_RTSP_TRIGGER, _trigger, schema=RTSP_TRIGGER_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RTSP_STOP, _stop, schema=RTSP_STOP_SCHEMA
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Proof Plus from a config entry."""
    # Live video needs aiortc, which Home Assistant cannot install itself.
    if error := await async_ensure_aiortc(hass):
        raise ConfigEntryNotReady(f"aiortc could not be installed: {error}")

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
    entry.async_on_unload(hass.bus.async_listen(EVENT_MESSAGE, coordinator.async_on_message))
    # Restore the dashcam settings read previously, so those controls work
    # straight away instead of waiting for the user to refresh them.
    await coordinator.async_load_stored_props()
    await coordinator.async_config_entry_first_refresh()
    # A re-added integration starts from nothing; pick up what the removed
    # entry knew about the same dashcams.
    await coordinator.async_adopt_previous_state()
    _async_migrate_alert_ids(hass, entry)
    async_clear_login_notice(hass, entry)
    # The RTSP output exists in every mode, so its controls can switch it on.
    coordinator.rtsp = ProofRtsp(hass, entry, coordinator)

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

    # RTSP for NVRs. Its go2rtc is a separate process, so it must also stop
    # when Home Assistant does, not only when the entry unloads.
    await coordinator.rtsp.async_setup()
    _async_register_services(hass)

    async def _stop_rtsp(_event: Event) -> None:
        await coordinator.rtsp.async_shutdown()

    entry.async_on_unload(
        hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, _stop_rtsp)
    )

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
    before = coordinator.options_snapshot
    changed = {
        key
        for key in set(entry.options) | set(before)
        if entry.options.get(key) != before.get(key)
    }
    if not changed:
        return
    if changed <= RUNTIME_OPTIONS:
        # The RTSP mode switches on the fly; reloading would drop the NVR.
        coordinator.options_snapshot = dict(entry.options)
        if coordinator.rtsp is not None:
            await coordinator.rtsp.async_set_mode(
                entry.options.get(CONF_RTSP_MODE, DEFAULT_RTSP_MODE)
            )
        return
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
        if coordinator is not None and coordinator.rtsp is not None:
            await coordinator.rtsp.async_shutdown()
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_RTSP_TRIGGER)
            hass.services.async_remove(DOMAIN, SERVICE_RTSP_STOP)
    return unload_ok
