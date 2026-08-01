"""Constants for the Proof Plus integration."""
from __future__ import annotations

DOMAIN = "proof_plus"

CONF_CODE = "code"
CONF_REFRESH_TOKEN = "refresh_token"

# Options (all default off — nothing activates unless the user opts in).
CONF_SCAN_INTERVAL = "scan_interval"
CONF_ENABLE_SNAPSHOT = "enable_snapshot"
CONF_ENABLE_MEDIA_BROWSER = "enable_media_browser"
CONF_ENABLE_LIVE = "enable_live"
CONF_LIVE_KEEPALIVE = "live_keepalive"
CONF_SETTINGS_INTERVAL = "settings_interval"

DEFAULT_SCAN_INTERVAL = 30  # seconds; the device itself reports every ~30s
MIN_SCAN_INTERVAL = 10

# How long the live WebRTC session stays open after the last frame is requested.
# 0 means keep it open indefinitely (streams over cellular until stopped).
DEFAULT_LIVE_KEEPALIVE = 10

# How often to read the dashcam's own settings, in hours. Each read connects to
# the device, so this is off (0) unless the user asks for it.
DEFAULT_SETTINGS_INTERVAL = 0

# Always-on platforms; image/camera are added only when their option is enabled.
PLATFORMS = [
    "binary_sensor",
    "button",
    "device_tracker",
    "number",
    "select",
    "sensor",
    "switch",
]

ATTRIBUTION = "Data provided by 2proof.co.il"
