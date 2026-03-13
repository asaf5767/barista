"""
De'Longhi ECAM Bluetooth Low Energy Driver
Handles scanning, connecting, sending commands, and receiving notifications.
Features auto-reconnect for persistent operation.
"""

import asyncio
import logging
import time
from typing import Callable, Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from barista.protocol import (
    SERVICE_UUID,
    CONTROL_CHARACTERISTIC_UUID,
    START_BYTE_IN,
    cmd_monitor,
    parse_packet,
    verify_packet,
)

logger = logging.getLogger("delonghi.ble")


class DelonghiBLE:
    """BLE driver for De'Longhi ECAM machines with auto-reconnect."""

    def __init__(self):
        self.client: Optional[BleakClient] = None
        self.device: Optional[BLEDevice] = None
        self.connected = False
        self._address: Optional[str] = None
        self._buffer = bytearray()
        self._status_callback: Optional[Callable] = None
        self._raw_callback: Optional[Callable] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._last_status: Optional[dict] = None
        self._last_status_time: float = 0
        self._auto_reconnect = True
        self._reconnecting = False
        self._connection_listeners: list[Callable] = []

    # ── Events ────────────────────────────────────────────────────────────────

    def on_connection_change(self, callback: Callable):
        """Register a callback for connection state changes."""
        self._connection_listeners.append(callback)

    def _notify_connection(self, connected: bool):
        self.connected = connected
        for cb in self._connection_listeners:
            try:
                cb(connected)
            except Exception:
                pass

    # ── Scanning ──────────────────────────────────────────────────────────────

    @staticmethod
    async def scan(timeout: float = 10.0) -> list[dict]:
        """Scan for De'Longhi coffee machines."""
        logger.info(f"Scanning for BLE devices ({timeout}s)...")
        devices = await BleakScanner.discover(
            timeout=timeout,
            service_uuids=[SERVICE_UUID],
        )

        results = []
        for d in devices:
            results.append({
                "name": d.name or "Unknown",
                "address": d.address,
                "rssi": d.rssi if hasattr(d, "rssi") else None,
            })
            logger.info(f"  Found: {d.name} ({d.address})")

        if not results:
            logger.info("No devices found with service filter. Trying broad scan...")
            all_devices = await BleakScanner.discover(timeout=timeout)
            for d in all_devices:
                name = (d.name or "").lower()
                if any(kw in name for kw in ["delonghi", "ecam", "dlwifi", "dinamica", "d15"]):
                    results.append({
                        "name": d.name,
                        "address": d.address,
                        "rssi": d.rssi if hasattr(d, "rssi") else None,
                    })
                    logger.info(f"  Found (broad): {d.name} ({d.address})")

        return results

    # ── Connection ────────────────────────────────────────────────────────────

    def _on_disconnect(self, client: BleakClient):
        """Called when the BLE connection drops."""
        logger.warning("BLE connection lost!")
        self._notify_connection(False)
        if self._auto_reconnect and self._address:
            logger.info("Will auto-reconnect...")
            # Schedule reconnect (can't await in callback)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._reconnect_loop())
            except RuntimeError:
                pass

    async def _reconnect_loop(self):
        """Keep trying to reconnect with exponential backoff."""
        if self._reconnecting:
            return
        self._reconnecting = True
        delay = 2
        max_delay = 30

        while self._auto_reconnect and not self.connected:
            logger.info(f"Reconnecting in {delay}s...")
            await asyncio.sleep(delay)
            try:
                ok = await self._do_connect(self._address)
                if ok:
                    logger.info("Reconnected successfully!")
                    self._reconnecting = False
                    return
            except Exception as e:
                logger.warning(f"Reconnect attempt failed: {e}")
            delay = min(delay * 1.5, max_delay)

        self._reconnecting = False

    async def _do_connect(self, address: str) -> bool:
        """Internal connect logic."""
        try:
            self.device = await BleakScanner.find_device_by_address(address, timeout=15.0)
            if not self.device:
                logger.error(f"Device {address} not found")
                return False

            self.client = BleakClient(
                self.device,
                timeout=15.0,
                disconnected_callback=self._on_disconnect,
            )
            await self.client.connect()

            if not self.client.is_connected:
                logger.error("Failed to establish connection")
                return False

            logger.info(f"Connected to {self.device.name} ({address})")

            await self.client.start_notify(
                CONTROL_CHARACTERISTIC_UUID,
                self._on_notification,
            )
            logger.info("Subscribed to notifications")

            self._notify_connection(True)
            return True

        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self._notify_connection(False)
            return False

    async def connect(self, address: str) -> bool:
        """Connect to a De'Longhi machine by MAC address."""
        logger.info(f"Connecting to {address}...")
        self._address = address
        return await self._do_connect(address)

    async def disconnect(self):
        """Disconnect from the machine."""
        self._auto_reconnect = False

        if self._monitor_task:
            self._monitor_task.cancel()
            self._monitor_task = None

        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None

        if self.client and self.client.is_connected:
            try:
                await self.client.stop_notify(CONTROL_CHARACTERISTIC_UUID)
            except Exception:
                pass
            await self.client.disconnect()

        self._notify_connection(False)
        logger.info("Disconnected")

    # ── Sending Commands ──────────────────────────────────────────────────────

    async def send(self, data: bytes) -> bool:
        """Send a raw command packet to the machine. Auto-reconnects if needed."""
        if not self.client or not self.client.is_connected:
            if self._address and self._auto_reconnect:
                logger.info("Not connected, attempting reconnect before send...")
                ok = await self._do_connect(self._address)
                if not ok:
                    return False
            else:
                logger.error("Not connected")
                return False

        try:
            logger.debug(f"TX: {data.hex(' ')}")
            await self.client.write_gatt_char(
                CONTROL_CHARACTERISTIC_UUID,
                data,
                response=True,
            )
            return True
        except Exception as e:
            logger.error(f"Send failed: {e}")
            self._notify_connection(False)
            return False

    async def request_status(self) -> Optional[dict]:
        """Send a monitor request and wait briefly for the response."""
        return await self.send_and_wait(cmd_monitor(), "monitor")

    async def send_and_wait(self, command: bytes, response_type: str,
                            timeout: float = 5.0) -> Optional[dict]:
        """Send a command and wait for a specific response type.
        Returns the parsed response dict, or None on timeout.
        """
        event = asyncio.Event()
        result = {}

        def capture(parsed):
            if parsed.get("type") == response_type:
                result.update(parsed)
                event.set()
            # Also keep monitoring data flowing
            if parsed.get("type") == "monitor":
                self._last_status = parsed
                self._last_status_time = time.time()

        old_cb = self._status_callback
        self._status_callback = capture

        sent = await self.send(command)
        if not sent:
            self._status_callback = old_cb
            if response_type == "monitor":
                return self._last_status
            return None

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"Request for '{response_type}' timed out")

        self._status_callback = old_cb
        if result:
            if response_type == "monitor":
                self._last_status = result
                self._last_status_time = time.time()
        return result if result else None

    # ── Background Monitoring ─────────────────────────────────────────────────

    async def start_monitoring(self, interval: float = 5.0, callback: Optional[Callable] = None):
        """Start periodic status polling."""
        if callback:
            self._status_callback = callback

        async def _loop():
            while True:
                if self.connected:
                    try:
                        await self.send(cmd_monitor())
                    except Exception as e:
                        logger.error(f"Monitor poll error: {e}")
                await asyncio.sleep(interval)

        self._monitor_task = asyncio.create_task(_loop())

    def get_last_status(self) -> Optional[dict]:
        """Get the last received status without sending a new request."""
        return self._last_status

    def get_status_age(self) -> float:
        """Seconds since last status update."""
        if self._last_status_time == 0:
            return float('inf')
        return time.time() - self._last_status_time

    # ── Notification Handler ──────────────────────────────────────────────────

    def _on_notification(self, sender, data: bytearray):
        """Handle incoming BLE notifications (data chunks)."""
        self._buffer.extend(data)

        while len(self._buffer) >= 4:
            start_idx = -1
            for i in range(len(self._buffer)):
                if self._buffer[i] == START_BYTE_IN:
                    start_idx = i
                    break

            if start_idx == -1:
                self._buffer.clear()
                return

            if start_idx > 0:
                self._buffer = self._buffer[start_idx:]

            if len(self._buffer) < 2:
                return

            expected_len = self._buffer[1] + 1
            if expected_len < 4 or expected_len > 256:
                self._buffer = self._buffer[1:]
                continue

            if len(self._buffer) < expected_len:
                return

            packet = bytes(self._buffer[:expected_len])
            self._buffer = self._buffer[expected_len:]

            logger.debug(f"RX: {packet.hex(' ')}")

            parsed = parse_packet(packet)
            if parsed.get("type") == "monitor":
                self._last_status = parsed
                self._last_status_time = time.time()

            if self._status_callback:
                self._status_callback(parsed)

            if self._raw_callback:
                self._raw_callback(packet)
