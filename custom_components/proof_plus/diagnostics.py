"""Diagnostics support for the Proof Plus integration."""
from __future__ import annotations

import re
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_USERNAME,
    DOMAIN,
)
from .coordinator import ProofCoordinator

TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    # Either token is the account's live session.
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_RTSP_USERNAME,
    CONF_RTSP_PASSWORD,
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

# go2rtc logs the feed URL, which carries the feed's key.
_FEED_KEY = re.compile(r"key=[^&\s\"]+")


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    coordinator: ProofCoordinator = hass.data[DOMAIN][entry.entry_id]
    rtsp = coordinator.rtsp
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": async_redact_data(dict(entry.options), TO_REDACT),
        "devices": async_redact_data(coordinator.data, TO_REDACT),
        "rtsp": {
            "running": rtsp.running,
            "streams": {device_id: rtsp.state(device_id) for device_id in coordinator.data},
            "go2rtc_log": [
                _FEED_KEY.sub("key=**REDACTED**", line) for line in rtsp.go2rtc_log
            ],
        }
        if rtsp is not None
        else None,
    }
