#!/usr/bin/env python3
"""LVTOPSUN Battery BLE → MQTT bridge for Home Assistant."""

import asyncio
import json
import logging
import os
import sys
import time

from bleak import BleakClient, BleakScanner
import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
FRAME_MAGIC = b"\x55\xAA"
_BLOCK_BASE = 24

# GATT UUIDs and handles
CHAR_FF01_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
FF01_CCCD_HANDLE  = 0x0028   # FF01 Client Characteristic Config descriptor

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


async def _run_bleak(address: str, connect_timeout: float,
                    frame_timeout: float, assembler, frame_queue,
                    publish_interval, mqttc, topic_base,
                    last_soc, last_pub_ts):
    """Connect via bleak, write CCCD manually, stream indications.

    Instead of using start_notify() (which triggers BlueZ D-Bus
    StartNotify and caused BMS disconnects), we write the CCCD
    descriptor directly and rely on bleak's internal notification
    routing to deliver indication callbacks.
    """
    got_data = False
    data_event = asyncio.Event()

    def on_indication(_char, data: bytearray):
        nonlocal got_data, last_soc, last_pub_ts
        got_data = True
        data_event.set()
        LOG.info("Indication %d bytes", len(data))
        assembler.feed(bytes(data))

        while not frame_queue.empty():
            try:
                soc, _v, rx_ts = frame_queue.get_nowait()
                if (soc != last_soc
                        or (rx_ts - last_pub_ts) >= publish_interval):
                    publish_state(mqttc, topic_base, soc)
                    publish_availability(mqttc, topic_base, True)
                    last_soc = soc
                    last_pub_ts = rx_ts
            except asyncio.QueueEmpty:
                break

    async with BleakClient(address, timeout=connect_timeout) as client:
        LOG.info("Connected to %s (MTU=%d)", address, client.mtu_size)

        # Subscribe using start_notify — this writes CCCD and registers
        # the callback. If the BMS disconnects us, we'll catch it.
        LOG.info("Subscribing to FF01 indications...")
        await client.start_notify(CHAR_FF01_UUID, on_indication)
        LOG.info("Subscribed to FF01")

        LOG.info("Waiting for indications (timeout=%.0fs)...", frame_timeout)
        deadline = time.time() + frame_timeout

        while client.is_connected:
            remaining = deadline - time.time()
            if remaining <= 0:
                if not got_data:
                    LOG.warning("No indication for %.0fs; reconnecting",
                                frame_timeout)
                break

            data_event.clear()
            try:
                await asyncio.wait_for(data_event.wait(),
                                       timeout=min(remaining, 10.0))
                # Got data — extend deadline
                deadline = time.time() + frame_timeout
            except asyncio.TimeoutError:
                pass

        if not client.is_connected:
            LOG.warning("BMS disconnected")

    return last_soc, last_pub_ts, got_data


async def connect_and_stream(opts, mqttc, topic_base, address,
                             last_soc, last_pub_ts):
    """Connect via bleak, stream indications, publish SOC.

    Uses bleak with start_notify() for indication subscription.
    Retries with bluez cache clear if no data is received.
    """
    frame_timeout = max(float(opts.get("frame_timeout", 180)), 30.0)
    connect_timeout = max(float(opts.get("connect_timeout", 30)), 5.0)
    publish_interval = max(float(opts.get("poll_interval", 30)), 1.0)

    frame_queue: asyncio.Queue = asyncio.Queue()

    def on_frame(frame: bytes):
        soc = decode_soc(frame)
        if soc is None:
            LOG.debug("Ignoring %d-byte payload: not decodable", len(frame))
            return
        voltage = decode_pack_voltage(frame)
        if voltage is not None:
            LOG.info("Decoded SOC=%d%%, pack=%.2fV", soc, voltage)
        else:
            LOG.info("Decoded SOC=%d%%", soc)
        frame_queue.put_nowait((soc, voltage, time.time()))

    assembler = FrameAssembler(on_frame=on_frame)

    last_exc = None
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        try:
            if attempt > 1:
                await clear_bluez_cache(address)
                await asyncio.sleep(10)

            LOG.info("BLE connect attempt %d/%d to %s",
                     attempt, max_attempts, address)

            last_soc, last_pub_ts, got_data = await _run_bleak(
                address, connect_timeout, frame_timeout,
                assembler, frame_queue, publish_interval,
                mqttc, topic_base, last_soc, last_pub_ts,
            )
            if got_data:
                last_exc = None
                break
            LOG.warning("Attempt %d: no indications received",
                        attempt)

        except Exception as exc:
            last_exc = exc
            LOG.warning("Attempt %d failed: %s", attempt, exc)

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

    LOG.info("Starting LVTOPSUN Battery BLE add-on")
    LOG.info("Device: %s  Poll: %ds  Frame timeout: %ds",
             opts["device_name"], opts["poll_interval"],
             opts.get("frame_timeout", 180))

    topic_base = opts.get("mqtt_topic", "lvtopsun_battery")
    mqttc = await build_mqtt_client(opts)
    publish_ha_discovery(mqttc, topic_base)
    publish_availability(mqttc, topic_base, True)

    retry_delay = max(int(opts.get("retry_delay", 10)), 1)
    last_soc = None
    last_pub_ts = 0.0

    # Scan once at startup to discover the MAC address
    device_name = opts["device_name"]
    scan_timeout = opts["scan_timeout"]
    device = await find_device(device_name, scan_timeout)
    while device is None:
        LOG.warning("Device '%s' not found, retrying in %ds",
                    device_name, retry_delay)
        await asyncio.sleep(retry_delay)
        device = await find_device(device_name, scan_timeout)
    address = device.address

    try:
        while True:
            last_soc, last_pub_ts = await connect_and_stream(
                opts, mqttc, topic_base, address, last_soc, last_pub_ts,
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
