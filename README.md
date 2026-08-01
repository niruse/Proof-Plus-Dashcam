# Proof Plus — Home Assistant Integration

Custom integration for [Proof](https://2proof.co.il) 4G dashcams (IME/EDOG devices). It signs in
to the Proof cloud with the same account as the mobile app and exposes your dashcam's live
position and telemetry in Home Assistant.

Unlike the original YAML-based integration, this one uses the current **v5 cloud API**
(`app-p106.2proof.co.il`) and is fully configured from the UI (config flow, re-auth and options).

## Features

Per dashcam on your account:

| Entity | Description |
|---|---|
| `device_tracker` | Live GPS position (works with the map card and zones) |
| `binary_sensor` Online | Device connected to the cloud |
| `binary_sensor` Ignition | ACC / ignition state |
| `sensor` Speed | Current GPS speed (km/h) |
| `sensor` Total distance | Cumulative odometer reported by the device (km) |
| `sensor` Altitude / Heading | Last GPS fix details |
| `sensor` Device temperature | Internal device temperature (°C) |
| `sensor` GSM signal | Cellular signal strength (dBm) |
| `sensor` Last seen | Timestamp of the last report |

Redacted diagnostics are available from the integration page for troubleshooting.

### Optional video features (off by default)

Enable these under the integration's **Configure** options — nothing activates or streams
unless you turn it on:

- **Event snapshots** — adds an `image` entity per dashcam showing the most recent impact/event
  snapshot, with the event's GPS location. The image is only downloaded when Home Assistant
  renders it.
- **Media browser** — lists recorded impact and collision clips (image and video) under
  **Media → Proof Plus**, streamed straight from the Proof file server on demand.
- **Live view** — see the note under [Live view](#live-view) below.

## Installation

### HACS (recommended)

1. HACS → Integrations → ⋮ → **Custom repositories**
2. Add `https://github.com/niruse/Proof-Plus` as an **Integration**
3. Install **Proof Plus** and restart Home Assistant

### Manual

Copy `custom_components/proof` into your Home Assistant `config/custom_components/` folder and
restart Home Assistant.

## Configuration

1. Settings → Devices & Services → **Add Integration** → search for **Proof Plus**
2. Enter the **phone number** and password you use in the Proof mobile app
   (email addresses are not accepted by the Proof login service)
3. Enter the verification code that is sent to that number by SMS
4. All dashcams on the account are added automatically

You only enter a code once — the session is then kept alive with the refresh token. If the
session is ever rejected, Home Assistant asks you to re-authenticate with a new code.

The polling interval (default 30 s — the same rate the device reports at) can be changed under
the integration's **Configure** options.

## Live view

Live view is **not implemented yet**, but the mechanism has been fully reverse-engineered so it can
be added:

1. Open the `ws://…:8282/imclient` WebSocket and authenticate with the access token
   (`[2,0,{"token":…,"info":…}]`).
2. Send the device a request over that socket:
   `sendReqMessage("<did>#0", ["rtmp_start", <camera_index>, <push_url>], 30000)`, where
   `push_url = liveBase + getRtmpUrlStr(did)` and `liveBase` is `rtmp://aws7.2proof.co.il/live/`.
3. The device then pushes RTMP to that URL; play the matching
   `play_url = liveBase + getRtmpPlayUrlStr(did)`.

Both URL builders append `"Proof/" + base64url(AES-CBC("Proof|<did>|<epoch_ms>[|play]"))`, encrypted
with the app's separate `rtmpSecret`/`rtmpaesIv` constants.

It is deliberately left out for now because **each live view makes the dashcam stream over its own
cellular connection** (data cost and wake-up latency), and it needs a stateful WebSocket session that
should be verified against a real device before shipping. When added it will be an explicit,
off-by-default option, and streaming will only start when the camera is actually opened.

## Notes

- Cloud polling only; nothing is sent to third parties besides the official Proof cloud.
- If you change your Proof password, Home Assistant will prompt to re-authenticate.
- Devices added to your Proof account after setup appear after a Home Assistant restart
  (or by reloading the integration).
- The Proof v5 endpoints require a signed `x-sign` header; this integration generates it, so the
  system clock must be reasonably accurate (the signature embeds the current time).

### How it talks to the cloud

Established by analysing the mobile app's traffic and its (Hermes) JavaScript bundle:

- **Login:** `POST /api/app/v5/user/sendcode` `{"pn":"<phone>","type":"login","locale":"en_us"}`,
  then `POST /oauth/token` with `grant_type=app`, `client_id=app`, `client_secret=api1234`,
  `scope=SCOPE_READ`, `username`, `password` and `vcode`. `grant_type=refresh_token` renews it
  (the `scope` parameter is required, otherwise it fails with `invalid_scope`).
- **Request signing:** every `/api/app/v5/…` request carries an `x-sign` header equal to
  `base64( AES-128-CBC-PKCS7( "Proof|<epoch_ms>|<version>" ) )` with a fixed key and IV the app
  embeds. It encodes a timestamp, not the request body, so it must be regenerated per call and the
  client clock must be roughly correct. `/oauth/token` and `/user/sendcode` do not need it.
- **Signed endpoints:** `user/devices`, `user/profile`, `user/sysconfig`, and `cloud/files` (the
  media list — `type=shake` for impact clips, `type=coll` for collisions; the files themselves are
  then plain unsigned GETs from `fs-p106.2proof.co.il`).
- **WebSocket:** `ws://…:8282/imclient` authenticates with just the token
  (`[2,0,{"token":"…","info":{…}}]` → `[1,0,0,{"sid":"…"}]`, no signature). It carries WebRTC
  signalling to the device and generic RPC `[5,<seq>,["s.<method>",…]]`, so a live-video or command
  path may be reachable through it — a possible avenue for future features.
- The cloud appears to keep **one active token per account**, so signing in on the phone can
  invalidate Home Assistant's session and vice-versa.

## Credits

Inspired by [dimagoltsman/ha-proof-dashcam-integration](https://github.com/dimagoltsman/ha-proof-dashcam-integration),
which targeted the older Proof API. This integration was built by analyzing the current
Proof mobile app traffic (API v5).
