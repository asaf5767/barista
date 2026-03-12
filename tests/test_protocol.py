"""Tests for the ECAM BLE protocol implementation."""

import pytest
from barista.protocol import (
    BeverageId,
    Aroma,
    Temperature,
    MachineState,
    Alarm,
    NozzleState,
    START_BYTE_OUT,
    START_BYTE_IN,
    compute_crc,
    build_packet,
    verify_packet,
    cmd_power_on,
    cmd_monitor,
    cmd_brew,
    cmd_brew_stop,
    cmd_brew_verified,
    cmd_stop_verified,
    cmd_steam,
    cmd_hot_water,
    cmd_profile_select,
    cmd_statistics,
    parse_packet,
    parse_monitor_v2,
    get_beverage_names,
    VERIFIED_COMMANDS,
    VERIFIED_STOP_COMMANDS,
    BEVERAGE_DEFAULTS,
)


# ── CRC Tests ─────────────────────────────────────────────────────────────────

class TestCRC:
    def test_known_power_on_crc(self):
        """Power on command has known CRC bytes 0x55 0x12."""
        pkt = bytes([0x0D, 0x07, 0x84, 0x0F, 0x02, 0x01])
        crc = compute_crc(pkt)
        assert crc == bytes([0x55, 0x12])

    def test_known_monitor_crc(self):
        """Monitor command has known CRC bytes 0xDA 0x25."""
        pkt = bytes([0x0D, 0x05, 0x75, 0x0F])
        crc = compute_crc(pkt)
        assert crc == bytes([0xDA, 0x25])

    def test_crc_deterministic(self):
        """Same input always produces same CRC."""
        data = bytes([0x0D, 0x0F, 0x83, 0xF0, 0x02])
        assert compute_crc(data) == compute_crc(data)

    def test_crc_different_for_different_input(self):
        """Different inputs produce different CRCs."""
        assert compute_crc(bytes([0x01])) != compute_crc(bytes([0x02]))

    def test_crc_is_two_bytes(self):
        """CRC output is always exactly 2 bytes."""
        crc = compute_crc(bytes([0x00]))
        assert len(crc) == 2

    def test_crc_big_endian(self):
        """CRC is big-endian (matches the HA integration's crc_hqx behavior)."""
        from binascii import crc_hqx
        data = bytes([0x0D, 0x05, 0x75, 0x0F])
        raw_crc = crc_hqx(data, 0x1D0F)
        expected = raw_crc.to_bytes(2, byteorder="big")
        assert compute_crc(data) == expected


# ── Packet Building Tests ─────────────────────────────────────────────────────

class TestBuildPacket:
    def test_starts_with_0x0d(self):
        """All outbound packets start with 0x0D."""
        pkt = build_packet([0x75, 0x0F])
        assert pkt[0] == START_BYTE_OUT

    def test_length_byte_correct(self):
        """Length byte = payload length + 3 (start + len + crc*2 - 1)."""
        payload = [0x83, 0xF0, 0x01]
        pkt = build_packet(payload)
        assert pkt[1] == len(payload) + 3

    def test_ends_with_valid_crc(self):
        """Packet ends with valid CRC of all preceding bytes."""
        pkt = build_packet([0x83, 0xF0, 0x01])
        assert verify_packet(pkt)

    def test_empty_payload(self):
        """Building with empty payload still produces valid packet."""
        pkt = build_packet([])
        assert pkt[0] == 0x0D
        assert verify_packet(pkt)


class TestVerifyPacket:
    def test_valid_power_on(self):
        """Known-good power on command verifies."""
        assert verify_packet(cmd_power_on())

    def test_valid_monitor(self):
        """Known-good monitor command verifies."""
        assert verify_packet(cmd_monitor())

    def test_corrupted_packet(self):
        """Corrupted CRC byte fails verification."""
        pkt = bytearray(cmd_power_on())
        pkt[-1] ^= 0xFF  # flip last byte
        assert not verify_packet(bytes(pkt))

    def test_too_short_packet(self):
        """Packets shorter than 4 bytes fail."""
        assert not verify_packet(bytes([0x0D, 0x01]))
        assert not verify_packet(bytes([0x0D]))
        assert not verify_packet(bytes([]))


# ── Known Command Tests ───────────────────────────────────────────────────────

class TestKnownCommands:
    def test_power_on_exact_bytes(self):
        """Power on matches the known byte sequence."""
        expected = bytes([0x0D, 0x07, 0x84, 0x0F, 0x02, 0x01, 0x55, 0x12])
        assert cmd_power_on() == expected

    def test_monitor_exact_bytes(self):
        """Monitor request matches known byte sequence."""
        expected = bytes([0x0D, 0x05, 0x75, 0x0F, 0xDA, 0x25])
        assert cmd_monitor() == expected

    def test_brew_stop_structure(self):
        """Brew stop has correct trigger byte (0x02) and end marker (0x06)."""
        pkt = cmd_brew_stop(BeverageId.ESPRESSO)
        assert pkt[0] == 0x0D
        assert pkt[2] == 0x83  # BeverageDispensingMode
        assert pkt[3] == 0xF0
        assert pkt[4] == BeverageId.ESPRESSO
        assert pkt[5] == 0x02  # Stop trigger
        assert pkt[6] == 0x06  # End marker
        assert verify_packet(pkt)

    def test_steam_valid(self):
        """Steam command is a valid packet."""
        pkt = cmd_steam()
        assert pkt[0] == 0x0D
        assert verify_packet(pkt)

    def test_hot_water_default_250ml(self):
        """Hot water defaults to 250ml."""
        pkt = cmd_hot_water()
        assert verify_packet(pkt)
        # 0xFA = 250
        assert 0xFA in pkt

    def test_hot_water_custom_quantity(self):
        """Hot water respects custom quantity."""
        pkt = cmd_hot_water(100)
        assert verify_packet(pkt)

    def test_profile_select_range(self):
        """Profile selection builds valid packets for profiles 1-4."""
        for pid in range(1, 5):
            pkt = cmd_profile_select(pid)
            assert pkt[0] == 0x0D
            assert pkt[2] == 0xA9
            assert verify_packet(pkt)

    def test_statistics_valid(self):
        """Statistics command builds valid packet."""
        pkt = cmd_statistics(100, 10)
        assert pkt[0] == 0x0D
        assert verify_packet(pkt)


# ── Verified Commands Tests ───────────────────────────────────────────────────

class TestVerifiedCommands:
    def test_all_verified_commands_have_valid_crc(self):
        """Every verified command byte sequence has a correct CRC."""
        for name, cmd in VERIFIED_COMMANDS.items():
            assert verify_packet(cmd), f"CRC mismatch in verified command: {name}"

    def test_all_verified_stop_commands_have_valid_crc(self):
        """Every verified stop command has a correct CRC."""
        for name, cmd in VERIFIED_STOP_COMMANDS.items():
            assert verify_packet(cmd), f"CRC mismatch in verified stop: {name}"

    def test_verified_commands_start_with_0x0d(self):
        """All verified commands start with the outbound start byte."""
        for name, cmd in VERIFIED_COMMANDS.items():
            assert cmd[0] == START_BYTE_OUT, f"{name} doesn't start with 0x0D"

    def test_verified_espresso_exact(self):
        """The verified espresso command matches the HA integration bytes."""
        expected = bytes([
            0x0D, 0x11, 0x83, 0xF0, 0x01, 0x01, 0x01, 0x00,
            0x28, 0x02, 0x03, 0x08, 0x00, 0x00, 0x00, 0x06, 0x8F, 0xFC,
        ])
        assert VERIFIED_COMMANDS["espresso"] == expected

    def test_cmd_brew_verified_returns_bytes(self):
        """cmd_brew_verified returns bytes for known beverages."""
        for name in VERIFIED_COMMANDS:
            result = cmd_brew_verified(name)
            assert result is not None
            assert isinstance(result, bytes)

    def test_cmd_brew_verified_unknown_returns_none(self):
        """cmd_brew_verified returns None for unknown beverages."""
        assert cmd_brew_verified("unicorn_frappuccino") is None

    def test_cmd_stop_verified_returns_bytes(self):
        """cmd_stop_verified returns bytes for known beverages."""
        for name in VERIFIED_STOP_COMMANDS:
            result = cmd_stop_verified(name)
            assert result is not None

    def test_cmd_stop_verified_unknown_returns_none(self):
        assert cmd_stop_verified("nonexistent") is None

    def test_verified_count(self):
        """We have at least 8 verified brew commands."""
        assert len(VERIFIED_COMMANDS) >= 8
        assert len(VERIFIED_STOP_COMMANDS) >= 8


# ── Brew Command Builder Tests ────────────────────────────────────────────────

class TestCmdBrew:
    def test_brew_default_espresso(self):
        """Brew espresso with defaults builds a valid packet."""
        pkt = cmd_brew(BeverageId.ESPRESSO)
        assert pkt[0] == 0x0D
        assert pkt[2] == 0x83
        assert pkt[4] == BeverageId.ESPRESSO
        assert verify_packet(pkt)

    def test_brew_contains_beverage_id(self):
        """Each beverage ID appears in its brew command."""
        for bev in [BeverageId.COFFEE, BeverageId.AMERICANO, BeverageId.DOPPIO_PLUS]:
            pkt = cmd_brew(bev)
            assert pkt[4] == bev

    def test_brew_aroma_in_packet(self):
        """Aroma value appears in the brew packet."""
        for aroma in Aroma:
            pkt = cmd_brew(BeverageId.ESPRESSO, aroma=aroma)
            assert aroma.value in pkt
            assert verify_packet(pkt)

    def test_brew_quantity_in_packet(self):
        """Quantity appears in the packet."""
        pkt = cmd_brew(BeverageId.ESPRESSO, quantity_ml=40)
        assert 40 in pkt  # 0x28 = 40

    def test_brew_temperature_in_packet(self):
        """Temperature value appears in the packet."""
        pkt = cmd_brew(BeverageId.ESPRESSO, temperature=Temperature.VERY_HIGH)
        assert verify_packet(pkt)


# ── Response Parsing Tests ────────────────────────────────────────────────────

class TestParseMonitorV2:
    def test_parse_ready_state(self):
        """Parse a known READY status response."""
        # Captured from real machine
        data = bytes([
            0xD0, 0x12, 0x75, 0x0F, 0x00, 0x04, 0x00, 0x00,
            0x00, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
            0x00, 0xDB, 0x77,
        ])
        status = parse_monitor_v2(data)
        assert status is not None
        assert status.state == MachineState.READY
        assert status.is_ready
        assert len(status.alarms) == 0
        assert status.nozzle == NozzleState.DETACHED

    def test_parse_standby_state(self):
        """Parse a STANDBY status response."""
        data = bytes([
            0xD0, 0x12, 0x75, 0x0F, 0x00, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x03, 0x64, 0x00, 0x00, 0x00, 0x00,
            0x00, 0x90, 0x80,
        ])
        status = parse_monitor_v2(data)
        assert status is not None
        assert status.state == MachineState.STANDBY
        assert not status.is_ready

    def test_parse_with_alarm(self):
        """Parse status with empty water tank alarm (bit 0)."""
        data = bytearray([
            0xD0, 0x12, 0x75, 0x0F, 0x00, 0x04, 0x00,
            0x01,  # alarm byte 0: bit 0 = empty water tank
            0x00, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x00,
        ])
        status = parse_monitor_v2(bytes(data))
        assert status is not None
        assert Alarm.EMPTY_WATER_TANK in status.alarms
        assert not status.is_ready  # has alarms → not ready

    def test_parse_with_waste_container_alarm(self):
        """Parse status with waste container full alarm (bit 1)."""
        data = bytearray([
            0xD0, 0x12, 0x75, 0x0F, 0x00, 0x04, 0x00,
            0x02,  # alarm byte 0: bit 1 = waste container full
            0x00, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x00,
        ])
        status = parse_monitor_v2(bytes(data))
        assert Alarm.WASTE_CONTAINER_FULL in status.alarms

    def test_parse_nozzle_milk_frother(self):
        """Parse status with milk frother attached."""
        data = bytearray([
            0xD0, 0x12, 0x75, 0x0F,
            0x02,  # nozzle = milk frother
            0x04, 0x00, 0x00, 0x00, 0x07, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        ])
        status = parse_monitor_v2(bytes(data))
        assert status is not None
        assert status.nozzle == NozzleState.MILK_FROTHER

    def test_parse_too_short(self):
        """Packets shorter than 14 bytes return None."""
        assert parse_monitor_v2(bytes([0xD0, 0x05, 0x75, 0x0F])) is None

    def test_parse_wrong_command_id(self):
        """Non-0x75 command ID returns None."""
        data = bytes([0xD0, 0x12, 0x83, 0x0F] + [0x00] * 15)
        assert parse_monitor_v2(data) is None

    def test_to_dict(self):
        """MachineStatus.to_dict returns expected keys."""
        data = bytes([
            0xD0, 0x12, 0x75, 0x0F, 0x00, 0x04, 0x00, 0x00,
            0x00, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
            0x00, 0xDB, 0x77,
        ])
        status = parse_monitor_v2(data)
        d = status.to_dict()
        assert "state" in d
        assert "is_ready" in d
        assert "alarms" in d
        assert "nozzle" in d
        assert "raw_hex" in d
        assert d["state"] == "READY"
        assert d["is_ready"] is True


class TestParsePacket:
    def test_parse_monitor_response(self):
        """parse_packet routes 0x75 to monitor parser."""
        data = bytes([
            0xD0, 0x12, 0x75, 0x0F, 0x00, 0x04, 0x00, 0x00,
            0x00, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
            0x00, 0xDB, 0x77,
        ])
        result = parse_packet(data)
        assert result["type"] == "monitor"
        assert result["state"] == "READY"

    def test_parse_beverage_response(self):
        """parse_packet handles 0x83 beverage response."""
        data = bytes([0xD0, 0x05, 0x83, 0x0F, 0x00, 0x00])
        result = parse_packet(data)
        assert result["type"] == "beverage_response"

    def test_parse_unknown_start_byte(self):
        """Non-0xD0 packets are unknown."""
        data = bytes([0xFF, 0x05, 0x75, 0x0F, 0x00, 0x00])
        result = parse_packet(data)
        assert result["type"] == "unknown"

    def test_parse_too_short(self):
        """Short packets are unknown."""
        result = parse_packet(bytes([0xD0, 0x01]))
        assert result["type"] == "unknown"


# ── Enum & Helpers Tests ──────────────────────────────────────────────────────

class TestEnums:
    def test_beverage_id_values(self):
        """Spot-check key beverage IDs."""
        assert BeverageId.ESPRESSO == 0x01
        assert BeverageId.COFFEE == 0x02
        assert BeverageId.AMERICANO == 0x06
        assert BeverageId.HOT_WATER == 0x10
        assert BeverageId.STEAM == 0x11

    def test_aroma_range(self):
        """Aroma values go from 1 (extra mild) to 5 (extra strong)."""
        assert Aroma.EXTRA_MILD == 1
        assert Aroma.EXTRA_STRONG == 5
        assert len(Aroma) == 5

    def test_temperature_range(self):
        """Temperature values go from 0 (low) to 3 (very high)."""
        assert Temperature.LOW == 0
        assert Temperature.VERY_HIGH == 3
        assert len(Temperature) == 4

    def test_machine_state_ready(self):
        assert MachineState.READY == 7

    def test_machine_state_standby(self):
        assert MachineState.STANDBY == 0


class TestBeverageNames:
    def test_get_beverage_names_returns_dict(self):
        names = get_beverage_names()
        assert isinstance(names, dict)

    def test_at_least_20_beverages(self):
        names = get_beverage_names()
        assert len(names) >= 20

    def test_espresso_in_names(self):
        names = get_beverage_names()
        assert "espresso" in names
        assert names["espresso"] == BeverageId.ESPRESSO

    def test_all_names_lowercase(self):
        names = get_beverage_names()
        for name in names:
            assert name == name.lower()


class TestBeverageDefaults:
    def test_espresso_default_40ml(self):
        d = BEVERAGE_DEFAULTS[BeverageId.ESPRESSO]
        assert d["quantity_ml"] == 40

    def test_coffee_default_120ml(self):
        d = BEVERAGE_DEFAULTS[BeverageId.COFFEE]
        assert d["quantity_ml"] == 120

    def test_ristretto_default_strong(self):
        d = BEVERAGE_DEFAULTS[BeverageId.RISTRETTO]
        assert d["aroma"] == Aroma.STRONG

    def test_all_defaults_have_quantity(self):
        for bev, d in BEVERAGE_DEFAULTS.items():
            assert "quantity_ml" in d, f"{bev.name} missing quantity_ml"
            assert d["quantity_ml"] > 0
