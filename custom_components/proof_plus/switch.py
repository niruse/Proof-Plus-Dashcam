"""Dashcam settings exposed as switches.

Reading and writing these talks to the dashcam itself over the control channel,
so the values are only known once they have been fetched (press "Refresh
settings", or change one). They are not polled, because every read would make
the device stream over its cellular connection.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_ENABLE_LIVE,
    CONF_ENABLE_SNAPSHOT,
    CONF_SNAPSHOT_INTERVAL,
    DEFAULT_SNAPSHOT_INTERVAL,
    DOMAIN,
)
from .coordinator import ProofCoordinator
from .entity import ProofEntity


@dataclass(frozen=True, kw_only=True)
class ProofSwitchDescription(SwitchEntityDescription):
    """A dashcam setting that is on or off."""

    prop: str


SWITCHES: tuple[ProofSwitchDescription, ...] = (
    ProofSwitchDescription(
        key="record_voice",
        translation_key="record_voice",
        prop="dvr_voice",
        icon="mdi:microphone",
    ),
    ProofSwitchDescription(
        key="voice_messages",
        translation_key="voice_messages",
        prop="ttson",
        icon="mdi:account-voice",
    ),
    ProofSwitchDescription(
        key="motion_detection",
        translation_key="motion_detection",
        prop="vibc",
        icon="mdi:motion-sensor",
    ),
    ProofSwitchDescription(
        key="timelapse_parking",
        translation_key="timelapse_parking",
        prop="cm_mode",
        icon="mdi:timelapse",
    ),
    ProofSwitchDescription(
        key="rear_camera",
        translation_key="rear_camera",
        prop="back_camera_enable",
        icon="mdi:camera-rear",
        entity_category=EntityCategory.CONFIG,
    ),
    ProofSwitchDescription(
        key="wifi",
        translation_key="wifi",
        prop="wifion",
        icon="mdi:wifi",
        entity_category=EntityCategory.CONFIG,
    ),
)


# Which alerts the account receives. These live in the cloud rather than on the
# dashcam, so unlike the settings above they are cheap to read and are polled.
ALERTS: tuple[SwitchEntityDescription, ...] = (
    SwitchEntityDescription(key="accmsgOn", translation_key="alert_acc",
                            icon="mdi:key-variant", entity_category=EntityCategory.CONFIG),
    SwitchEntityDescription(key="shakeOn", translation_key="alert_vibration",
                            icon="mdi:vibrate", entity_category=EntityCategory.CONFIG),
    SwitchEntityDescription(key="collOn", translation_key="alert_accident",
                            icon="mdi:car-emergency", entity_category=EntityCategory.CONFIG),
    SwitchEntityDescription(key="sosOn", translation_key="alert_share",
                            icon="mdi:share-variant", entity_category=EntityCategory.CONFIG),
    SwitchEntityDescription(key="neutralOn", translation_key="alert_idle",
                            icon="mdi:timer-sand", entity_category=EntityCategory.CONFIG),
    SwitchEntityDescription(key="fenceOn", translation_key="alert_geofence",
                            icon="mdi:map-marker-radius", entity_category=EntityCategory.CONFIG),
    SwitchEntityDescription(key="overSpeedOn", translation_key="alert_overspeed",
                            icon="mdi:speedometer", entity_category=EntityCategory.CONFIG),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the setting switches for each dashcam."""
    coordinator: ProofCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = [
        ProofSettingSwitch(hass, coordinator, device_id, description)
        for device_id in coordinator.data
        for description in SWITCHES
    ]
    # The alert toggles belong to the account, not to a dashcam, so they are
    # created once and attached to the first device.
    if first_device := next(iter(coordinator.data), None):
        entities.extend(
            ProofAlertSwitch(coordinator, first_device, description)
            for description in ALERTS
        )
    # Auto-refresh only makes sense when there are snapshots to refresh.
    if entry.options.get(CONF_ENABLE_LIVE) and entry.options.get(CONF_ENABLE_SNAPSHOT):
        minutes = entry.options.get(CONF_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL)
        entities.extend(
            ProofAutoSnapshotSwitch(hass, coordinator, device_id, minutes)
            for device_id in coordinator.data
        )
    async_add_entities(entities)


class ProofAutoSnapshotSwitch(ProofEntity, SwitchEntity, RestoreEntity):
    """Keep the dashboard snapshots refreshing on a timer.

    Off by default, and remembered across restarts: every refresh wakes the
    dashcam and spends its mobile data, so this has to be a deliberate choice
    rather than something that quietly switches itself back on.
    """

    _attr_translation_key = "auto_snapshot"
    _attr_icon = "mdi:camera-timer"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: ProofCoordinator,
        device_id: str,
        minutes: int,
    ) -> None:
        super().__init__(coordinator, device_id)
        self.hass = hass
        self._minutes = max(1, int(minutes or DEFAULT_SNAPSHOT_INTERVAL))
        self._attr_unique_id = f"{device_id}_auto_snapshot"
        self._attr_is_on = False
        self._cancel: Any = None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Show how often it refreshes, since that is set in the options."""
        return {"interval_minutes": self._minutes}

    async def async_added_to_hass(self) -> None:
        """Restore the last choice and resume refreshing if it was on."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == "on"
        if self._attr_is_on:
            self._schedule()

    async def async_will_remove_from_hass(self) -> None:
        """Stop the timer so it cannot outlive the entity."""
        self._unschedule()
        await super().async_will_remove_from_hass()

    def _schedule(self) -> None:
        if self._cancel is not None:
            return
        self._cancel = async_track_time_interval(
            self.hass, self._async_refresh, timedelta(minutes=self._minutes)
        )

    def _unschedule(self) -> None:
        if self._cancel is not None:
            self._cancel()
            self._cancel = None

    async def _async_refresh(self, _now: Any) -> None:
        """Take a new still from each camera."""
        for image in self.coordinator.snapshot_images.get(self._device_id) or []:
            await image.async_capture()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start refreshing, and take one straight away."""
        self._attr_is_on = True
        self._schedule()
        self.async_write_ha_state()
        await self._async_refresh(None)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop refreshing; the last pictures stay on the dashboard."""
        self._attr_is_on = False
        self._unschedule()
        self.async_write_ha_state()


class ProofAlertSwitch(ProofEntity, SwitchEntity):
    """One account alert (which events the Proof service notifies about)."""

    def __init__(
        self,
        coordinator: ProofCoordinator,
        device_id: str,
        description: SwitchEntityDescription,
    ) -> None:
        super().__init__(coordinator, device_id)
        self.entity_description = description
        self._attr_unique_id = f"alert_{description.key}_{coordinator.client.uid}"

    @property
    def available(self) -> bool:
        """Available once the account settings have been read."""
        return super().available and self.entity_description.key in self.coordinator.alerts

    @property
    def is_on(self) -> bool | None:
        """Return whether this alert is enabled."""
        return self.coordinator.alerts.get(self.entity_description.key)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the alert."""
        await self.coordinator.async_set_alert(self.entity_description.key, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the alert."""
        await self.coordinator.async_set_alert(self.entity_description.key, False)


class ProofSettingSwitch(ProofEntity, SwitchEntity):
    """One on/off dashcam setting."""

    entity_description: ProofSwitchDescription

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: ProofCoordinator,
        device_id: str,
        description: ProofSwitchDescription,
    ) -> None:
        super().__init__(coordinator, device_id)
        self.hass = hass
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def available(self) -> bool:
        """Available once the device's settings have been read."""
        return super().available and self._value is not None

    @property
    def _value(self) -> Any:
        props = self.coordinator.device_props.get(self._device_id) or {}
        return props.get(self.entity_description.prop)

    @property
    def is_on(self) -> bool | None:
        """Return the setting's state."""
        value = self._value
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the setting."""
        await self._async_write(1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the setting."""
        await self._async_write(0)

    async def _async_write(self, value: int) -> None:
        await self.coordinator.async_set_device_props(
            self.hass, self._device_id, {self.entity_description.prop: value}
        )
