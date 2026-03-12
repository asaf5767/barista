"""Tests for the BLE driver (unit tests with mocked BLE)."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from barista.ble import DelonghiBLE
from barista.protocol import (
    START_BYTE_IN,
    CONTROL_CHARACTERISTIC_UUID,
    cmd_monitor,
)


class TestDelonghiBLEInit:
    def test_initial_state(self):
        """New instance starts disconnected."""
        d = DelonghiBLE()
        assert d.connected is False
        assert d.client is None
        assert d.device is None
        assert d.get_last_status() is None

    def test_status_age_inf_when_no_status(self):
        """Status age is infinity when never polled."""
        d = DelonghiBLE()
        assert d.get_status_age() == float("inf")


class TestNotificationParsing:
    def test_single_complete_packet(self):
        """A complete monitor response is parsed and stored."""
        d = DelonghiBLE()
        captured = []
        d._status_callback = lambda p: captured.append(p)

        # Simulate a complete MonitorV2 response
        packet = bytearray([
            0xD0, 0x12, 0x75, 0x0F, 0x00, 0x04, 0x00, 0x00,
            0x00, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
            0x00, 0xDB, 0x77,
        ])
        d._on_notification(None, packet)

        assert len(captured) == 1
        assert captured[0]["type"] == "monitor"
        assert captured[0]["state"] == "READY"
        assert d.get_last_status() is not None
        assert d.get_last_status()["state"] == "READY"

    def test_chunked_packet(self):
        """Packet arriving in two chunks is reassembled correctly."""
        d = DelonghiBLE()
        captured = []
        d._status_callback = lambda p: captured.append(p)

        full = bytearray([
            0xD0, 0x12, 0x75, 0x0F, 0x00, 0x04, 0x00, 0x00,
            0x00, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
            0x00, 0xDB, 0x77,
        ])

        # Send first 10 bytes, then the rest
        d._on_notification(None, full[:10])
        assert len(captured) == 0  # not complete yet

        d._on_notification(None, full[10:])
        assert len(captured) == 1
        assert captured[0]["type"] == "monitor"

    def test_garbage_before_packet(self):
        """Garbage bytes before start byte are discarded."""
        d = DelonghiBLE()
        captured = []
        d._status_callback = lambda p: captured.append(p)

        garbage = bytearray([0xFF, 0xAA, 0xBB])
        packet = bytearray([
            0xD0, 0x12, 0x75, 0x0F, 0x00, 0x04, 0x00, 0x00,
            0x00, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
            0x00, 0xDB, 0x77,
        ])

        d._on_notification(None, garbage + packet)
        assert len(captured) == 1

    def test_two_consecutive_packets(self):
        """Two packets in one notification are both parsed."""
        d = DelonghiBLE()
        captured = []
        d._status_callback = lambda p: captured.append(p)

        pkt1 = bytearray([
            0xD0, 0x12, 0x75, 0x0F, 0x00, 0x04, 0x00, 0x00,
            0x00, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
            0x00, 0xDB, 0x77,
        ])
        pkt2 = bytearray([
            0xD0, 0x05, 0x83, 0x0F, 0x00, 0x00,
        ])

        d._on_notification(None, pkt1 + pkt2)
        assert len(captured) == 2

    def test_incomplete_packet_buffered(self):
        """Incomplete packet stays in buffer for next notification."""
        d = DelonghiBLE()
        captured = []
        d._status_callback = lambda p: captured.append(p)

        partial = bytearray([0xD0, 0x12, 0x75, 0x0F, 0x00])
        d._on_notification(None, partial)
        assert len(captured) == 0
        assert len(d._buffer) == 5

    def test_raw_callback_fires(self):
        """Raw callback receives the original bytes."""
        d = DelonghiBLE()
        raw_captured = []
        d._raw_callback = lambda p: raw_captured.append(p)

        packet = bytearray([
            0xD0, 0x12, 0x75, 0x0F, 0x00, 0x04, 0x00, 0x00,
            0x00, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
            0x00, 0xDB, 0x77,
        ])
        d._on_notification(None, packet)
        assert len(raw_captured) == 1
        assert raw_captured[0] == bytes(packet)


class TestConnectionListeners:
    def test_connection_listener_called(self):
        """Connection state change notifies registered listeners."""
        d = DelonghiBLE()
        states = []
        d.on_connection_change(lambda s: states.append(s))

        d._notify_connection(True)
        assert states == [True]
        assert d.connected is True

        d._notify_connection(False)
        assert states == [True, False]
        assert d.connected is False

    def test_multiple_listeners(self):
        """Multiple listeners all get called."""
        d = DelonghiBLE()
        a, b = [], []
        d.on_connection_change(lambda s: a.append(s))
        d.on_connection_change(lambda s: b.append(s))

        d._notify_connection(True)
        assert a == [True]
        assert b == [True]


class TestScanStatic:
    @pytest.mark.asyncio
    async def test_scan_returns_list(self):
        """Scan returns a list (even if empty)."""
        with patch("barista.ble.BleakScanner") as mock_scanner:
            mock_scanner.discover = AsyncMock(return_value=[])
            results = await DelonghiBLE.scan(timeout=1.0)
            assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_scan_formats_results(self):
        """Scan formats discovered devices correctly."""
        mock_device = MagicMock()
        mock_device.name = "D1533270"
        mock_device.address = "00:A0:50:2A:D2:8F"
        mock_device.rssi = -45

        with patch("barista.ble.BleakScanner") as mock_scanner:
            mock_scanner.discover = AsyncMock(return_value=[mock_device])
            results = await DelonghiBLE.scan(timeout=1.0)
            assert len(results) == 1
            assert results[0]["name"] == "D1533270"
            assert results[0]["address"] == "00:A0:50:2A:D2:8F"
            assert results[0]["rssi"] == -45


class TestSendWithoutConnection:
    @pytest.mark.asyncio
    async def test_send_fails_when_disconnected(self):
        """Send returns False when not connected and no auto-reconnect address."""
        d = DelonghiBLE()
        d._auto_reconnect = False
        result = await d.send(cmd_monitor())
        assert result is False
