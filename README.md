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
2. Sign in with the **phone number** and password you use in the Proof mobile app
   (email addresses are not accepted by the Proof login service)
3. All dashcams on the account are added automatically

The polling interval (default 30 s — the same rate the device reports at) can be changed under
the integration's **Configure** options.

## Notes

- Cloud polling only; nothing is sent to third parties besides the official Proof cloud.
- If you change your Proof password, Home Assistant will prompt to re-authenticate.
- Devices added to your Proof account after setup appear after a Home Assistant restart
  (or by reloading the integration).

## Credits

Inspired by [dimagoltsman/ha-proof-dashcam-integration](https://github.com/dimagoltsman/ha-proof-dashcam-integration),
which targeted the older Proof API. This integration was built by analyzing the current
Proof mobile app traffic (API v5).
