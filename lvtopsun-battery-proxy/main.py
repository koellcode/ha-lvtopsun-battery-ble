#!/usr/bin/env python3
"""
LVTOPSUN BLE MITM Proxy for Home Assistant (BlueZ/bless).

Connects to the real BMS via bleak, then advertises as the battery
using bless (BlueZ GATT server). The phone app connects to this proxy
instead of the real battery, and all traffic is relayed and logged.
"""

import asyncio
import json
import logging
import os
import sys
import time

from bleak import BleakClient, BleakScanner
from bless import (
    BlessServer,
    BlessGATTCharacteristic,
    GATTCharacteristicProperties,
    GATTAttributePermissions,
)

LOG = logging.getLogger("proxy")

# ── Protocol constants (lowercase — BlueZ/bless normalise to lowercase) ─────
SVC_UUID    = "0000ff00-0000-1000-8000-00805f9b34fb"
FF00_UUID   = "0000ff00-0000-1000-8000-00805f9b34fb"
FF01_UUID   = "0000ff01-0000-1000-8000-00805f9b34fb"
DEVINFO_SVC = "0000180a-0000-1000-8000-00805f9b34fb"
CHAR_MFR    = "00002a29-0000-1000-8000-00805f9b34fb"
CHAR_MODEL  = "00002a24-0000-1000-8000-00805f9b34fb"
CHAR_SERIAL = "00002a25-0000-1000-8000-00805f9b34fb"
CHAR_HWREV  = "00002a27-0000-1000-8000-00805f9b34fb"
CHAR_FWREV  = "00002a26-0000-1000-8000-00805f9b34fb"
CHAR_SWREV  = "00002a28-0000-1000-8000-00805f9b34fb"


def hexs(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


# ── Options ──────────────────────────────────────────────────────────────────

def load_options():
    path = "/data/options.json"
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {
        "bms_name": os.environ.get("BMS_NAME", "LLM_UNAZAY_0008FR"),
        "proxy_name": os.environ.get("PROXY_NAME", "LLM_UNAZAY_0008FR"),
        "device_id": os.environ.get("DEVICE_ID", "LVTOPSUNAZAY0008FRF48F1U"),
        "scan_timeout": int(os.environ.get("SCAN_TIMEOUT", "15")),
        "connect_timeout": int(os.environ.get("CONNECT_TIMEOUT", "15")),
        "log_level": os.environ.get("LOG_LEVEL", "info"),
    }


# ── BMS client side (bleak) ─────────────────────────────────────────────────

class BMSClient:
    """Maintains connection to the real BMS and subscribes to FF01."""

    def __init__(self, opts):
        self.opts = opts
        self.client = None
        self.connected = False
        self.on_indication = None  # callback(data: bytes)
        self.indication_count = 0
        self._address = None
        self._disconnect_event = None  # set by run_proxy

    def _on_disconnect(self, client):
        LOG.warning("BMS disconnected (addr=%s)", self._address)
        self.connected = False
        if self._disconnect_event:
            self._disconnect_event.set()

    async def _clear_bluez_cache(self, address):
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

    async def connect(self):
        name = self.opts["bms_name"]
        timeout = self.opts["scan_timeout"]
        LOG.info("Scanning for BMS '%s' (%ds)...", name, timeout)
        devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
        target = None
        wanted = name.lower()
        for dev, adv in devices.values():
            dname = (dev.name or adv.local_name or "").lower()
            if wanted in dname or wanted in dev.address.lower():
                target = dev
                LOG.info("Found BMS: %s @ %s RSSI=%s", dev.name or adv.local_name, dev.address, adv.rssi)
                break
        if target is None:
            # Fallback: try "ASR"
            for dev, adv in devices.values():
                dname = (dev.name or adv.local_name or "").lower()
                if dname == "asr":
                    target = dev
                    LOG.info("Found BMS by fallback 'ASR': %s @ %s", dev.name, dev.address)
                    break
        if target is None:
            LOG.warning("BMS not found")
            return False

        self._address = target.address

        # Clear BlueZ cache to avoid stale GATT data
        await self._clear_bluez_cache(target.address)
        await asyncio.sleep(1)

        self.client = BleakClient(
            target.address,
            timeout=self.opts["connect_timeout"],
            disconnected_callback=self._on_disconnect,
        )
        await self.client.connect()
        LOG.info("Connected to BMS @ %s (MTU=%d)", target.address, self.client.mtu_size)

        await self.client.start_notify(FF01_UUID, self._on_notify)
        LOG.info("Subscribed to BMS FF01")
        self.connected = True
        self.indication_count = 0
        return True

    async def reconnect(self):
        """Fast reconnect by known address — skips scan."""
        if not self._address:
            LOG.warning("No known BMS address, falling back to full scan")
            return await self.connect()

        LOG.info("Fast reconnect to %s...", self._address)
        await self._clear_bluez_cache(self._address)
        await asyncio.sleep(1)

        self.client = BleakClient(
            self._address,
            timeout=self.opts["connect_timeout"],
            disconnected_callback=self._on_disconnect,
        )
        await self.client.connect()
        LOG.info("Reconnected to BMS @ %s (MTU=%d)", self._address, self.client.mtu_size)

        await self.client.start_notify(FF01_UUID, self._on_notify)
        LOG.info("Subscribed to BMS FF01")
        self.connected = True
        self.indication_count = 0
        return True

    def _on_notify(self, _sender, data):
        payload = bytes(data)
        self.indication_count += 1
        LOG.info("BMS->proxy %dB (total=%d): %s",
                 len(payload), self.indication_count, hexs(payload[:20]))
        if self.on_indication:
            self.on_indication(payload)

    async def write(self, char_uuid: str, data: bytes):
        if not self.client or not self.client.is_connected:
            LOG.warning("BMS not connected, dropping write")
            return
        try:
            await self.client.write_gatt_char(char_uuid, data, response=False)
            LOG.info("proxy->BMS write %s %dB: %s", char_uuid[-4:], len(data), hexs(data))
        except Exception as e:
            LOG.error("BMS write failed: %s", e)

    async def disconnect(self):
        if self.client:
            try:
                await self.client.stop_notify(FF01_UUID)
            except Exception:
                pass
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.connected = False


# ── GATT server (bless) ─────────────────────────────────────────────────────

async def run_proxy(opts):
    bms = BMSClient(opts)
    proxy_name = opts["proxy_name"]
    device_id = opts["device_id"]
    loop = asyncio.get_running_loop()

    # Build GATT tree
    gatt = {
        # Device Information Service
        DEVINFO_SVC: {
            CHAR_MFR: {
                "Properties": GATTCharacteristicProperties.read,
                "Permissions": GATTAttributePermissions.readable,
                "Value": bytearray(b"LVTOPSUN"),
            },
            CHAR_MODEL: {
                "Properties": GATTCharacteristicProperties.read,
                "Permissions": GATTAttributePermissions.readable,
                "Value": bytearray(b"AZAY0008FR"),
            },
            CHAR_SERIAL: {
                "Properties": GATTCharacteristicProperties.read,
                "Permissions": GATTAttributePermissions.readable,
                "Value": bytearray(device_id.encode("utf-8")),
            },
            CHAR_HWREV: {
                "Properties": GATTCharacteristicProperties.read,
                "Permissions": GATTAttributePermissions.readable,
                "Value": bytearray(b"1.0"),
            },
            CHAR_FWREV: {
                "Properties": GATTCharacteristicProperties.read,
                "Permissions": GATTAttributePermissions.readable,
                "Value": bytearray(b"1.0"),
            },
            CHAR_SWREV: {
                "Properties": GATTCharacteristicProperties.read,
                "Permissions": GATTAttributePermissions.readable,
                "Value": bytearray(b"1.0"),
            },
        },
        # Custom BMS service
        SVC_UUID: {
            FF00_UUID: {
                "Properties": (
                    GATTCharacteristicProperties.write
                    | GATTCharacteristicProperties.write_without_response
                ),
                "Permissions": GATTAttributePermissions.writeable,
                "Value": None,
            },
            FF01_UUID: {
                "Properties": (
                    GATTCharacteristicProperties.read
                    | GATTCharacteristicProperties.write
                    | GATTCharacteristicProperties.indicate
                ),
                "Permissions": (
                    GATTAttributePermissions.readable
                    | GATTAttributePermissions.writeable
                ),
                "Value": None,
            },
        },
    }

    def on_read(char: BlessGATTCharacteristic, **kwargs) -> bytearray:
        LOG.info("App READ %s -> %dB", char.uuid[-4:], len(char.value or b""))
        return char.value or bytearray()

    def on_write(char: BlessGATTCharacteristic, value, **kwargs):
        data = bytes(value)
        char_uuid = str(char.uuid).upper()
        LOG.info("App WRITE %s %dB: %s", char.uuid[-4:], len(data), hexs(data))
        # Forward to BMS
        if bms.connected:
            asyncio.run_coroutine_threadsafe(bms.write(char_uuid, data), loop)

    server = BlessServer(name=proxy_name, loop=loop)
    server.read_request_func = on_read
    server.write_request_func = on_write

    await server.add_gatt(gatt)

    # Relay BMS indications to app
    def on_bms_indication(data: bytes):
        ff01_char = server.get_characteristic(FF01_UUID)
        if ff01_char is None:
            LOG.warning("FF01 characteristic not found in GATT server (uuid=%s)", FF01_UUID)
            return
        ff01_char.value = bytearray(data)
        LOG.info("proxy->App indicate %dB: %s", len(data), hexs(data[:20]) + ("..." if len(data) > 20 else ""))
        server.update_value(SVC_UUID, FF01_UUID)

    bms.on_indication = on_bms_indication

    # Start GATT server first so the adapter is already in peripheral mode
    # before we connect to BMS. Connecting first then starting the server
    # causes BlueZ to drop the BMS connection when switching to dual mode.
    await server.start()
    LOG.info("Proxy advertising as '%s' with device_id='%s'", proxy_name, device_id)

    # Wait for adapter to stabilise in peripheral mode
    await asyncio.sleep(3)

    # Event-driven reconnect: fires immediately on BMS disconnect
    disconnect_event = asyncio.Event()
    bms._disconnect_event = disconnect_event

    async def connect_bms(use_fast=False):
        """Try to connect to BMS with retries."""
        for attempt in range(1, 11):
            try:
                if use_fast and bms._address:
                    ok = await bms.reconnect()
                else:
                    ok = await bms.connect()
                if ok:
                    bms.on_indication = on_bms_indication
                    return True
            except Exception as e:
                LOG.error("BMS connect attempt %d failed: %s", attempt, e)
            delay = min(3 * attempt, 15)
            LOG.info("Retrying BMS connect in %ds...", delay)
            await asyncio.sleep(delay)
            # After 3 fast-reconnect failures, fall back to full scan
            if attempt >= 3:
                use_fast = False
        return False

    # Initial BMS connection (full scan)
    if not await connect_bms(use_fast=False):
        LOG.warning("Initial BMS connect failed — will keep retrying")

    LOG.info("Waiting for app to connect... (Ctrl+C to stop)")

    try:
        while True:
            disconnect_event.clear()
            # Wait for disconnect event or periodic health log (30s)
            try:
                await asyncio.wait_for(disconnect_event.wait(), timeout=30)
                # BMS disconnected — reconnect fast (by address, no scan)
                LOG.info("BMS disconnect detected — fast reconnect in 2s...")
                await bms.disconnect()
                await asyncio.sleep(2)
                if await connect_bms(use_fast=True):
                    LOG.info("BMS reconnected, indications=%d", bms.indication_count)
                else:
                    LOG.warning("BMS reconnect failed — will retry on next cycle")
            except asyncio.TimeoutError:
                # Periodic health log
                if bms.client and bms.client.is_connected:
                    LOG.info("Health: BMS connected, indications=%d", bms.indication_count)
                else:
                    LOG.warning("Health: BMS not connected — attempting reconnect...")
                    await bms.disconnect()
                    await asyncio.sleep(2)
                    if await connect_bms(use_fast=True):
                        LOG.info("BMS reconnected successfully")
    except asyncio.CancelledError:
        pass
    finally:
        LOG.info("Shutting down proxy")
        await server.stop()
        await bms.disconnect()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    opts = load_options()
    level = getattr(logging, opts.get("log_level", "info").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # Suppress noisy dbus_fast errors from bleak/bless sharing the same D-Bus session
    logging.getLogger("dbus_fast.message_bus").setLevel(logging.CRITICAL)
    LOG.info("LVTOPSUN BLE Proxy starting")
    LOG.info("BMS name: %s  Proxy name: %s  Device ID: %s",
             opts["bms_name"], opts["proxy_name"], opts["device_id"])

    asyncio.run(run_proxy(opts))


if __name__ == "__main__":
    main()
