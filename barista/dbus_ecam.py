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

        # Step 1: Ensure device is connected
        try:
            dev_intro = await self._bus.introspect(BLUEZ, self._dev_path)
            dev_obj = self._bus.get_proxy_object(BLUEZ, self._dev_path, dev_intro)
            dev = dev_obj.get_interface(DEVICE_IFACE)
            dev_props = dev_obj.get_interface(PROPS_IFACE)

            connected = await dev_props.call_get(DEVICE_IFACE, "Connected")
            if not connected.value:
                logger.info("Calling Connect()...")
                try:
                    await asyncio.wait_for(dev.call_connect(), timeout=15)
                except asyncio.TimeoutError:
                    logger.warning("Connect() timed out, checking state...")
                except Exception as e:
                    logger.warning(f"Connect(): {e}")

                await asyncio.sleep(2)
                connected = await dev_props.call_get(DEVICE_IFACE, "Connected")
                if not connected.value:
                    logger.error("Device not connected after Connect()")
                    return False

            logger.info("BLE link established")
        except Exception as e:
            logger.error(f"Device not found in BlueZ: {e}")
            logger.info("Scanning for device first...")
            # Need to scan first so BlueZ knows about the device
            try:
                from bleak import BleakScanner
                device = await BleakScanner.find_device_by_address(
                    self.address, timeout=10
                )
                if not device:
                    logger.error("Device not found in scan")
                    return False
                logger.info(f"Found: {device.name}")

                # Now try Connect() again
                dev_intro = await self._bus.introspect(BLUEZ, self._dev_path)
                dev_obj = self._bus.get_proxy_object(BLUEZ, self._dev_path, dev_intro)
                dev = dev_obj.get_interface(DEVICE_IFACE)
                dev_props = dev_obj.get_interface(PROPS_IFACE)

                try:
                    await asyncio.wait_for(dev.call_connect(), timeout=15)
                except Exception:
                    pass
                await asyncio.sleep(2)

                connected = await dev_props.call_get(DEVICE_IFACE, "Connected")
                if not connected.value:
                    logger.error("Still not connected")
                    return False
                logger.info("Connected after scan")
            except Exception as e2:
                logger.error(f"Scan + connect failed: {e2}")
                return False

        # Step 2: Force GATT service resolution via ConnectProfile
        resolved = await dev_props.call_get(DEVICE_IFACE, "ServicesResolved")
        if not resolved.value:
            logger.info("Triggering GATT resolution via ConnectProfile...")
            try:
                await asyncio.wait_for(
                    dev.call_connect_profile(ECAM_SERVICE_UUID), timeout=15
                )
            except Exception as e:
                logger.debug(f"ConnectProfile result: {e} (expected)")

            # Check resolution
            await asyncio.sleep(1)
            resolved = await dev_props.call_get(DEVICE_IFACE, "ServicesResolved")
            if not resolved.value:
                # Try again
                logger.warning("Services not resolved, retrying ConnectProfile...")
                try:
                    await asyncio.wait_for(
                        dev.call_connect_profile(ECAM_SERVICE_UUID), timeout=10
                    )
                except Exception:
                    pass
                await asyncio.sleep(1)
                resolved = await dev_props.call_get(DEVICE_IFACE, "ServicesResolved")

            if not resolved.value:
                logger.error("GATT services never resolved!")
                return False

        logger.info("GATT services resolved!")

        # Step 3: Set up notification listener
        try:
            char_intro = await self._bus.introspect(BLUEZ, self._char_path)
            char_obj = self._bus.get_proxy_object(BLUEZ, self._char_path, char_intro)
            char = char_obj.get_interface(GATT_CHAR_IFACE)
            char_props = char_obj.get_interface(PROPS_IFACE)

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

        except Exception as e:
            logger.error(f"Notification setup failed: {e}")
            return False

        # Step 4: Watch for disconnection
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

    async def write(self, data: bytes) -> bool:
        """Write a command to the ECAM control characteristic."""
        if not self._bus or not self._connected:
            return False

        try:
            char_intro = await self._bus.introspect(BLUEZ, self._char_path)
            char_obj = self._bus.get_proxy_object(BLUEZ, self._char_path, char_intro)
            char = char_obj.get_interface(GATT_CHAR_IFACE)

            await char.call_write_value(
                bytes(data), {"type": Variant("s", "request")}
            )
            logger.debug(f"TX: {data.hex(' ')}")
            return True
        except Exception as e:
            logger.error(f"Write failed: {e}")
            return False

    async def disconnect(self):
        """Disconnect and clean up."""
        self._connected = False
        if self._bus:
            try:
                dev_intro = await self._bus.introspect(BLUEZ, self._dev_path)
                dev_obj = self._bus.get_proxy_object(BLUEZ, self._dev_path, dev_intro)
                dev = dev_obj.get_interface(DEVICE_IFACE)
                await dev.call_disconnect()
            except Exception:
                pass
            try:
                self._bus.disconnect()
            except Exception:
                pass
            self._bus = None
        logger.info("Disconnected")
