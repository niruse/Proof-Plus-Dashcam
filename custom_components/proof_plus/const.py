"""Constants for the Proof Plus integration."""
from __future__ import annotations

DOMAIN = "proof_plus"

CONF_CODE = "code"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ACCESS_TOKEN = "access_token"
CONF_TOKEN_EXPIRES = "token_expires_at"

# Options (all default off — nothing activates unless the user opts in).
CONF_SCAN_INTERVAL = "scan_interval"
CONF_ENABLE_SNAPSHOT = "enable_snapshot"
CONF_ENABLE_MEDIA_BROWSER = "enable_media_browser"
CONF_ENABLE_LIVE = "enable_live"
CONF_LIVE_KEEPALIVE = "live_keepalive"
CONF_SETTINGS_INTERVAL = "settings_interval"
CONF_ALBUM_LIMIT = "album_limit"
CONF_SELFCHECK_INTERVAL = "selfcheck_interval"
CONF_EVENT_IMAGES = "event_images"
CONF_SNAPSHOT_INTERVAL = "snapshot_interval"
CONF_MESSAGE_IMAGES = "message_images"

DEFAULT_SCAN_INTERVAL = 30  # seconds; the device itself reports every ~30s
MIN_SCAN_INTERVAL = 10

# How long the live WebRTC session stays open after the last frame is requested.
# 0 means keep it open indefinitely (streams over cellular until stopped).
DEFAULT_LIVE_KEEPALIVE = 10

# How often to read the dashcam's own settings, in hours. Each read connects to
# the device, so this is off (0) unless the user asks for it.
DEFAULT_SETTINGS_INTERVAL = 0

# How many recordings each album folder lists. Kept small so the browser shows
# the recent ones instead of thousands of past events.
DEFAULT_ALBUM_LIMIT = 5
MAX_ALBUM_LIMIT = 100

# The Proof cloud's access token lasts about a week. Within this many hours of
# its expiry, try to renew it while it still works, and if the cloud refuses,
# ask for a fresh SMS login before anything stops working.
SESSION_RENEW_HOURS = 24
# How often to retry the renewal inside that window, in hours.
SESSION_RENEW_RETRY_HOURS = 1

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

# How often to run the self-check, in hours. Daily by default; 0 disables the
# schedule and leaves the button.
DEFAULT_SELFCHECK_INTERVAL = 24

# The signal bands the app shows on its self-check screen. Anything from
# "bad" down is treated as a problem.
GSM_BANDS = (
    (-96, "excellent"),
    (-104, "good"),
    (-113, "bad"),
)
GSM_WEAK_DBM = -104

# How many recent event snapshots to expose as image entities, so a dashboard
# can show them as a grid. 0 turns the grid off.
DEFAULT_EVENT_IMAGES = 6
MAX_EVENT_IMAGES = 12

# How often the snapshots refresh while the auto-refresh switch is on, in
# minutes. Each refresh wakes the dashcam and spends its mobile data, so this
# is deliberately not frequent.
DEFAULT_SNAPSHOT_INTERVAL = 15
MIN_SNAPSHOT_INTERVAL = 1

# How many photos attached to alerts to keep on hand. Some alerts (anti-theft,
# vibration, collision) leave a picture from each camera in the cloud album;
# ignition alerts do not. 0 turns the feature off.
DEFAULT_MESSAGE_IMAGES = 4
MAX_MESSAGE_IMAGES = 12

# How far either side of an alert to look for its pictures. The dashcam
# uploads them a little after the event, and the two clocks are not exact.
MESSAGE_PHOTO_WINDOW = 120

# --- RTSP output for NVRs ----------------------------------------------------
CONF_RTSP_MODE = "rtsp_mode"
CONF_RTSP_PORT = "rtsp_port"
CONF_RTSP_CAMERA = "rtsp_camera"
CONF_RTSP_USERNAME = "rtsp_username"
CONF_RTSP_PASSWORD = "rtsp_password"
CONF_RTSP_ALERTS = "rtsp_alerts"
CONF_RTSP_ALERT_MINUTES = "rtsp_alert_minutes"
CONF_RTSP_DRIVING = "rtsp_driving"
CONF_RTSP_DRIVING_GRACE = "rtsp_driving_grace"

# off: no RTSP server. trigger: live video only while something triggered it,
# a standby picture otherwise. always: live video whenever a client is pulling.
RTSP_MODE_OFF = "off"
RTSP_MODE_TRIGGER = "trigger"
RTSP_MODE_ALWAYS = "always"
RTSP_MODES = [RTSP_MODE_OFF, RTSP_MODE_TRIGGER, RTSP_MODE_ALWAYS]
DEFAULT_RTSP_MODE = RTSP_MODE_OFF

# Clear of go2rtc's usual 8554 (Frigate's add-on uses it), Home Assistant's
# own 8555/18554 and the CuboAI integration's 8556-8558.
DEFAULT_RTSP_PORT = 8564

RTSP_CAMERAS = ["front", "rear"]
DEFAULT_RTSP_CAMERA = "front"

# Alert types that can start live video, by the "type" the cloud gives each
# alert. Ignition ("acc") is the separate while-driving trigger.
RTSP_ALERT_TYPES = ["shake", "coll", "sos", "fence", "overspeed", "neutral"]
DEFAULT_RTSP_ALERTS = ["shake", "coll", "sos"]
DEFAULT_RTSP_ALERT_MINUTES = 5
DEFAULT_RTSP_DRIVING = True
DEFAULT_RTSP_DRIVING_GRACE = 2

# Options that are applied on the fly rather than by reloading the entry.
RUNTIME_OPTIONS = {CONF_RTSP_MODE}
