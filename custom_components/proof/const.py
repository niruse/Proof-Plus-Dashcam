"""Constants for the Proof Dashcam integration."""
from __future__ import annotations

DOMAIN = "proof"

CONF_BASE_URL = "base_url"

DEFAULT_SCAN_INTERVAL = 30  # seconds; the device itself reports every ~30s
MIN_SCAN_INTERVAL = 10

PLATFORMS = ["binary_sensor", "device_tracker", "sensor"]

ATTRIBUTION = "Data provided by 2proof.co.il"
