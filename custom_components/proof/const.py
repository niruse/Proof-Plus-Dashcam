"""Constants for the Proof Dashcam integration."""
from __future__ import annotations

DOMAIN = "proof"

CONF_CODE = "code"
CONF_REFRESH_TOKEN = "refresh_token"

# Options (all default off — nothing activates unless the user opts in).
CONF_SCAN_INTERVAL = "scan_interval"
CONF_ENABLE_SNAPSHOT = "enable_snapshot"
CONF_ENABLE_MEDIA_BROWSER = "enable_media_browser"
CONF_ENABLE_LIVE = "enable_live"

DEFAULT_SCAN_INTERVAL = 30  # seconds; the device itself reports every ~30s
MIN_SCAN_INTERVAL = 10

# Always-on platforms; image/camera are added only when their option is enabled.
PLATFORMS = ["binary_sensor", "device_tracker", "sensor"]

ATTRIBUTION = "Data provided by 2proof.co.il"
