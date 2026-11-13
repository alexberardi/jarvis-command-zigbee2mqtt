# Zigbee (via Zigbee2MQTT)

Zigbee device control for [Jarvis](https://github.com/alexberardi/jarvis) through [Zigbee2MQTT](https://www.zigbee2mqtt.io/). It discovers and controls the switches, lights, locks, thermostats and covers you have paired in Zigbee2MQTT. It is the Zigbee counterpart of the Z-Wave (via Z-Wave JS UI) package.

## How It Works

1. **Zigbee2MQTT** manages your Zigbee coordinator and mesh. You pair devices in its web UI.
2. This package connects to Zigbee2MQTT's built-in **frontend WebSocket** (`ws://host:8080/api`). No separate MQTT broker credentials are needed.
3. On connect, Zigbee2MQTT sends the device list, current states and availability, and these are cached.
4. Control sends a `<device>/set` message and waits briefly for the device to report its new state. If Zigbee2MQTT logs an error for that device, it is returned as the failure reason.

## Supported Domains

| Domain | Actions | Zigbee2MQTT expose |
|--------|---------|--------------------|
| switch | turn_on, turn_off, toggle | `switch` |
| light | turn_on (optional brightness), turn_off, set_brightness, toggle | `light` |
| lock | lock, unlock | `lock` |
| climate | set_temperature, set_hvac_mode | `climate` |
| cover | open_cover, close_cover, stop_cover | `cover` |

Brightness is 0-100% in Jarvis and is converted to the device's native range (usually 0-254).

## Setup

1. Run Zigbee2MQTT with a Zigbee coordinator and pair your devices.
2. Make sure the frontend is enabled in Zigbee2MQTT's `configuration.yaml` (default port 8080):
   ```yaml
   frontend:
     port: 8080
   ```
3. Install this package from the Jarvis Pantry.
4. Set the **Zigbee2MQTT URL** secret, e.g. `ws://10.0.0.50:8080`. The `/api` path is added automatically, and `http://` URLs are accepted too.
5. If you set `auth_token` under `frontend:`, enter it as **Zigbee2MQTT Auth Token**. Leave it empty otherwise.
6. Run **Scan for Devices** in the Jarvis app.

## Usage Examples

Once devices are discovered, you can use them with the device control commands:

- "Turn on the living room lamp"
- "Set the kitchen light to 40 percent"
- "Lock the front door"
- "Set the bedroom thermostat to 68"
- "Open the office blinds"

## Components

- **Zigbee2MqttProtocol** (`IJarvisDeviceProtocol`): discover, control and query Zigbee devices
- **Zigbee2MqttAgent** (`IJarvisAgent`): refreshes the cache every 5 minutes and provides voice context
- **zigbee2mqtt_shared/service.py**: singleton WebSocket client shared by both

## Notes

- Entity IDs are based on the device's IEEE address (`zigbee_<ieee>`), so renaming a device in Zigbee2MQTT does not break it. Run a scan after renaming to refresh the display name.
- Sensors and other read-only devices are not exposed yet.
- Battery-powered devices may not confirm a command immediately. The command is still reported as sent.

## Dependencies

- `websockets`

## License

MIT