#!/usr/bin/env python3
"""Minimal LVTOPSUN BLE connection holder using gatttool."""

import asyncio
import json
import logging
import os
import re
import sys


LOG = logging.getLogger("proxy")
DEVICE_LINE_RE = re.compile(
    r"Device\s+([0-9A-F:]{17})\s*(.*)",
    re.IGNORECASE,
)
CONNECT_SUCCESS = "Connection successful"


def load_options():
    path = "/data/options.json"
    if os.path.exists(path):
        with open(path) as handle:
            return json.load(handle)
    return {
        "bms_name": os.environ.get("BMS_NAME", "LLM_UNAZAY_0008FR"),
        "bms_address": os.environ.get("BMS_ADDRESS", ""),
        "scan_timeout": int(os.environ.get("SCAN_TIMEOUT", "15")),
        "connect_timeout": int(os.environ.get("CONNECT_TIMEOUT", "15")),
        "keepalive_interval": int(os.environ.get("KEEPALIVE_INTERVAL", "8")),
        "reconnect_delay": int(os.environ.get("RECONNECT_DELAY", "5")),
        "log_level": os.environ.get("LOG_LEVEL", "info"),
    }


async def run_command(*args: str, timeout: int | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return stdout.decode(errors="replace")


def parse_scan_output(output: str, wanted_name: str):
    fallback_address = None
    wanted = wanted_name.lower()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        match = DEVICE_LINE_RE.search(line)
        if not match:
            continue
        address = match.group(1).upper()
        name = match.group(2).strip()
        name_lower = name.lower()
        if wanted and wanted in name_lower:
            return address, name or address
        if name_lower == "asr" and fallback_address is None:
            fallback_address = (address, name or address)
    return fallback_address


async def query_known_devices(wanted_name: str):
    output = await run_command("bluetoothctl", "devices", timeout=10)
    return parse_scan_output(output, wanted_name)


async def resolve_address(opts, cached_address: str | None):
    configured = (opts.get("bms_address") or "").strip()
    if configured:
        return configured.upper(), "configured address"
    if cached_address:
        return cached_address.upper(), "cached address"

    timeout = max(int(opts.get("scan_timeout", 15)), 5)
    wanted_name = opts.get("bms_name", "")
    known = await query_known_devices(wanted_name)
    if known is not None:
        return known[0], f"known device {known[1]}"

    LOG.info("Scanning for BMS '%s' (%ds) via bluetoothctl...", wanted_name, timeout)
    await run_command(
        "bluetoothctl",
        "--timeout",
        str(timeout),
        "scan",
        "on",
        timeout=timeout + 5,
    )
    result = await query_known_devices(wanted_name)
    if result is None:
        return None, None
    return result


class GatttoolSession:
    def __init__(self, address: str, connect_timeout: int, keepalive_interval: int):
        self.address = address
        self.connect_timeout = connect_timeout
        self.keepalive_interval = keepalive_interval
        self.proc = None
        self._connected = asyncio.Event()
        self._done = asyncio.Event()
        self._reader_task = None

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            "gatttool",
            "-b",
            self.address,
            "-t",
            "public",
            "-I",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._reader_task = asyncio.create_task(self._read_output())
        await self.send("connect")
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=self.connect_timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("gatttool connect timed out") from exc
        LOG.info("Connected to %s via gatttool", self.address)

    async def _read_output(self):
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                self._done.set()
                return
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            LOG.info("gatttool: %s", text)
            lower = text.lower()
            if CONNECT_SUCCESS in text:
                self._connected.set()
            if "error" in lower or "disconnected" in lower or "connect error" in lower:
                self._done.set()

    async def send(self, command: str):
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("gatttool process is not running")
        self.proc.stdin.write((command + "\n").encode())
        await self.proc.stdin.drain()

    async def keepalive(self):
        while not self._done.is_set():
            await asyncio.sleep(self.keepalive_interval)
            if not self._done.is_set():
                LOG.debug("Session idle for %ss, waiting for peer activity", self.keepalive_interval)

    async def wait(self):
        await self._done.wait()

    async def close(self):
        if self.proc is not None and self.proc.stdin is not None:
            try:
                await self.send("disconnect")
            except Exception:
                pass
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        if self._reader_task is not None:
            await self._reader_task


async def run_holder(opts):
    reconnect_delay = max(int(opts.get("reconnect_delay", 5)), 1)
    connect_timeout = max(int(opts.get("connect_timeout", 15)), 5)
    keepalive_interval = max(int(opts.get("keepalive_interval", 8)), 3)
    cached_address = None

    while True:
        address = None
        source = None
        session = None
        try:
            address, source = await resolve_address(opts, cached_address)
            if not address:
                LOG.warning("BMS not found, retrying in %ds", reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                continue

            cached_address = address
            LOG.info("Using BMS address %s (%s)", address, source)

            session = GatttoolSession(address, connect_timeout, keepalive_interval)
            await session.start()
            await session.keepalive()
            LOG.warning("gatttool session ended for %s", address)
        except Exception as exc:
            LOG.warning("gatttool session failed: %s", exc)
        finally:
            if session is not None:
                await session.close()

        await asyncio.sleep(reconnect_delay)


def main():
    opts = load_options()
    level = getattr(logging, opts.get("log_level", "info").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    LOG.info("LVTOPSUN BLE gatttool holder starting")
    LOG.info(
        "Target: %s  Scan: %ss  Connect: %ss  Keepalive: %ss",
        opts.get("bms_name", ""),
        opts.get("scan_timeout", 15),
        opts.get("connect_timeout", 15),
        opts.get("keepalive_interval", 8),
    )
    asyncio.run(run_holder(opts))


if __name__ == "__main__":
    main()