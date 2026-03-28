# LVTOPSUN Battery BLE

Reads SOC (State of Charge) from a LVTOPSUN LiFePO4 battery via BLE and publishes it to MQTT with Home Assistant auto-discovery.

## Installation

1. Copy the `lvtopsun-battery-ble` folder into your Home Assistant `addons` directory (e.g. via Samba or SSH).
2. In HA go to **Settings → Add-ons → Add-on Store** → three-dot menu → **Check for updates**.
3. The add-on appears under **Local add-ons**. Click it → **Install** → **Start**.

## Configuration

| Option                    | Default                | Description                                                          |
| ------------------------- | ---------------------- | -------------------------------------------------------------------- |
| `device_name`             | `LLM_UNAZAY_0008FR`    | BLE device name (or substring) to search for                         |
| `scan_timeout`            | `10`                   | BLE scan timeout in seconds                                          |
| `connect_timeout`         | `30`                   | BLE connection timeout in seconds                                    |
| `frame_timeout`           | `120`                  | Maximum time to wait for a telemetry frame before reconnecting       |
| `first_burst_timeout`     | `12`                   | Seconds to wait for the first BLE indication after connecting        |
| `poll_interval`           | `30`                   | Minimum interval between MQTT publishes for unchanged SOC            |
| `retry_delay`             | `10`                   | Delay before opening a new BLE session after disconnect              |
| `probe_interval`          | `5`                    | Interval used while waiting for frame queue activity                 |
| `subscribe_settle_delay`  | `0.0`                  | Optional delay before subscribing to `FF01`                          |
| `post_subscribe_delay`    | `0.0`                  | Optional delay after subscribing before entering stream loop         |
| `inspect_ff01_descriptors`| `true`                 | Read and log `FF01` descriptor handles and values after subscribe    |
| `force_ff01_cccd_indicate`| `false`                | Opt-in debug: explicitly write `0x02 0x00` to the `FF01` CCCD        |
| `ff00_request_hex`        | _(empty)_              | Optional hex payload to write to `FF00` as a telemetry request       |
| `ff00_request_timing`     | `before-subscribe`     | When to send `ff00_request_hex`: before or after `FF01` subscribe    |
| `ff00_request_response`   | `false`                | Whether the `FF00` write should request a write response             |
| `mqtt_host`               | _(auto)_               | MQTT broker host. Leave empty to auto-discover from Mosquitto add-on |
| `mqtt_port`               | `1883`                 | MQTT broker port                                                     |
| `mqtt_username`           | _(auto)_               | MQTT username                                                        |
| `mqtt_password`           | _(auto)_               | MQTT password                                                        |
| `mqtt_topic`              | `lvtopsun_battery`     | MQTT topic prefix                                                    |
| `log_level`               | `info`                 | Log level: debug, info, warning, error                               |

## Current Linux Status

The add-on now reliably reaches BLE subscribe and can hold a stream session for several seconds on Home Assistant OS / BlueZ. The remaining blocker is that some sessions disconnect before the first telemetry frame arrives.

The add-on includes BlueZ state tracing and an optional `FF00` request hook because this battery may require a proprietary request packet before it starts sending telemetry on Linux.

It also includes `FF01` descriptor diagnostics so you can verify whether BlueZ leaves the Client Characteristic Configuration Descriptor (`0x2902`) in the expected indication-enabled state.

## Testing An `FF00` Request

1. Capture the phone app's first write to characteristic `0000ff00-0000-1000-8000-00805f9b34fb`.
2. Set `ff00_request_hex` to that payload in the add-on config.
3. Start with `ff00_request_timing: before-subscribe`.
4. Start with `ff00_request_response: false`.
5. If the session still disconnects before frames arrive, retry with `ff00_request_timing: after-subscribe`.

## Testing `FF01` CCCD Behavior

1. Leave `inspect_ff01_descriptors: true` to log the `FF01` characteristic handle, properties, and descriptor values after subscribe.
2. If Linux still subscribes successfully but receives `notifications=0`, enable `force_ff01_cccd_indicate: true` for one test run.
3. Check whether the log shows the `0x2902` descriptor and whether forcing `02 00` changes the behavior.

Example debug config:

```yaml
inspect_ff01_descriptors: true
force_ff01_cccd_indicate: true
```

Example:

```yaml
ff00_request_hex: "55AA01020304"
ff00_request_timing: "before-subscribe"
ff00_request_response: false
```

## MQTT Topics

- `lvtopsun_battery/state` — JSON `{"soc": 97}`
- `lvtopsun_battery/availability` — `online` / `offline`
- HA auto-discovery on `homeassistant/sensor/lvtopsun_battery_soc/config`

## Requirements

- Home Assistant OS or Supervised install (for Supervisor API / D-Bus access)
- Bluetooth adapter on the HA host within range of the battery
- MQTT broker (Mosquitto add-on recommended)
