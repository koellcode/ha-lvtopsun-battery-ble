#!/usr/bin/env python3
"""LVTOPSUN Battery BLE → MQTT bridge for Home Assistant."""

import asyncio
import json
import logging
import os
import sys
import time

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CHAR_FF00_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"  # Write char
CHAR_FF01_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"  # Indicate char
FRAME_MAGIC = b"\x55\xAA"
_BLOCK_BASE = 24

LOG = logging.getLogger("lvtopsun")

# ---------------------------------------------------------------------------
# Options — loaded from /data/options.json (HA add-on convention)
# ---------------------------------------------------------------------------

def load_options():
    path = "/data/options.json"
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    # Fallback for local testing
    return {
        "device_name": os.environ.get("DEVICE_NAME", "LLM_UNAZAY_0008FR"),
        "scan_timeout": int(os.environ.get("SCAN_TIMEOUT", "10")),
        "connect_timeout": int(os.environ.get("CONNECT_TIMEOUT", "30")),
        "frame_timeout": int(os.environ.get("FRAME_TIMEOUT", "120")),
        "poll_interval": int(os.environ.get("POLL_INTERVAL", "30")),
        "retry_delay": int(os.environ.get("RETRY_DELAY", "10")),
        "mqtt_host": os.environ.get("MQTT_HOST", ""),
        "mqtt_port": int(os.environ.get("MQTT_PORT", "1883")),
        "mqtt_username": os.environ.get("MQTT_USERNAME", ""),
        "mqtt_password": os.environ.get("MQTT_PASSWORD", ""),
        "mqtt_topic": os.environ.get("MQTT_TOPIC", "lvtopsun_battery"),
        "log_level": os.environ.get("LOG_LEVEL", "info"),
    }


# ---------------------------------------------------------------------------
# MQTT broker discovery via Supervisor API
# ---------------------------------------------------------------------------

def discover_mqtt_from_supervisor():
    """Try to get MQTT config from the HA Supervisor API."""
    try:
        import urllib.request
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            return None
        req = urllib.request.Request(
            "http://supervisor/services/mqtt",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        d = data.get("data", {})
        return {
            "host": d.get("host", ""),
            "port": d.get("port", 1883),
            "username": d.get("username", ""),
            "password": d.get("password", ""),
        }
    except Exception as exc:
        LOG.debug("Supervisor MQTT discovery failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Frame reassembly
# ---------------------------------------------------------------------------

class FrameAssembler:
    def __init__(self, on_frame):
        self._buf = bytearray()
        self._expected_len = None
        self._on_frame = on_frame

    def feed(self, data: bytes):
        if data[:2] == FRAME_MAGIC:
            self._buf = bytearray(data)
            if len(self._buf) >= 4:
                payload_len = (self._buf[2] << 8) | self._buf[3]
                self._expected_len = payload_len + 4
            else:
                self._expected_len = None
        else:
            self._buf.extend(data)

        if self._expected_len is None and len(self._buf) >= 4:
            payload_len = (self._buf[2] << 8) | self._buf[3]
            self._expected_len = payload_len + 4

        if self._expected_len is not None and len(self._buf) >= self._expected_len:
            frame = bytes(self._buf[:self._expected_len])
            leftover = bytes(self._buf[self._expected_len:])
            self._buf = bytearray()
            self._expected_len = None
            self._on_frame(frame)
            if leftover:
                self.feed(leftover)


# ---------------------------------------------------------------------------
# Frame decoder — extract SOC
# ---------------------------------------------------------------------------

def _find_pack_voltage_offset(block, start=10, end=35):
    limit = min(end, len(block) - 1)
    for i in range(start, limit):
        val = (block[i] << 8) | block[i + 1]
        if 4000 <= val <= 7500:
            return i
    return None


def decode_soc(frame: bytes):
    """Return SOC percentage from a BMS frame, or None on failure."""
    if len(frame) < 70 or frame[:2] != FRAME_MAGIC:
        return None
    block = frame[_BLOCK_BASE:]
    v_off = _find_pack_voltage_offset(block)
    if v_off is None:
        return None
    if v_off + 5 < len(block):
        soc = block[v_off + 5]
        if 0 <= soc <= 100:
            return soc
    return None


def decode_pack_voltage(frame: bytes):
    """Return pack voltage in V from a BMS frame, or None."""
    if len(frame) < 70 or frame[:2] != FRAME_MAGIC:
        return None
    block = frame[_BLOCK_BASE:]
    v_off = _find_pack_voltage_offset(block)
    if v_off is None:
        return None
    raw = (block[v_off] << 8) | block[v_off + 1]
    return raw / 100.0


# ---------------------------------------------------------------------------
# MQTT helpers
# ---------------------------------------------------------------------------

async def build_mqtt_client(opts):
    mqtt_host = opts.get("mqtt_host") or ""
    mqtt_port = opts.get("mqtt_port", 1883)
    mqtt_user = opts.get("mqtt_username") or ""
    mqtt_pass = opts.get("mqtt_password") or ""

    # Auto-discover from Supervisor if host not set
    if not mqtt_host:
        sup = discover_mqtt_from_supervisor()
        if sup:
            mqtt_host = sup["host"]
            mqtt_port = sup["port"]
            mqtt_user = mqtt_user or sup["username"]
            mqtt_pass = mqtt_pass or sup["password"]
            LOG.info("MQTT via Supervisor: %s:%d user=%s",
                     mqtt_host, mqtt_port, mqtt_user or "(none)")

    if not mqtt_host:
        LOG.error("No MQTT host configured and Supervisor discovery failed")
        sys.exit(1)

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="lvtopsun-battery-ble",
    )
    if mqtt_user:
        client.username_pw_set(mqtt_user, mqtt_pass)

    connected_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_connect(_client, _userdata, _flags, rc, _properties=None):
        if rc == 0:
            LOG.info("MQTT connected to %s:%d", mqtt_host, mqtt_port)
            loop.call_soon_threadsafe(connected_event.set)
        else:
            LOG.error("MQTT connect failed: %s (user=%s)",
                      rc, mqtt_user or "(none)")

    client.on_connect = on_connect
    client.connect(mqtt_host, mqtt_port, keepalive=60)
    client.loop_start()

    try:
        await asyncio.wait_for(connected_event.wait(), timeout=10)
    except asyncio.TimeoutError:
        LOG.error("MQTT connection to %s:%d timed out (user=%s)",
                  mqtt_host, mqtt_port, mqtt_user or "(none)")
        sys.exit(1)

    return client


def publish_ha_discovery(mqttc, topic_base):
    """Publish MQTT discovery config so HA auto-creates the sensor."""
    device_info = {
        "identifiers": ["lvtopsun_battery_ble"],
        "name": "LVTOPSUN Battery",
        "manufacturer": "LVTOPSUN",
        "model": "16S LiFePO4",
    }
    soc_config = {
        "name": "LVTOPSUN Battery SOC",
        "unique_id": "lvtopsun_battery_soc",
        "state_topic": f"{topic_base}/state",
        "value_template": "{{ value_json.soc }}",
        "unit_of_measurement": "%",
        "device_class": "battery",
        "state_class": "measurement",
        "device": device_info,
        "availability_topic": f"{topic_base}/availability",
    }
    mqttc.publish(
        "homeassistant/sensor/lvtopsun_battery_soc/config",
        json.dumps(soc_config),
        retain=True,
    )
    LOG.info("Published HA MQTT discovery for SOC sensor")


def publish_state(mqttc, topic_base, soc):
    payload = json.dumps({"soc": soc})
    mqttc.publish(f"{topic_base}/state", payload, retain=True)
    LOG.info("Published SOC=%d%%", soc)


def publish_availability(mqttc, topic_base, online: bool):
    mqttc.publish(
        f"{topic_base}/availability",
        "online" if online else "offline",
        retain=True,
    )


# ---------------------------------------------------------------------------
# BLE connect + stream
# ---------------------------------------------------------------------------

async def find_device(name: str, timeout: float):
    LOG.info("Scanning for BLE device '%s' (timeout=%ds)...", name, timeout)
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for d, adv in devices.values():
        dname = d.name or adv.local_name or ""
        if name.lower() in dname.lower() or name.lower() in d.address.lower():
            LOG.info("Found device: %s addr=%s RSSI=%s",
                     dname, d.address, adv.rssi)
            return d
    # Fallback: try "ASR" (known BMS advertising name)
    if name.lower() != "asr":
        for d, adv in devices.values():
            dname = d.name or adv.local_name or ""
            if "asr" == dname.lower():
                LOG.info("Found device by fallback 'ASR': %s addr=%s RSSI=%s",
                         dname, d.address, adv.rssi)
                return d
    return None


async def clear_bluez_cache(address: str):
    """Remove device from BlueZ to clear cached GATT database."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl", "remove", address,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        LOG.info("bluetoothctl remove %s: rc=%d %s",
                 address, proc.returncode,
                 (stdout or stderr or b"").decode().strip())
    except Exception as exc:
        LOG.debug("bluetoothctl remove failed (non-fatal): %s", exc)


async def connect_and_stream(opts, mqttc, topic_base, last_soc, last_pub_ts):
    """Scan, connect, subscribe FF01, stream frames, publish SOC.

    Each attempt does: clear cache → scan → connect → subscribe → stream.
    After subscribing, writes a trigger byte to FF00 to kick the BMS
    into sending telemetry (some BMS firmware requires this on BlueZ).
    """
    frame_timeout = max(float(opts.get("frame_timeout", 120)), 5.0)
    connect_timeout = max(float(opts.get("connect_timeout", 30)), 5.0)
    publish_interval = max(float(opts.get("poll_interval", 30)), 1.0)
    device_name = opts["device_name"]
    scan_timeout = opts["scan_timeout"]

    disconnected_event = asyncio.Event()
    frame_queue: asyncio.Queue = asyncio.Queue()
    last_frame_ts = time.time()

    def on_frame(frame: bytes):
        nonlocal last_frame_ts
        soc = decode_soc(frame)
        if soc is None:
            LOG.debug("Ignoring %d-byte payload: not decodable", len(frame))
            return
        voltage = decode_pack_voltage(frame)
        if voltage is not None:
            LOG.info("Decoded SOC=%d%%, pack=%.2fV", soc, voltage)
        else:
            LOG.info("Decoded SOC=%d%%", soc)
        last_frame_ts = time.time()
        frame_queue.put_nowait((soc, voltage, time.time()))

    assembler = FrameAssembler(on_frame=on_frame)

    def on_notify(_char: BleakGATTCharacteristic, data: bytearray):
        LOG.debug("BLE indication: %d bytes", len(data))
        assembler.feed(bytes(data))

    def on_disconnect(_client):
        LOG.warning("BLE disconnected")
        disconnected_event.set()

    last_exc = None
    last_address = None
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        try:
            # Clear BlueZ cache before scanning (if we know the address).
            if last_address:
                await clear_bluez_cache(last_address)

            # Fresh scan each attempt so BlueZ re-discovers the device.
            device = await find_device(device_name, scan_timeout)
            if device is None:
                LOG.warning("Device '%s' not found (attempt %d/%d)",
                            device_name, attempt, max_attempts)
                if attempt < max_attempts:
                    await asyncio.sleep(2)
                continue
            last_address = device.address

            LOG.info("BLE connect attempt %d/%d to %s",
                     attempt, max_attempts, device.address)
            disconnected_event.clear()

            async with BleakClient(
                device,
                timeout=connect_timeout,
                disconnected_callback=on_disconnect,
            ) as client:
                LOG.info("BLE connected")

                # Log FF01 characteristic properties for debugging.
                char = client.services.get_characteristic(
                    CHAR_FF01_UUID)
                if char:
                    LOG.info("FF01 properties: %s", char.properties)
                else:
                    LOG.warning("FF01 characteristic not found!")

                # Let BMS settle after connect (name changes
                # to "ASR" ~0.5s after connect).
                await asyncio.sleep(1.0)

                # Read FF01 before subscribing — matches the
                # macOS proxy sequence where the first operation
                # is a read returning "C2 Value".
                try:
                    init_val = await client.read_gatt_char(
                        CHAR_FF01_UUID)
                    LOG.info("FF01 initial read: %d bytes: %s",
                             len(init_val), init_val.hex(" "))
                except Exception as e:
                    LOG.warning("FF01 initial read failed: %s", e)

                await client.start_notify(CHAR_FF01_UUID, on_notify)
                LOG.info("Subscribed to FF01 indications")

                # Write trigger byte to FF00 after subscribe.
                try:
                    await client.write_gatt_char(
                        CHAR_FF00_UUID, b"\x01")
                    LOG.info("Wrote trigger to FF00")
                except Exception as e:
                    LOG.warning("FF00 trigger write failed: %s", e)

                LOG.info("Waiting for frames (timeout=%.0fs)",
                         frame_timeout)

                # Clear disconnect flag right before streaming — BlueZ
                # may fire disconnect during connect/service-discovery.
                disconnected_event.clear()
                last_frame_ts = time.time()

                try:
                    diag_done = False
                    while (client.is_connected
                           and not disconnected_event.is_set()):
                        try:
                            soc, _v, rx_ts = await asyncio.wait_for(
                                frame_queue.get(), timeout=5.0,
                            )
                            if (soc != last_soc
                                    or (rx_ts - last_pub_ts)
                                    >= publish_interval):
                                publish_state(mqttc, topic_base, soc)
                                publish_availability(
                                    mqttc, topic_base, True)
                                last_soc = soc
                                last_pub_ts = rx_ts
                        except asyncio.TimeoutError:
                            idle = time.time() - last_frame_ts
                            # Diagnostic: if no indication after 10s,
                            # try a manual read of FF01.
                            if idle >= 10 and not diag_done:
                                diag_done = True
                                try:
                                    val = await client.read_gatt_char(
                                        CHAR_FF01_UUID)
                                    LOG.info(
                                        "Diagnostic FF01 read: %d "
                                        "bytes: %s",
                                        len(val), val.hex(" "))
                                except Exception as e:
                                    LOG.warning(
                                        "Diagnostic FF01 read "
                                        "failed: %s", e)
                            if idle >= frame_timeout:
                                LOG.warning("No frame for %.0fs; "
                                            "reconnecting", idle)
                                break
                finally:
                    try:
                        if client.is_connected:
                            await client.stop_notify(CHAR_FF01_UUID)
                    except Exception:
                        pass

            last_exc = None
            break

        except Exception as exc:
            last_exc = exc
            LOG.warning("BLE attempt %d failed: %s", attempt, exc)
            if attempt < max_attempts:
                await asyncio.sleep(2)

    if last_exc is not None:
        LOG.error("BLE error after %d attempts: %s", max_attempts, last_exc)
    return last_soc, last_pub_ts


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run():
    opts = load_options()

    level = getattr(logging, opts.get("log_level", "info").upper(),
                    logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    # Always enable debug for bleak to trace BLE subscription issues.
    logging.getLogger("bleak").setLevel(logging.DEBUG)

    LOG.info("Starting LVTOPSUN Battery BLE add-on")
    LOG.info("Device: %s  Poll: %ds  Frame timeout: %ds",
             opts["device_name"], opts["poll_interval"],
             opts.get("frame_timeout", 120))

    topic_base = opts.get("mqtt_topic", "lvtopsun_battery")
    mqttc = await build_mqtt_client(opts)
    publish_ha_discovery(mqttc, topic_base)
    publish_availability(mqttc, topic_base, True)

    retry_delay = max(int(opts.get("retry_delay", 10)), 1)
    last_soc = None
    last_pub_ts = 0.0

    try:
        while True:
            last_soc, last_pub_ts = await connect_and_stream(
                opts, mqttc, topic_base, last_soc, last_pub_ts,
            )
            LOG.info("Reconnecting in %ds", retry_delay)
            await asyncio.sleep(retry_delay)
    except asyncio.CancelledError:
        pass
    finally:
        publish_availability(mqttc, topic_base, False)
        mqttc.loop_stop()
        mqttc.disconnect()
        LOG.info("Shutting down")


if __name__ == "__main__":
    asyncio.run(run())
