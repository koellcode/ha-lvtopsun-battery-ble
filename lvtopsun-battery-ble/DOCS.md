# LVTOPSUN Battery BLE

Reads SOC (State of Charge) from a LVTOPSUN LiFePO4 battery via BLE and publishes it to MQTT with Home Assistant auto-discovery.

## Installation

1. Copy the `lvtopsun-battery-ble` folder into your Home Assistant `addons` directory (e.g. via Samba or SSH).
2. In HA go to **Settings → Add-ons → Add-on Store** → three-dot menu → **Check for updates**.
3. The add-on appears under **Local add-ons**. Click it → **Install** → **Start**.

## Configuration

| Option | Default | Description |
|---|---|---|
| `device_name` | `LLM_UNAZAY_0008FR` | BLE device name (or substring) to search for |
| `scan_timeout` | `10` | BLE scan timeout in seconds |
| `connect_timeout` | `15` | BLE connection timeout in seconds |
| `poll_interval` | `30` | Seconds between SOC reads |
| `mqtt_host` | *(auto)* | MQTT broker host. Leave empty to auto-discover from Mosquitto add-on |
| `mqtt_port` | `1883` | MQTT broker port |
| `mqtt_username` | *(auto)* | MQTT username |
| `mqtt_password` | *(auto)* | MQTT password |
| `mqtt_topic` | `lvtopsun_battery` | MQTT topic prefix |
| `log_level` | `info` | Log level: debug, info, warning, error |

## MQTT Topics

- `lvtopsun_battery/state` — JSON `{"soc": 97}`
- `lvtopsun_battery/availability` — `online` / `offline`
- HA auto-discovery on `homeassistant/sensor/lvtopsun_battery_soc/config`

## Requirements

- Home Assistant OS or Supervised install (for Supervisor API / D-Bus access)
- Bluetooth adapter on the HA host within range of the battery
- MQTT broker (Mosquitto add-on recommended)
