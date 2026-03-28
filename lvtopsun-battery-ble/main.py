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
CHAR_FF00_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"  # write (command)
CHAR_FF01_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"  # indicate (response)
FF01_CCCD_HANDLE  = 0x0028   # FF01 Client Characteristic Config descriptor

LOG = logging.getLogger("lvtopsun")

DISCOVERY_FAILURE_MARKERS = (
    "failed to discover services",
    "unlikely error",
    "not found",
)

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

    preferred_asr = None
    preferred_name = None
    for d, adv in devices.values():
        dname = d.name or adv.local_name or ""
        visible_name = (d.name or "").lower()
        address = d.address.lower()

        if visible_name == "asr":
            preferred_asr = (d, adv)
        if name.lower() in dname.lower() or name.lower() in address:
            preferred_name = (d, adv)

    if preferred_asr is not None:
        d, adv = preferred_asr
        dname = d.name or adv.local_name or ""
        LOG.info("Found device by preferred name 'ASR': %s addr=%s RSSI=%s",
                 dname, d.address, adv.rssi)
        return d

    if preferred_name is not None:
        d, adv = preferred_name
        dname = d.name or adv.local_name or ""
        LOG.info("Found device: %s addr=%s RSSI=%s",
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
        output = (stdout or stderr or b"").decode().strip().splitlines()
        summary = output[-1] if output else ""
        LOG.debug("bluetoothctl remove %s: rc=%d %s",
                  address, proc.returncode, summary)
    except Exception as exc:
        LOG.debug("bluetoothctl remove failed (non-fatal): %s", exc)


def should_clear_cache_for_error(exc: Exception | None) -> bool:
    if exc is None:
        return False
    message = str(exc).lower()
    return any(marker in message for marker in DISCOVERY_FAILURE_MARKERS)


async def _run_bleak(address: str, connect_timeout: float,
                    frame_timeout: float, first_burst_timeout: float,
                    trigger_command: bytes | None,
                    assembler, frame_queue,
                    poll_interval, mqttc, topic_base,
                    last_soc, last_pub_ts):
    """Connect once, subscribe to FF01, then poll on interval inside the connection."""
    indication_event = asyncio.Event()

    def on_indication(_char, data: bytearray):
        indication_event.set()
        LOG.debug("Indication %d bytes", len(data))
        assembler.feed(bytes(data))

    got_first_data = False

    async with BleakClient(address, timeout=connect_timeout) as client:
        LOG.info("Connected to %s", address)

        # Subscribe to FF01 once for the lifetime of this connection.
        LOG.debug("Subscribing to FF01 indications...")
        for sub_attempt in range(3):
            try:
                await client.start_notify(CHAR_FF01_UUID, on_indication)
                LOG.debug("Subscribed to FF01 (attempt %d)", sub_attempt + 1)
                break
            except Exception as sub_exc:
                err_msg = str(sub_exc).lower()
                if ("0x0e" in err_msg or "unlikely" in err_msg) and sub_attempt < 2:
                    LOG.info("start_notify ATT 0x0e; retrying in 1s (attempt %d/3)",
                             sub_attempt + 1)
                    await asyncio.sleep(1)
                else:
                    raise

        # Poll loop — trigger, wait for data, sleep, repeat inside same connection.
        while client.is_connected:
            indication_event.clear()
            trigger_retried = False

            if trigger_command:
                LOG.info("Writing trigger to FF00: %s",
                         trigger_command.hex(' ').upper())
                try:
                    await client.write_gatt_char(
                        CHAR_FF00_UUID, trigger_command, response=True
                    )
                    LOG.debug("FF00 write accepted")
                except Exception as wr_exc:
                    LOG.info("FF00 write failed: %s", wr_exc)
                    break

            # Wait for an indication within first_burst_timeout.
            deadline = time.time() + first_burst_timeout
            got_indication = False
            while client.is_connected:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                indication_event.clear()
                try:
                    await asyncio.wait_for(indication_event.wait(),
                                           timeout=min(remaining, 3.0))
                    got_indication = True
                    await asyncio.sleep(2)  # drain remaining burst packets
                    break
                except asyncio.TimeoutError:
                    if (trigger_command and not trigger_retried
                            and client.is_connected):
                        trigger_retried = True
                        LOG.info("No indication after 3s, re-sending trigger")
                        try:
                            await client.write_gatt_char(
                                CHAR_FF00_UUID, trigger_command, response=True
                            )
                            LOG.debug("FF00 re-trigger accepted")
                        except Exception as rt_exc:
                            LOG.debug("FF00 re-trigger failed: %s", rt_exc)

            if not got_indication:
                LOG.info("No indication received; reconnecting")
                break

            # Drain frame queue and publish.
            while not frame_queue.empty():
                try:
                    soc, _v, rx_ts = frame_queue.get_nowait()
                    publish_state(mqttc, topic_base, soc)
                    publish_availability(mqttc, topic_base, True)
                    last_soc = soc
                    last_pub_ts = rx_ts
                    got_first_data = True
                except asyncio.QueueEmpty:
                    break

            if not got_first_data:
                LOG.info("Indications received but no decodable frame; reconnecting")
                break

            # Stay connected and wait for next poll interval.
            LOG.debug("Next poll in %ds (connection maintained)", poll_interval)
            poll_end = time.time() + poll_interval
            while client.is_connected and time.time() < poll_end:
                await asyncio.sleep(min(1.0, poll_end - time.time()))

        if not client.is_connected:
            LOG.info("BMS disconnected %s",
                     "after data" if got_first_data else "before first data")

    return last_soc, last_pub_ts, got_first_data


async def connect_and_stream(opts, mqttc, topic_base, address,
                             last_soc, last_pub_ts):
    """Connect via bleak, stream indications, publish SOC.

    Uses bleak with start_notify() for indication subscription.
    Retries with bluez cache clear if no data is received.
    """
    frame_timeout = max(float(opts.get("frame_timeout", 180)), 30.0)
    connect_timeout = max(float(opts.get("connect_timeout", 30)), 5.0)
    poll_interval = max(float(opts.get("poll_interval", 300)), 1.0)
    first_burst_timeout = max(float(opts.get("first_burst_timeout", 12)), 3.0)

    # Optional hex command to write to FF00 after subscribing
    trigger_hex = opts.get("trigger_command", "").strip()
    trigger_command = bytes.fromhex(trigger_hex) if trigger_hex else None

    frame_queue: asyncio.Queue = asyncio.Queue()

    def on_frame(frame: bytes):
        soc = decode_soc(frame)
        if soc is None:
            LOG.debug("Ignoring %d-byte payload: not decodable", len(frame))
            return
        voltage = decode_pack_voltage(frame)
        if voltage is not None:
            LOG.debug("Decoded SOC=%d%%, pack=%.2fV", soc, voltage)
        else:
            LOG.debug("Decoded SOC=%d%%", soc)
        frame_queue.put_nowait((soc, voltage, time.time()))

    assembler = FrameAssembler(on_frame=on_frame)

    last_exc = None
    got_any_data = False
    max_attempts = int(opts.get("max_connect_attempts", 20))
    for attempt in range(1, max_attempts + 1):
        try:
            await clear_bluez_cache(address)
            if attempt > 1:
                # Short pause between retries
                await asyncio.sleep(1.0)

            LOG.debug("BLE connect attempt %d/%d to %s",
                      attempt, max_attempts, address)

            last_soc, last_pub_ts, got_data = await _run_bleak(
                address, connect_timeout, frame_timeout,
                first_burst_timeout, trigger_command,
                assembler, frame_queue,
                poll_interval,
                mqttc, topic_base, last_soc, last_pub_ts,
            )
            if got_data:
                last_exc = None
                got_any_data = True
                break
            last_exc = Exception("no indications received")
            LOG.info("Attempt %d: no indications received", attempt)

        except Exception as exc:
            last_exc = exc
            LOG.info("Attempt %d failed: %s", attempt, exc)

    if last_exc is not None and not got_any_data:
        LOG.error("BLE error after %d attempts: %s", max_attempts, last_exc)
    return last_soc, last_pub_ts, got_any_data


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
    failure_streak = 0
    last_soc = None
    last_pub_ts = 0.0

    # Scan once at startup to discover the MAC address
    device_name = opts["device_name"]
    scan_timeout = opts["scan_timeout"]
    device = await find_device(device_name, scan_timeout)
    while device is None:
        LOG.info("Device '%s' not found, retrying in %ds",
                 device_name, retry_delay)
        await asyncio.sleep(retry_delay)
        device = await find_device(device_name, scan_timeout)
    address = device.address

    try:
        while True:
            last_soc, last_pub_ts, got_data = await connect_and_stream(
                opts, mqttc, topic_base, address, last_soc, last_pub_ts,
            )
            if got_data:
                failure_streak = 0
                # Sleep the remaining poll interval (the connection may have polled
                # internally already, so only sleep what's left since last publish).
                poll_interval = max(int(opts.get("poll_interval", 300)), 1)
                elapsed = time.time() - last_pub_ts
                delay = max(1, poll_interval - int(elapsed))
                if delay > 1:
                    LOG.debug("Reconnecting in %ds (last pub %.0fs ago)",
                              delay, elapsed)
            else:
                failure_streak += 1
                delay = min(retry_delay * (2 ** min(failure_streak - 1, 3)), 120)
                LOG.info("Reconnecting in %ds (failure streak=%d)", delay, failure_streak)
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
