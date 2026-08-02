"""DataUpdateCoordinator for the Proof Plus integration."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ProofApiClient, ProofAuthError, ProofError
from .const import (
    CONF_ALBUM_LIMIT,
    CONF_ENABLE_MEDIA_BROWSER,
    CONF_ENABLE_SNAPSHOT,
    CONF_SCAN_INTERVAL,
    DEFAULT_ALBUM_LIMIT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    GSM_BANDS,
    GSM_WEAK_DBM,
)

_LOGGER = logging.getLogger(__name__)


def _gsm_quality(signal: int | None) -> str | None:
    """Describe a signal level using the bands the app shows."""
    if signal is None:
        return None
    for floor, label in GSM_BANDS:
        if signal >= floor:
            return label
    return "very_poor"


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
        # They are kept on disk so the controls still work after a restart
        # instead of going unavailable until the user refreshes them.
        self.device_props: dict[str, dict[str, Any]] = {}
        self._store: Store = Store(
            hass, 1, f"{DOMAIN}.{entry.entry_id}.device_props"
        )
        # Account-wide alert toggles (msgctrl); cheap to read, so polled.
        self.alerts: dict[str, bool] = {}
        # Last self-check result per dashcam.
        self.self_check: dict[str, dict[str, Any]] = {}
        # Last known storage figures per dashcam (megabytes).
        self.storage: dict[str, dict[str, Any]] = {}
        # Alerts the cloud has pushed, newest first, per dashcam.
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.message_listener: Any = None
        # Snapshot images register themselves here so the refresh button can
        # find them without reaching into Home Assistant's platform internals.
        self.snapshot_images: dict[str, list[Any]] = {}
        # How many recordings each album folder lists.
        self.album_limit = entry.options.get(CONF_ALBUM_LIMIT, DEFAULT_ALBUM_LIMIT)
        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=scan_interval),
        )

    async def async_load_stored_props(self) -> None:
        """Restore the settings and self-check results from a previous run."""
        if isinstance(stored := await self._store.async_load(), dict):
            # Older versions stored only the settings, keyed by device id.
            if "props" in stored or "self_check" in stored:
                self.device_props = stored.get("props") or {}
                self.self_check = stored.get("self_check") or {}
                self.storage = stored.get("storage") or {}
                self.messages = stored.get("messages") or {}
            else:
                self.device_props = stored

    def async_run_self_check(self, device_id: str) -> dict[str, Any]:
        """Check the things the app's self-check screen reports on.

        The dashcam only answers direct queries while the ignition is on, so
        this judges the values it last reported to the cloud instead. Each item
        is "ok", "problem" or "unknown".
        """
        device = self.data.get(device_id) or {}
        stats = device.get("stats") or {}
        status = device.get("status") or {}
        status_stats = status.get("stats") or {}
        gps = status.get("gps") or {}

        def ok_if(value: bool | None) -> str:
            return "unknown" if value is None else ("ok" if value else "problem")

        signal = status_stats.get("sig")
        ignition_on = bool(stats.get("acc"))
        items = {
            "sim_card": ok_if(bool(stats.get("iccid")) if stats else None),
            "gsm_signal": ok_if(signal >= GSM_WEAK_DBM if signal is not None else None),
            # Whether the Proof cloud is reachable, which is what the app
            # checks here. Whether the dashcam itself is currently connected
            # is a separate thing, reported by the Online sensor: a parked
            # camera drops its connection and that is not a fault.
            "server": ok_if(self.last_update_success),
            # The app shows these two as failing while the car is parked, since
            # the dashcam cannot report either with the ignition off.
            "ignition": ok_if(ignition_on),
            "positioning": ok_if(bool(gps.get("valid")) and ignition_on),
            # Only known once the storage has been read, which needs a live
            # session to the dashcam; the button does that.
            "sd_card": ok_if(
                (self.storage.get(device_id, {}).get("external_sd") or {}).get("exist")
                if self.storage.get(device_id)
                else None
            ),
        }
        result = {
            "run": dt_util.utcnow().isoformat(),
            "items": items,
            "gsm_quality": _gsm_quality(signal),
            "gsm_signal_dbm": signal,
            # A parked car is not a fault, so only the items that point at a
            # real problem decide the overall outcome.
            "problems": sorted(
                k
                for k in ("sim_card", "gsm_signal", "server")
                if items[k] == "problem"
            ),
        }
        self.self_check[device_id] = result
        self._save_props()
        self.async_update_listeners()
        return result

    def save_state(self) -> None:
        """Persist everything that should survive a restart."""
        self._save_props()

    def _save_props(self) -> None:
        """Persist the settings and self-check results across restarts."""
        self._store.async_delay_save(
            lambda: {
                "props": self.device_props,
                "self_check": self.self_check,
                "storage": self.storage,
                "messages": self.messages,
            },
            2,
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
            self._save_props()
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
        self._save_props()
        self.async_update_listeners()
        if (fresh := await client.async_get_props()) is not None:
            self.device_props[device_id] = fresh
            self._save_props()
            self.async_update_listeners()
        return True

    async def async_get_storage(
        self, hass: HomeAssistant, device_id: str
    ) -> dict[str, Any] | None:
        """Read the dashcam's internal and SD-card capacity.

        This needs a live session to the camera, so it is only done on request.
        """
        client = await self.live_session(hass, device_id).async_acquire(0)
        if client is None:
            return None
        storage = await client.async_get_storage()
        if storage:
            self.storage[device_id] = storage
            self._save_props()
            self.async_update_listeners()
        return storage

    async def async_set_alert(self, key: str, enabled: bool) -> bool:
        """Turn one account alert on or off."""
        alerts = {**self.alerts, key: enabled}
        if not await self.client.async_set_alerts(alerts):
            return False
        self.alerts = alerts
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
        await self._async_refresh_alerts()
        if self._fetch_events:
            await self._async_refresh_events(result)
        return result

    async def _async_refresh_alerts(self) -> None:
        """Read the account's alert toggles; a failure must not fail the update."""
        try:
            config = await self.client.async_get_account_config()
        except ProofError as err:
            _LOGGER.debug("Could not read the account settings: %s", err)
            return
        if isinstance(config.get("msgctrl"), dict):
            self.alerts = config["msgctrl"]

    async def _async_refresh_events(self, devices: dict[str, dict[str, Any]]) -> None:
        """Fetch recent event metadata; failures here must not fail the update."""
        for device_id in devices:
            try:
                self.latest_events[device_id] = await self.client.async_get_files(
                    device_id, "shake", size=20
                )
            except ProofError as err:
                _LOGGER.debug("Could not list events for %s: %s", device_id, err)
