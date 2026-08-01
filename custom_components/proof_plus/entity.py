"""Base entity for the Proof Plus integration."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN
from .coordinator import ProofCoordinator


class ProofEntity(CoordinatorEntity[ProofCoordinator]):
    """Base entity tied to one Proof device."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(self, coordinator: ProofCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        dev = self.device or {}
        stats = dev.get("stats") or {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=dev.get("name") or device_id,
            manufacturer="Proof",
            model=dev.get("type"),
            sw_version=stats.get("fwver") or dev.get("model"),
            serial_number=stats.get("imei"),
        )

    @property
    def device(self) -> dict[str, Any] | None:
        """Return the raw device dict from the coordinator."""
        return self.coordinator.data.get(self._device_id)

    @property
    def available(self) -> bool:
        """Entity is available while the device appears in the account."""
        return super().available and self.device is not None
