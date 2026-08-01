"""Diagnostics support for the Proof Plus integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import ProofCoordinator

TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    "imei",
    "iccid",
    "imsi",
    "binduser",
    "curip",
    "loc",
    "lat",
    "lng",
    "acconinfo",
    "unique_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    coordinator: ProofCoordinator = hass.data[DOMAIN][entry.entry_id]
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "devices": async_redact_data(coordinator.data, TO_REDACT),
    }
