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
CHAR_FF01_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
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
        "connect_timeout": int(os.environ.get("CONNECT_TIMEOUT", "15")),
        "frame_timeout": int(os.environ.get("FRAME_TIMEOUT", "120")),
        "poll_interval": int(os.environ.get("POLL_INTERVAL", "30")),
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

def build_mqtt_client(opts):
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
            LOG.info("MQTT discovered via Supervisor: %s:%d", mqtt_host, mqtt_port)

    if not mqtt_host:
        LOG.error("No MQTT host configured and Supervisor discovery failed")
        sys.exit(1)

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="lvtopsun-battery-ble",
    )
    if mqtt_user:
        client.username_pw_set(mqtt_user, mqtt_pass)
    client.connect(mqtt_host, mqtt_port, keepalive=60)
    client.loop_start()
    LOG.info("MQTT connected to %s:%d", mqtt_host, mqtt_port)
    return client


def publish_ha_discovery(mqttc, topic_base):
    """Publish MQTT discovery config so HA auto-creates the sensor."""
    device_info = {
        "identifiers": ["lvtopsun_battery_ble"],
        "name": "LVTOPSUN Battery",
        "manufacturer": "LVTOPSUN",
        "model": "16S LiFePO4",
    }

    # SOC sensor
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
# BLE read loop
# ---------------------------------------------------------------------------

async def find_device(name: str, timeout: float):
    LOG.info("Scanning for BLE device '%s' (timeout=%ds)...", name, timeout)
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for d, adv in devices.values():
        dname = d.name or adv.local_name or ""
        if name.lower() in dname.lower() or name.lower() in d.address.lower():
            LOG.info("Found device: %s addr=%s RSSI=%s", dname, d.address, adv.rssi)
            return d
    return None


def process_candidate_frame(data: bytes, result: dict, got_frame: asyncio.Event, source: str):
    soc = decode_soc(data)
    if soc is None:
        LOG.debug("Ignoring %s payload (%d bytes): not a decodable telemetry frame", source, len(data))
        return
    result["soc"] = soc
    voltage = decode_pack_voltage(data)
    if voltage is None:
        LOG.info("Decoded SOC=%d%% from %s payload", soc, source)
    else:
        LOG.info("Decoded SOC=%d%%, pack_voltage=%.2fV from %s payload", soc, voltage, source)
    got_frame.set()


async def read_once(opts):
    """Connect, subscribe, wait for one complete frame, return SOC or None."""
    device = await find_device(opts["device_name"], opts["scan_timeout"])
    if device is None:
        LOG.warning("Device '%s' not found", opts["device_name"])
        return None

    result = {}
    got_frame = asyncio.Event()
    frame_timeout = max(float(opts.get("frame_timeout", 120)), 5.0)

    def on_frame(frame: bytes):
        process_candidate_frame(frame, result, got_frame, "notify")

    assembler = FrameAssembler(on_frame=on_frame)

    def on_notify(_char: BleakGATTCharacteristic, data: bytearray):
        assembler.feed(bytes(data))

    try:
        async with BleakClient(
            device.address, timeout=opts["connect_timeout"]
        ) as client:
            LOG.debug("BLE connected: %s", client.is_connected)
            await client.start_notify(CHAR_FF01_UUID, on_notify)

            # Some batteries do not auto-stream immediately on Linux/BlueZ.
            # Probe FF01 directly while subscribed to trigger or retrieve data.
            try:
                deadline = asyncio.get_running_loop().time() + frame_timeout
                attempt = 0
                while not got_frame.is_set():
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    attempt += 1
                    if got_frame.is_set():
                        break
                    try:
                        value = await client.read_gatt_char(CHAR_FF01_UUID)
                        LOG.debug("FF01 read attempt %d returned %d bytes", attempt, len(value))
                        process_candidate_frame(bytes(value), result, got_frame, f"read-{attempt}")
                    except Exception as exc:
                        LOG.debug("FF01 read attempt %d failed: %s", attempt, exc)

                    if got_frame.is_set():
                        break

                    try:
                        await asyncio.wait_for(got_frame.wait(), timeout=min(15.0, remaining))
                    except asyncio.TimeoutError:
                        LOG.debug("No notify frame received after FF01 read attempt %d", attempt)

                if not got_frame.is_set():
                    LOG.warning(
                        "Timed out waiting %.1fs for BMS frame after notify + direct reads",
                        frame_timeout,
                    )
            finally:
                try:
                    if client.is_connected:
                        await client.stop_notify(CHAR_FF01_UUID)
                except Exception as exc:
                    LOG.debug("Ignoring stop_notify cleanup failure: %s", exc)
    except Exception as exc:
        LOG.error("BLE error: %s", exc)
        return None

    return result.get("soc")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run():
    opts = load_options()

    level = getattr(logging, opts.get("log_level", "info").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    LOG.info("Starting LVTOPSUN Battery BLE add-on")
    LOG.info(
        "Device: %s  Poll interval: %ds  Frame timeout: %ss",
        opts["device_name"],
        opts["poll_interval"],
        opts.get("frame_timeout", 120),
    )

    topic_base = opts.get("mqtt_topic", "lvtopsun_battery")
    mqttc = build_mqtt_client(opts)
    publish_ha_discovery(mqttc, topic_base)
    publish_availability(mqttc, topic_base, True)

    consecutive_failures = 0
    max_backoff = 300  # 5 min max

    try:
        while True:
            soc = await read_once(opts)
            if soc is not None:
                publish_state(mqttc, topic_base, soc)
                publish_availability(mqttc, topic_base, True)
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                publish_availability(mqttc, topic_base, False)
                LOG.warning(
                    "Read failed (%d consecutive). Will retry.",
                    consecutive_failures,
                )

            # Backoff: normal interval, or exponential on failure (capped)
            if consecutive_failures == 0:
                delay = opts["poll_interval"]
            else:
                delay = min(
                    opts["poll_interval"] * (2 ** consecutive_failures),
                    max_backoff,
                )
            LOG.debug("Sleeping %ds before next poll", delay)
            await asyncio.sleep(delay)
    except asyncio.CancelledError:
        pass
    finally:
        publish_availability(mqttc, topic_base, False)
        mqttc.loop_stop()
        mqttc.disconnect()
        LOG.info("Shutting down")


if __name__ == "__main__":
    asyncio.run(run())
