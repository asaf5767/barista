"""
D-Bus GATT driver for De'Longhi ECAM coffee machines on Linux.

Uses BlueZ D-Bus API with a ConnectProfile() workaround to force GATT
service resolution. The ECAM firmware doesn't respond to standard ATT
service discovery, but calling ConnectProfile() triggers resolution
as a side effect.

Connection flow:
  1. BlueZ D-Bus Connect() — establishes BLE link
  2. ConnectProfile(service_uuid) — triggers GATT resolution (ignore error)
  3. StartNotify on char000f (0301) — subscribe to indications
  4. WriteValue to char000f (0301) with type=request — send commands
  5. Receive indications via PropertiesChanged D-Bus signals
"""

import asyncio
import logging
from typing import Callable, Optional

from dbus_fast.aio import MessageBus
from dbus_fast import BusType, Variant, MessageType

logger = logging.getLogger("delonghi.dbus_ecam")

BLUEZ = "org.bluez"
DEVICE_IFACE = "org.bluez.Device1"
GATT_CHAR_IFACE = "org.bluez.GattCharacteristic1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

ECAM_SERVICE_UUID = "00035b03-58e6-07dd-021a-08123a000300"
ECAM_CONTROL_UUID = "00035b03-58e6-07dd-021a-08123a000301"


class EcamDBusGATT:
    """D-Bus GATT client for ECAM machines with ConnectProfile workaround."""

    def __init__(self, address: str):
        self.address = address
        self._addr_path = address.replace(":", "_")
        self._dev_path = f"/org/bluez/hci0/dev_{self._addr_path}"
        self._char_path = f"{self._dev_path}/service000e/char000f"
        self._bus: Optional[MessageBus] = None
        self._connected = False
        self._disconnect_cb: Optional[Callable] = None
        self._notify_cb: Optional[Callable] = None
        self._char_iface = None  # Cached GattCharacteristic1 proxy
        self._write_lock = asyncio.Lock()  # Serialize BLE writes

    @property
    def is_connected(self) -> bool:
        return self._connected

    def set_disconnect_callback(self, cb: Callable):
        self._disconnect_cb = cb

    def set_notification_callback(self, cb: Callable):
        self._notify_cb = cb

    async def connect(self) -> bool:
        """Connect to the ECAM machine via D-Bus."""
        try:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception as e:
            logger.error(f"D-Bus connection failed: {e}")
            return False

        # Step 1: Ensure device exists in BlueZ (scan if needed)
        try:
            dev_intro = await self._bus.introspect(BLUEZ, self._dev_path)
        except Exception:
            logger.info("Device not in BlueZ, scanning...")
            try:
                from bleak import BleakScanner
                device = await BleakScanner.find_device_by_address(
                    self.address, timeout=10
                )
                if not device:
                    logger.error("Device not found in scan")
                    return False
                logger.info(f"Found: {device.name}")
                dev_intro = await self._bus.introspect(BLUEZ, self._dev_path)
            except Exception as e:
                logger.error(f"Scan + introspect failed: {e}")
                return False

        dev_obj = self._bus.get_proxy_object(BLUEZ, self._dev_path, dev_intro)
        dev = dev_obj.get_interface(DEVICE_IFACE)
        dev_props = dev_obj.get_interface(PROPS_IFACE)

        # Step 2: Disconnect if lingering, then reconnect clean
        try:
            connected = await dev_props.call_get(DEVICE_IFACE, "Connected")
            if connected.value:
                logger.info("Disconnecting stale BLE link...")
                try:
                    await asyncio.wait_for(dev.call_disconnect(), timeout=5)
                except Exception:
                    pass
                await asyncio.sleep(2)
        except Exception:
            pass

        # Step 3: Connect
        logger.info("Calling Connect()...")
        try:
            await asyncio.wait_for(dev.call_connect(), timeout=15)
        except asyncio.TimeoutError:
            logger.warning("Connect() timed out, checking state...")
        except Exception as e:
            logger.warning(f"Connect(): {e}")

        await asyncio.sleep(2)
        try:
            connected = await dev_props.call_get(DEVICE_IFACE, "Connected")
            if not connected.value:
                logger.error("Device not connected after Connect()")
                return False
        except Exception as e:
            logger.error(f"Could not verify connection: {e}")
            return False

        logger.info("BLE link established")

        # Step 4: Force GATT service resolution via ConnectProfile
        # This is the workaround for ECAM firmware that doesn't respond
        # to standard ATT service discovery.
        resolved = await dev_props.call_get(DEVICE_IFACE, "ServicesResolved")
        if not resolved.value:
            logger.info("Triggering GATT resolution via ConnectProfile...")
            for attempt in range(3):
                try:
                    await asyncio.wait_for(
                        dev.call_connect_profile(ECAM_SERVICE_UUID), timeout=15
                    )
                except Exception as e:
                    logger.debug(f"ConnectProfile attempt {attempt+1}: {e} (expected)")

                # Wait and check — resolution can take up to 15s
                for wait_s in range(15):
                    await asyncio.sleep(1)
                    try:
                        resolved = await dev_props.call_get(DEVICE_IFACE, "ServicesResolved")
                        if resolved.value:
                            logger.info(f"Services resolved after {wait_s+1}s!")
                            break
                    except Exception:
                        pass
                else:
                    # Didn't resolve in this attempt
                    if attempt < 2:
                        logger.warning(f"ConnectProfile attempt {attempt+1} didn't resolve, retrying...")
                        continue
                    logger.error("GATT services never resolved after 3 attempts!")
                    return False
                break  # resolved!

        logger.info("GATT services resolved!")

        # Step 5: Set up notification listener
        try:
            char_intro = await self._bus.introspect(BLUEZ, self._char_path)
            char_obj = self._bus.get_proxy_object(BLUEZ, self._char_path, char_intro)
            char = char_obj.get_interface(GATT_CHAR_IFACE)
            char_props = char_obj.get_interface(PROPS_IFACE)

            # Cache for writes
            self._char_iface = char

            # D-Bus PropertiesChanged signal handler
            def on_props_changed(iface, changed, invalidated):
                if iface == GATT_CHAR_IFACE and "Value" in changed:
                    val = changed["Value"]
                    data = bytes(val.value if isinstance(val, Variant) else val)
                    if data and any(b != 0 for b in data):
                        logger.debug(f"RX indication: {data.hex(' ')}")
                        if self._notify_cb:
                            self._notify_cb(None, bytearray(data))

            char_props.on_properties_changed(on_props_changed)

            # Start indications
            await char.call_start_notify()
            logger.info("StartNotify OK — listening for indications")

            # Wait for GATT to settle after StartNotify
            await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"Notification setup failed: {e}")
            return False

        # Step 6: Watch for disconnection
        def on_dev_changed(iface, changed, invalidated):
            if iface == DEVICE_IFACE and "Connected" in changed:
                val = changed["Connected"]
                conn = val.value if isinstance(val, Variant) else val
                if not conn:
                    logger.warning("Device disconnected (D-Bus signal)")
                    self._connected = False
                    if self._disconnect_cb:
                        self._disconnect_cb(self)

        dev_props.on_properties_changed(on_dev_changed)

        self._connected = True
        logger.info("ECAM D-Bus GATT driver ready!")
        return True

    async def write(self, data: bytes, allow_skip: bool = False) -> bool:
        """Write a command to the ECAM control characteristic.

        Args:
            data: Raw ECAM protocol packet to send.
            allow_skip: If True, use Write Without Response (non-blocking).
                        Used for background monitor polls that shouldn't block
                        the BLE channel. User commands use Write Request to
                        ensure the machine processes them.
        """
        if not self._bus or not self._connected:
            return False

        char = self._char_iface
        if not char:
            try:
                char_intro = await self._bus.introspect(BLUEZ, self._char_path)
                char_obj = self._bus.get_proxy_object(BLUEZ, self._char_path, char_intro)
                char = char_obj.get_interface(GATT_CHAR_IFACE)
                self._char_iface = char
            except Exception as e:
                logger.error(f"Write failed (no char proxy): {e}")
                return False

        # Background polls: skip if busy, use Write Without Response
        if allow_skip:
            if self._write_lock.locked():
                return False
            try:
                async with self._write_lock:
                    await char.call_write_value(
                        bytes(data), {"type": Variant("s", "command")}
                    )
                    logger.debug(f"TX (poll): {data.hex(' ')}")
                    return True
            except Exception as e:
                logger.debug(f"Poll write skipped: {e}")
                return False

        # User commands: use Write Request (blocks until machine ACKs)
        try:
            async with asyncio.timeout(15):
                async with self._write_lock:
                    await char.call_write_value(
                        bytes(data), {"type": Variant("s", "request")}
                    )
                    logger.debug(f"TX: {data.hex(' ')}")
                    return True
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("Write timed out (15s) — command may not have been sent")
            return False
        except Exception as e:
            logger.error(f"Write failed: {e}")
            return False

    async def disconnect(self):
        """Disconnect and clean up."""
        self._connected = False
        if self._bus:
            try:
                # Stop notifications first
                char_intro = await self._bus.introspect(BLUEZ, self._char_path)
                char_obj = self._bus.get_proxy_object(BLUEZ, self._char_path, char_intro)
                char = char_obj.get_interface(GATT_CHAR_IFACE)
                await char.call_stop_notify()
            except Exception:
                pass
            try:
                dev_intro = await self._bus.introspect(BLUEZ, self._dev_path)
                dev_obj = self._bus.get_proxy_object(BLUEZ, self._dev_path, dev_intro)
                dev = dev_obj.get_interface(DEVICE_IFACE)
                await asyncio.wait_for(dev.call_disconnect(), timeout=5)
            except Exception:
                pass
            try:
                self._bus.disconnect()
            except Exception:
                pass
            self._bus = None
        logger.info("Disconnected")
