"""DataUpdateCoordinator for the Proof Plus integration."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import STORAGE_DIR, Store
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
    SESSION_RENEW_HOURS,
    SESSION_RENEW_RETRY_HOURS,
)

_LOGGER = logging.getLogger(__name__)

STORE_SUFFIX = ".device_props"

# How long a settings or storage read keeps the dashcam session after it is
# done, so the reads a button makes back to back share one session (the
# dashcam wants a pause between sessions) without holding it open for good.
CONTROL_LINGER = 20


def _store_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}{STORE_SUFFIX}"


def _login_notice_id(entry: ConfigEntry) -> str:
    return f"{DOMAIN}_{entry.entry_id}_login"


@callback
def async_notify_login_needed(
    hass: HomeAssistant, entry: ConfigEntry, expires_at: float | None
) -> None:
    """Tell the user, where they will see it, that a new SMS code is needed.

    Home Assistant only flags a re-authentication on the integrations page,
    which is easy to miss for weeks while every Proof entity sits unavailable.
    """
    if expires_at and expires_at > time.time():
        when = dt_util.as_local(dt_util.utc_from_timestamp(expires_at))
        status = f"runs out on {when:%a %d %b at %H:%M}"
    else:
        status = "has run out, so the dashcam is no longer being updated"
    persistent_notification.async_create(
        hass,
        f"The Proof Plus sign-in {status}. The Proof cloud will not renew it on "
        "its own, so it needs a new SMS code: open "
        f"[Proof Plus](/config/integrations/integration/{DOMAIN}) and choose "
        "**Re-authenticate**.",
        title="Proof Plus needs a new SMS code",
        notification_id=_login_notice_id(entry),
    )


@callback
def async_clear_login_notice(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Withdraw the sign-in notice once a working session is back."""
    persistent_notification.async_dismiss(hass, _login_notice_id(entry))


# A fix this much older than the dashcam's latest report means it has lost
# GPS and is repeating its last position.
GPS_STALE_SECONDS = 600
# A repair issue after this long driving without a fix, if the dashcam has
# reported within GPS_REPORT_RECENT_SECONDS (so it is really online).
GPS_ISSUE_SECONDS = 3600
GPS_REPORT_RECENT_SECONDS = 900


def last_report(device: dict[str, Any]) -> float | None:
    """When the dashcam last reported anything, in epoch seconds.

    ``status.stime`` alone is not it: on 2026-10-09 it froze along with the
    GPS fix, while the device kept reporting — the report times in the GPS
    block (``gtm``/``dtm``, milliseconds) and ``status.dtime`` move on.
    """
    status = device.get("status") or {}
    gps = status.get("gps") or {}
    times = [status.get("stime"), status.get("dtime")]
    times += [gps[key] / 1000 for key in ("gtm", "dtm") if gps.get(key)]
    times = [t for t in times if isinstance(t, (int, float)) and t > 0]
    return max(times) if times else None


def gps_fix_fresh(device: dict[str, Any]) -> bool | None:
    """Whether the position is a current fix rather than a repeated old one.

    The dashcam keeps ``valid`` set and resends its last position when it
    cannot see satellites, so compare the fix time with its latest report.
    """
    gps = (device.get("status") or {}).get("gps") or {}
    if not gps:
        return None
    if not gps.get("valid"):
        return False
    fix, report = gps.get("time"), last_report(device)
    if not fix or not report:
        return True
    return report - fix <= GPS_STALE_SECONDS


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
        self._control_timers: dict[str, Any] = {}
        # The RTSP output, when it is set up (see rtsp.ProofRtsp).
        self.rtsp: Any = None
        # Last known device settings per dashcam, read on demand over the
        # control channel (reading them requires connecting to the device).
        # They are kept on disk so the controls still work after a restart
        # instead of going unavailable until the user refreshes them.
        self.device_props: dict[str, dict[str, Any]] = {}
        self._store: Store = Store(hass, 1, _store_key(entry.entry_id))
        # Whether this entry had saved anything yet; a re-added integration
        # has not, and picks up what the removed entry left behind instead.
        self._had_store = False
        # Early session renewal: when it was last tried, and what came of it.
        self.session_renew_attempt: datetime | None = None
        self.session_renew_result: str | None = None
        # Account-wide alert toggles (msgctrl); cheap to read, so polled.
        self.alerts: dict[str, bool] = {}
        # device id -> (ignition, epoch seconds) last seen while the dashcam was
        # online or from an ACC alert, held while it is offline (_hold_ignition).
        self._acc_seen: dict[str, tuple[bool, float]] = {}
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
        # The latest still per camera, shared by the camera entity that shows
        # it and the image entity the refresh button drives. Keeping one copy
        # is what makes "Take new photos" change the picture on the card.
        self.stills: dict[str, dict[int, bytes]] = {}
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
            self._had_store = True
            # Older versions stored only the settings, keyed by device id.
            if "props" in stored or "self_check" in stored:
                self.device_props = stored.get("props") or {}
                self.self_check = stored.get("self_check") or {}
                self.storage = stored.get("storage") or {}
                self.messages = stored.get("messages") or {}
            else:
                self.device_props = stored

    async def async_adopt_previous_state(self) -> str | None:
        """Carry the history over from a removed entry for the same dashcams.

        Removing and re-adding the integration makes a new config entry with
        an empty store, so the alert history, the last settings read and the
        self-check result all vanish — and the settings controls stay
        unavailable until the dashcam is read again. The removed entry's store
        is still on disk; take the newest one that covers these dashcams.
        Returns the entry id it came from, if any.
        """
        if self._had_store or not self.data:
            return None
        folder = Path(self.hass.config.path(STORAGE_DIR))
        live = {e.entry_id for e in self.hass.config_entries.async_entries(DOMAIN)}
        prefix = f"{DOMAIN}."

        def _orphans() -> list[tuple[float, str]]:
            found = []
            for path in folder.glob(f"{DOMAIN}.*{STORE_SUFFIX}"):
                entry_id = path.name[len(prefix) : -len(STORE_SUFFIX)]
                if entry_id not in live:
                    found.append((path.stat().st_mtime, entry_id))
            return sorted(found, reverse=True)

        devices = set(self.data)
        for _mtime, entry_id in await self.hass.async_add_executor_job(_orphans):
            stored = await Store(self.hass, 1, _store_key(entry_id)).async_load()
            if not isinstance(stored, dict) or not (
                "props" in stored or "messages" in stored
            ):
                continue

            def ours(section: str) -> dict[str, Any]:
                return {
                    device_id: value
                    for device_id, value in (stored.get(section) or {}).items()
                    if device_id in devices
                }

            if not any(
                ours(section)
                for section in ("props", "self_check", "storage", "messages")
            ):
                continue
            self.device_props = ours("props")
            self.self_check = ours("self_check")
            self.storage = ours("storage")
            self.messages = ours("messages")
            self._had_store = True
            self._save_props()
            return entry_id
        return None

    async def _async_renew_session_early(self) -> None:
        """Renew the cloud session in its last day, while it still works.

        Waiting for the token to run out means everything stops until someone
        notices; renewing ahead of time either keeps it going, or — if the
        cloud refuses — asks for a new SMS code while there is still time.
        """
        expires_at = self.client.token_expires_at
        if not expires_at or expires_at - time.time() > SESSION_RENEW_HOURS * 3600:
            return
        now = dt_util.utcnow()
        if self.session_renew_attempt and now - self.session_renew_attempt < timedelta(
            hours=SESSION_RENEW_RETRY_HOURS
        ):
            return
        self.session_renew_attempt = now
        try:
            await self.client.async_renew()
        except ProofAuthError as err:
            self.session_renew_result = f"refused: {err}"
            async_notify_login_needed(self.hass, self.config_entry, expires_at)
            self.config_entry.async_start_reauth(self.hass)
        except ProofError as err:
            # Unreachable for now; try again on the next round.
            self.session_renew_result = f"failed: {err}"
        else:
            self.session_renew_result = "renewed"
            async_clear_login_notice(self.hass, self.config_entry)

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
            "positioning": ok_if(bool(gps_fix_fresh(device)) and ignition_on),
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
        client = await self._async_control_client(hass, device_id)
        try:
            if client is None:
                return None
            props = await client.async_get_props()
        finally:
            self._schedule_control_release(hass, device_id)
        if props is not None:
            self.device_props[device_id] = props
            self._save_props()
            self.async_update_listeners()
        return props

    async def async_set_device_props(
        self, hass: HomeAssistant, device_id: str, props: dict[str, Any]
    ) -> bool:
        """Change dashcam settings, then refresh the cached values."""
        client = await self._async_control_client(hass, device_id)
        try:
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
        finally:
            self._schedule_control_release(hass, device_id)

    async def async_get_storage(
        self, hass: HomeAssistant, device_id: str
    ) -> dict[str, Any] | None:
        """Read the dashcam's internal and SD-card capacity.

        This needs a live session to the camera, so it is only done on request.
        """
        client = await self._async_control_client(hass, device_id)
        try:
            if client is None:
                return None
            storage = await client.async_get_storage()
        finally:
            self._schedule_control_release(hass, device_id)
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

    async def _async_control_client(self, hass: HomeAssistant, device_id: str) -> Any:
        """The dashcam session, for a settings or storage exchange.

        Registered as a watcher of its own, so the session is not left open
        afterwards with nobody holding it — and taken on whichever camera is
        showing, so a settings read does not pull a viewer (or the RTSP
        stream) off the rear camera.
        """
        session = self.live_session(hass, device_id)
        session.add_watcher(f"{device_id}:control")
        self._schedule_control_release(hass, device_id)
        return await session.async_acquire(None)

    @callback
    def _schedule_control_release(self, hass: HomeAssistant, device_id: str) -> None:
        """Let go of the control claim once the reads have gone quiet."""
        if (handle := self._control_timers.pop(device_id, None)) is not None:
            handle.cancel()
        session = self.live_session(hass, device_id)
        self._control_timers[device_id] = hass.loop.call_later(
            CONTROL_LINGER,
            lambda: hass.async_create_task(
                session.async_release(f"{device_id}:control")
            ),
        )

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
            async_notify_login_needed(self.hass, self.config_entry, None)
            raise ConfigEntryAuthFailed(str(err)) from err
        except ProofError as err:
            # Connection issues, a stale signature (clock skew) or a transient
            # server error — all retryable.
            raise UpdateFailed(str(err)) from err
        if not devices:
            raise UpdateFailed("Proof returned no devices for this account")

        result = {dev["id"]: dev for dev in devices if "id" in dev}
        self._hold_ignition(result)
        self._check_gps(result)
        await self._async_renew_session_early()
        await self._async_refresh_alerts()
        if self._fetch_events:
            await self._async_refresh_events(result)
            self._attach_message_photos()
        return result

    @callback
    def _hold_ignition(self, devices: dict[str, dict[str, Any]]) -> None:
        """Keep the ignition through a drop of the dashcam's connection.

        While the dashcam is offline the cloud reports acc 0 (and speed 0)
        even mid-drive, so a dashcam that lost its 4G link turned the ignition
        off for the rest of the trip (seen for 50 minutes). Offline, the
        ignition stays what it was last seen online, until an ACC alert says
        otherwise (async_on_message).
        """
        now = time.time()
        for device_id, dev in devices.items():
            stats = dev.get("stats")
            if not isinstance(stats, dict) or "acc" not in stats:
                continue
            if dev.get("online"):
                self._acc_seen[device_id] = (bool(stats["acc"]), now)
            elif device_id in self._acc_seen:
                stats["acc"] = 1 if self._acc_seen[device_id][0] else 0

    @callback
    def async_on_message(self, event: Event) -> None:
        """An ACC alert sets the ignition held while the dashcam is offline."""
        if str(event.data.get("type") or "").lower() != "acc":
            return
        device_id = event.data.get("device_id")
        when = dt_util.parse_datetime(str(event.data.get("time") or ""))
        stamp = when.timestamp() if when else time.time()
        seen = self._acc_seen.get(device_id)
        if seen is not None and seen[1] >= stamp:
            return  # an alert replayed at login, older than what was seen since
        on = str(event.data.get("topic") or "").upper().endswith("ON")
        self._acc_seen[device_id] = (on, stamp)
        dev = (self.data or {}).get(device_id)
        if dev and not dev.get("online") and isinstance(dev.get("stats"), dict):
            dev["stats"]["acc"] = 1 if on else 0
            self.async_update_listeners()

    @callback
    def _check_gps(self, devices: dict[str, dict[str, Any]]) -> None:
        """Raise a repair issue while a dashcam drives without a GPS fix.

        With the ignition on, the dashcam should have a current fix within
        minutes. An hour without one means its GPS is not working (receiver,
        antenna or mount); the map, speed and overspeed alerts all depend on
        it. The issue clears by itself with the next current fix.
        """
        now = time.time()
        for device_id, dev in devices.items():
            issue_id = f"gps_no_fix_{device_id}"
            gps = (dev.get("status") or {}).get("gps") or {}
            fix = gps.get("time")
            ignition = bool((dev.get("stats") or {}).get("acc"))
            report = last_report(dev)
            stale = (
                ignition
                and gps_fix_fresh(dev) is False
                and isinstance(fix, (int, float))
                and fix > 0
                and report is not None
                and now - report < GPS_REPORT_RECENT_SECONDS
                and now - fix > GPS_ISSUE_SECONDS
            )
            if stale:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="gps_no_fix",
                    translation_placeholders={
                        "name": dev.get("name") or device_id,
                        "since": dt_util.as_local(dt_util.utc_from_timestamp(fix)).strftime("%d/%m/%Y %H:%M"),
                        "satellites": str(gps.get("satu", 0)),
                    },
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def _attach_message_photos(self) -> None:
        """Pair each alert with the pictures the dashcam saved for it.

        Some alerts leave a photo from each camera in the cloud album a few
        seconds later; ignition alerts leave none. The cloud gives no link
        between the two, so match them the way the app's own screen appears
        to — by time.
        """
        from .const import MESSAGE_PHOTO_WINDOW

        for device_id, messages in self.messages.items():
            events = [
                e for e in (self.latest_events.get(device_id) or [])
                if e.get("ftype") == "image" and e.get("fid") and e.get("time")
            ]
            if not events:
                continue
            for message in messages:
                if "photos" in message:
                    continue
                when = message.get("time")
                if not when:
                    continue
                try:
                    stamp = dt_util.parse_datetime(when).timestamp()
                except (TypeError, ValueError, AttributeError):
                    continue
                photos = [
                    {
                        "fid": e["fid"],
                        "camera": "Rear" if e.get("camid") else "Front",
                    }
                    for e in events
                    if abs(e["time"] / 1000 - stamp) <= MESSAGE_PHOTO_WINDOW
                ]
                # Record the empty result too, so an alert that never had
                # pictures is not re-examined on every refresh.
                message["photos"] = photos
            self._save_props()

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
