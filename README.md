# Proof Dashcam — Home Assistant Integration

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

## Installation

### HACS (recommended)

1. HACS → Integrations → ⋮ → **Custom repositories**
2. Add `https://github.com/niruse/Proof` as an **Integration**
3. Install **Proof Dashcam** and restart Home Assistant

### Manual

Copy `custom_components/proof` into your Home Assistant `config/custom_components/` folder and
restart Home Assistant.

## Configuration

1. Settings → Devices & Services → **Add Integration** → search for **Proof Dashcam**
2. Enter the **phone number** and password you use in the Proof mobile app
   (email addresses are not accepted by the Proof login service)
3. Enter the verification code that is sent to that number by SMS
4. All dashcams on the account are added automatically

You only enter a code once — the session is then kept alive with the refresh token. If the
session is ever rejected, Home Assistant asks you to re-authenticate with a new code.

## Status: blocked on request signing

The login flow above works, but the Proof **v5 data endpoints additionally require an `x-sign`
request header** that the mobile app computes locally. Without it the server answers every request
with `{"code":1,"data":"System error: null"}` — an internal error raised before the token is even
checked (an invalid token on a correctly signed request returns a clean `401` instead).

What is known about the header so far:

- It is 32 bytes, Base64-encoded, and **changes on every request**, including repeats of an
  identical URL and token.
- Two requests to the same URL produced values whose **first 16 bytes are byte-identical** while
  the last 16 bytes differ. A hash of the request could not do that, so this is a two-block
  cipher (16-byte blocks): the first block is derived from the request, the second varies per
  call — most likely a timestamp or nonce.
- Sending the header empty, omitting it, or replaying a previously valid value all fail
  identically, so the value is genuinely verified.

Recovering the algorithm needs the key from the Proof app binary. Until then the integration
authenticates successfully but cannot read device data.

The polling interval (default 30 s — the same rate the device reports at) can be changed under
the integration's **Configure** options.

## Notes

- Cloud polling only; nothing is sent to third parties besides the official Proof cloud.
- If you change your Proof password, Home Assistant will prompt to re-authenticate.
- Devices added to your Proof account after setup appear after a Home Assistant restart
  (or by reloading the integration).

### Notes for further work

The following are established from traffic captures of the mobile app and may help finish this:

- **Login:** `POST /api/app/v5/user/sendcode` `{"pn":"<phone>","type":"login","locale":"en_us"}`,
  then `POST /oauth/token` with `grant_type=app`, `client_id=app`, `client_secret=api1234`,
  `scope=SCOPE_READ`, `username`, `password` and `vcode`. `grant_type=refresh_token` renews it
  (the `scope` parameter is required, otherwise it fails with `invalid_scope`).
- **Not signed:** `/oauth/token`, `/api/app/v5/user/sendcode` and the `ws://…:8282/imclient`
  WebSocket, which authenticates with `[2,0,{"token":"…","info":{…}}]` and replies
  `[1,0,0,{"sid":"…"}]`. That socket carries WebRTC signalling to the device and generic RPC
  (`[5,<seq>,["s.<method>",…]]`), so a live-video or command path may be reachable through it.
- **Signed:** everything under `/api/app/v5/…`, including `user/devices`, `user/profile` and
  `cloud/files` (the media list, with `type=shake` for impact clips and `type=coll` for
  collisions; the files themselves are then plain unsigned GETs from `fs-p106.2proof.co.il`).
- The cloud appears to keep **one active token per account**, so a new login elsewhere can
  invalidate the session this integration holds.

## Credits

Inspired by [dimagoltsman/ha-proof-dashcam-integration](https://github.com/dimagoltsman/ha-proof-dashcam-integration),
which targeted the older Proof API. This integration was built by analyzing the current
Proof mobile app traffic (API v5).
