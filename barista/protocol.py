"""
De'Longhi ECAM Bluetooth Protocol Implementation
Reverse-engineered from:
  - Arbuzov/home_assistant_delonghi_primadonna (Python/HA)
  - manekinekko/cafy (TypeScript)
"""

from binascii import crc_hqx
from enum import IntEnum
from typing import Optional
import logging

logger = logging.getLogger("delonghi.protocol")


# ── BLE Constants ──────────────────────────────────────────────────────────────

SERVICE_UUID = "00035b03-58e6-07dd-021a-08123a000300"
CONTROL_CHARACTERISTIC_UUID = "00035b03-58e6-07dd-021a-08123a000301"
NAME_CHARACTERISTIC_UUID = "00002a00-0000-1000-8000-00805f9b34fb"

START_BYTE_OUT = 0x0D   # Outbound (to machine)
START_BYTE_IN  = 0xD0   # Inbound (from machine)


# ── Enums ──────────────────────────────────────────────────────────────────────

class BeverageId(IntEnum):
    ESPRESSO           = 0x01
    COFFEE             = 0x02
    LONG_COFFEE        = 0x03
    ESPRESSO_2X        = 0x04
    DOPPIO_PLUS        = 0x05
    AMERICANO          = 0x06
    CAPPUCCINO         = 0x07
    LATTE_MACCHIATO    = 0x08
    CAFFE_LATTE        = 0x09
    FLAT_WHITE         = 0x0A
    ESPRESSO_MACCHIATO = 0x0B
    HOT_MILK           = 0x0C
    CAPPUCCINO_DOPPIO  = 0x0D
    COLD_MILK          = 0x0E
    CAPPUCCINO_MIX     = 0x0F   # CappuccinoReverse — milk-first cappuccino
    HOT_WATER          = 0x10
    STEAM              = 0x11
    RISTRETTO          = 0x13
    LONG_ESPRESSO      = 0x14
    TEA                = 0x16
    COFFEE_POT         = 0x17
    CORTADO            = 0x18
    LONG_BLACK         = 0x19
    TRAVEL_MUG         = 0x1A
    BREW_OVER_ICE      = 0x1B


class Ingredient(IntEnum):
    """ECAM ingredient IDs. Used in recipe read/write commands."""
    TEMP        = 0x00   # Narrow (1 byte)
    COFFEE      = 0x01   # Wide (2 bytes) — coffee quantity in ml
    TASTE       = 0x02   # Narrow — aroma/strength level
    DUE_X_PER   = 0x08   # Narrow
    MILK        = 0x09   # Wide (2 bytes) — milk duration in seconds
    INVERSION   = 0x0C   # Narrow — milk-first flag
    HOT_WATER   = 0x0F   # Wide (2 bytes) — hot water quantity in ml
    PROGRAMMABLE = 0x18  # Narrow
    VISIBLE     = 0x19   # Narrow
    INDEX_LENGTH = 0x1B  # Narrow
    ACCESSORIO  = 0x1C   # Narrow


# Ingredients that use 2-byte (wide) encoding; all others use 1-byte (narrow)
WIDE_INGREDIENTS = frozenset({
    Ingredient.COFFEE,
    Ingredient.MILK,
    Ingredient.HOT_WATER,
})

# Ingredients that are actual brewing parameters (sent in brew commands).
# Metadata fields (VISIBLE, INDEX_LENGTH, PROGRAMMABLE, ACCESSORIO) are
# profile metadata returned by RecipeQuantityRead but NOT sent to brew.
BREW_INGREDIENTS = frozenset({
    Ingredient.TEMP,
    Ingredient.COFFEE,
    Ingredient.TASTE,
    Ingredient.DUE_X_PER,
    Ingredient.MILK,
    Ingredient.INVERSION,
    Ingredient.HOT_WATER,
})

# Human-readable names for ingredients
INGREDIENT_NAMES = {
    Ingredient.TEMP:         "temperature",
    Ingredient.COFFEE:       "coffee",
    Ingredient.TASTE:        "taste",
    Ingredient.MILK:         "milk",
    Ingredient.INVERSION:    "inversion",
    Ingredient.HOT_WATER:    "hot_water",
    Ingredient.DUE_X_PER:    "due_x_per",
    Ingredient.PROGRAMMABLE: "programmable",
    Ingredient.VISIBLE:      "visible",
    Ingredient.INDEX_LENGTH: "index_length",
    Ingredient.ACCESSORIO:   "accessorio",
}


class TasteType(IntEnum):
    """EcamBeverageTasteType — the final byte of a brew command."""
    DELETE                   = 0x00
    SAVE                     = 0x01
    PREPARE                  = 0x02
    PREPARE_AND_SAVE         = 0x03
    SAVE_INVERSION           = 0x05
    PREPARE_INVERSION        = 0x06
    PREPARE_AND_SAVE_INVERSION = 0x07


class Aroma(IntEnum):
    EXTRA_MILD  = 0x01
    MILD        = 0x02
    NORMAL      = 0x03
    STRONG      = 0x04
    EXTRA_STRONG = 0x05


class Temperature(IntEnum):
    LOW       = 0x00
    MID       = 0x01
    HIGH      = 0x02
    VERY_HIGH = 0x03


class MachineState(IntEnum):
    STANDBY            = 0
    TURNING_ON         = 1
    SHUTTING_DOWN      = 2
    DESCALING          = 4
    STEAM_PREPARATION  = 5
    RECOVERY           = 6
    READY              = 7
    RINSING            = 8
    MILK_PREPARATION   = 10
    HOT_WATER_DELIVERY = 11
    MILK_CLEANING      = 12
    CHOCOLATE_PREP     = 16


class Alarm(IntEnum):
    EMPTY_WATER_TANK     = 0
    WASTE_CONTAINER_FULL = 1
    DESCALE_ALARM        = 2
    REPLACE_WATER_FILTER = 3
    COFFEE_TOO_FINE      = 4
    BEANS_EMPTY          = 5
    MACHINE_TO_SERVICE   = 6
    HEATER_PROBE_FAIL    = 7
    TOO_MUCH_COFFEE      = 8
    INFUSER_MOTOR_FAIL   = 9
    STEAMER_PROBE_FAIL   = 10
    EMPTY_DRIP_TRAY      = 11
    HYDRAULIC_PROBLEM    = 12
    TANK_IN_POSITION     = 13
    BEANS_EMPTY_2        = 15
    TANK_TOO_FULL        = 16
    BEAN_HOPPER_ABSENT   = 17


class NozzleState(IntEnum):
    UNKNOWN          = -1
    DETACHED         = 0
    STEAM            = 1
    MILK_FROTHER     = 2
    MILK_CLEAN       = 3


_NOZZLE_VALUES = frozenset(e.value for e in NozzleState)


# ── Packet Building ───────────────────────────────────────────────────────────

def compute_crc(data: bytes) -> bytes:
    """CRC-16/CCITT with init=0x1D0F, big-endian output."""
    crc = crc_hqx(data, 0x1D0F)
    return crc.to_bytes(2, byteorder="big")


def build_packet(payload: list[int]) -> bytes:
    """Wrap payload with header byte, length, and CRC."""
    pkt = [START_BYTE_OUT, len(payload) + 3] + payload
    crc = compute_crc(bytes(pkt))
    pkt.append(crc[0])
    pkt.append(crc[1])
    return bytes(pkt)


def verify_packet(data: bytes) -> bool:
    """Verify CRC of an incoming packet."""
    if len(data) < 4:
        return False
    body = data[:-2]
    expected = compute_crc(body)
    return data[-2:] == expected


# ── Ingredient Encoding ──────────────────────────────────────────────────────

def encode_ingredient(ingredient_id: int, value: int) -> list[int]:
    """Encode a single ingredient for a brew command.
    Wide ingredients (Coffee, Milk, HotWater) use 2-byte big-endian values.
    All others use 1-byte values.
    """
    result = [ingredient_id & 0xFF]
    if ingredient_id in WIDE_INGREDIENTS:
        result.append((value >> 8) & 0xFF)
        result.append(value & 0xFF)
    else:
        result.append(value & 0xFF)
    return result


def decode_ingredient(data: bytes, offset: int) -> tuple[int, int, int]:
    """Decode a single ingredient from a response.
    Returns: (ingredient_id, value, bytes_consumed)
    """
    if offset >= len(data):
        raise ValueError(f"No data at offset {offset}")

    ingredient_id = data[offset]
    if ingredient_id in WIDE_INGREDIENTS:
        if offset + 2 >= len(data):
            raise ValueError(f"Incomplete wide ingredient at offset {offset}")
        value = (data[offset + 1] << 8) | data[offset + 2]
        return ingredient_id, value, 3
    else:
        if offset + 1 >= len(data):
            raise ValueError(f"Incomplete narrow ingredient at offset {offset}")
        value = data[offset + 1]
        return ingredient_id, value, 2


def encode_recipe(recipe: list[tuple[int, int]]) -> list[int]:
    """Encode a list of (ingredient_id, value) pairs for a brew command."""
    result = []
    for ing_id, value in recipe:
        result.extend(encode_ingredient(ing_id, value))
    return result


def decode_recipe(data: bytes) -> list[tuple[int, int]]:
    """Decode a variable-length ingredient list from response bytes.
    Returns list of (ingredient_id, value) tuples.
    """
    ingredients = []
    offset = 0
    while offset < len(data):
        try:
            ing_id, value, consumed = decode_ingredient(data, offset)
            ingredients.append((ing_id, value))
            offset += consumed
        except ValueError as e:
            logger.warning(f"Recipe decode error at offset {offset}: {e}")
            break
    return ingredients


def recipe_to_dict(ingredients: list[tuple[int, int]]) -> dict:
    """Convert ingredient list to a human-readable dict."""
    result = {}
    for ing_id, value in ingredients:
        name = INGREDIENT_NAMES.get(ing_id, f"unknown_0x{ing_id:02x}")
        result[name] = value
    return result


def recipe_from_dict(recipe_dict: dict) -> list[tuple[int, int]]:
    """Convert a human-readable dict back to ingredient list."""
    # Reverse lookup: name -> Ingredient ID
    name_to_id = {v: k for k, v in INGREDIENT_NAMES.items()}
    ingredients = []
    for name, value in recipe_dict.items():
        if name in name_to_id:
            ingredients.append((int(name_to_id[name]), value))
        else:
            logger.warning(f"Unknown ingredient name: {name}")
    return ingredients


# ── Command Builders ──────────────────────────────────────────────────────────

def cmd_power_on() -> bytes:
    """Turn the machine on."""
    return build_packet([0x84, 0x0F, 0x02, 0x01])


def cmd_monitor() -> bytes:
    """Request machine status (MonitorV2)."""
    return build_packet([0x75, 0x0F])


def cmd_recipe_read(profile: int, beverage: BeverageId) -> bytes:
    """Request saved recipe for a beverage from a machine profile.
    Command ID: 0xA6 (RecipeQuantityRead)
    """
    return build_packet([0xA6, 0xF0, profile & 0xFF, beverage & 0xFF])


def cmd_brew_recipe(beverage: BeverageId, recipe: list[tuple[int, int]]) -> bytes:
    """Brew a beverage using a full recipe (ingredient list from the machine).
    Filters out profile metadata (visible, index_length, etc.) and sends
    only actual brewing parameters.

    Packet: [0x83] [0xF0] [bev_id] [0x01=Start] [...ingredients] [taste_type]
    """
    # Filter to only brewing ingredients — skip metadata fields
    brew_recipe = [(ing_id, val) for ing_id, val in recipe if ing_id in BREW_INGREDIENTS]

    has_inversion = any(ing_id == Ingredient.INVERSION for ing_id, _ in brew_recipe)
    taste_type = TasteType.PREPARE_INVERSION if has_inversion else TasteType.PREPARE

    payload = [
        0x83, 0xF0,
        beverage & 0xFF,
        0x01,                       # Trigger: Start
    ]
    payload.extend(encode_recipe(brew_recipe))
    payload.append(taste_type & 0xFF)
    return build_packet(payload)


def cmd_brew(
    beverage: BeverageId,
    quantity_ml: int = 100,
    aroma: Aroma = Aroma.NORMAL,
    temperature: Temperature = Temperature.HIGH,
    milk_seconds: int = 0,
) -> bytes:
    """Build a beverage command from explicit parameters (legacy/fallback).
    Prefer cmd_brew_recipe() when a saved recipe is available.
    """
    recipe = [
        (Ingredient.COFFEE, quantity_ml),
        (Ingredient.TASTE, aroma),
        (Ingredient.TEMP, temperature),
    ]
    if milk_seconds > 0:
        recipe.append((Ingredient.MILK, milk_seconds))
    return cmd_brew_recipe(beverage, recipe)


def cmd_brew_stop(beverage: BeverageId) -> bytes:
    """Stop a beverage that is currently dispensing."""
    payload = [
        0x83, 0xF0,
        beverage & 0xFF,
        0x02,  # Trigger: Stop
        TasteType.PREPARE_INVERSION & 0xFF,
    ]
    return build_packet(payload)


def cmd_profile_select(profile_id: int) -> bytes:
    """Select a user profile (1-4)."""
    return build_packet([0xA9, 0xF0, profile_id & 0xFF])


def cmd_steam() -> bytes:
    """Start steam. Uses the verified command."""
    return VERIFIED_COMMANDS["steam"]


def cmd_hot_water(quantity_ml: int = 250) -> bytes:
    """Dispense hot water."""
    qty_hi = (quantity_ml >> 8) & 0xFF
    qty_lo = quantity_ml & 0xFF
    return build_packet([0x83, 0xF0, 0x10, 0x01, 0x0F, qty_hi, qty_lo, 0x1C, 0x01, 0x06])


def cmd_statistics(start_index: int = 100, count: int = 10) -> bytes:
    """Request statistics (beverage counts, etc.)."""
    return build_packet([
        0xA2, 0x0F,
        (start_index >> 8) & 0xFF,
        start_index & 0xFF,
        count & 0xFF,
    ])


# ── Verified Raw Commands from Home Assistant Integration ─────────────────────
# These are captured from real machines and known to work.

VERIFIED_COMMANDS = {
    "espresso":    bytes([0x0d, 0x11, 0x83, 0xf0, 0x01, 0x01, 0x01, 0x00,
                          0x28, 0x02, 0x03, 0x08, 0x00, 0x00, 0x00, 0x06, 0x8f, 0xfc]),
    "coffee":      bytes([0x0d, 0x0f, 0x83, 0xf0, 0x02, 0x01, 0x01,
                          0x00, 0x67, 0x02, 0x02, 0x00, 0x00, 0x06, 0x77, 0xff]),
    "long_coffee": bytes([0x0d, 0x0f, 0x83, 0xf0, 0x03, 0x01, 0x01,
                          0x00, 0xa0, 0x02, 0x03, 0x00, 0x00, 0x06, 0x18, 0x7f]),
    "espresso_2x": bytes([0x0d, 0x0f, 0x83, 0xf0, 0x04, 0x01, 0x01,
                          0x00, 0x28, 0x02, 0x02, 0x00, 0x00, 0x06, 0xab, 0x53]),
    "doppio_plus": bytes([0x0d, 0x0d, 0x83, 0xf0, 0x05, 0x01, 0x01,
                          0x00, 0x78, 0x00, 0x00, 0x06, 0xc4, 0x7e]),
    "americano":   bytes([0x0d, 0x12, 0x83, 0xf0, 0x06, 0x01, 0x01, 0x00,
                          0x28, 0x02, 0x03, 0x0f, 0x00, 0x6e, 0x00, 0x00,
                          0x06, 0x47, 0x8b]),
    "hot_water":   bytes([0x0d, 0x0d, 0x83, 0xf0, 0x10, 0x01,
                          0x0f, 0x00, 0xfa, 0x1c, 0x01, 0x06, 0x04, 0xb4]),
    "steam":       bytes([0x0d, 0x0d, 0x83, 0xf0, 0x11, 0x01,
                          0x09, 0x03, 0x84, 0x1c, 0x01, 0x06, 0xc0, 0x7b]),
}

VERIFIED_STOP_COMMANDS = {
    "espresso":    bytes([0x0d, 0x08, 0x83, 0xf0, 0x01, 0x02, 0x06, 0x9d, 0xe1]),
    "coffee":      bytes([0x0d, 0x08, 0x83, 0xf0, 0x02, 0x02, 0x06, 0xc4, 0xb1]),
    "long_coffee": bytes([0x0d, 0x08, 0x83, 0xf0, 0x03, 0x02, 0x06, 0xf3, 0x81]),
    "espresso_2x": bytes([0x0d, 0x08, 0x83, 0xf0, 0x04, 0x02, 0x06, 0x76, 0x11]),
    "doppio_plus": bytes([0x0d, 0x08, 0x83, 0xf0, 0x05, 0x02, 0x06, 0x41, 0x21]),
    "americano":   bytes([0x0d, 0x08, 0x83, 0xf0, 0x06, 0x02, 0x06, 0x18, 0x71]),
    "hot_water":   bytes([0x0d, 0x08, 0x83, 0xf0, 0x10, 0x02, 0x06, 0xe9, 0xb2]),
    "steam":       bytes([0x0d, 0x08, 0x83, 0xf0, 0x11, 0x02, 0x06, 0xde, 0x82]),
}


def cmd_brew_verified(beverage_name: str) -> Optional[bytes]:
    """Get a verified brew command by name. These are known-good from the HA integration."""
    return VERIFIED_COMMANDS.get(beverage_name.lower())


def cmd_stop_verified(beverage_name: str) -> Optional[bytes]:
    """Get a verified stop command by name."""
    return VERIFIED_STOP_COMMANDS.get(beverage_name.lower())


# ── Response Parsing ──────────────────────────────────────────────────────────

class MachineStatus:
    """Parsed machine status from a MonitorV2 response."""

    def __init__(self):
        self.state: MachineState = MachineState.STANDBY
        self.sub_state: int = 0
        self.alarms: list[Alarm] = []
        self.nozzle: NozzleState = NozzleState.UNKNOWN
        self.switches: int = 0
        self.raw: bytes = b""

    @property
    def is_ready(self) -> bool:
        return self.state == MachineState.READY and len(self.alarms) == 0

    @property
    def state_name(self) -> str:
        try:
            return MachineState(self.state).name
        except ValueError:
            return f"UNKNOWN({self.state})"

    def to_dict(self) -> dict:
        return {
            "state": self.state_name,
            "state_code": int(self.state),
            "sub_state": self.sub_state,
            "is_ready": self.is_ready,
            "alarms": [a.name for a in self.alarms],
            "nozzle": NozzleState(self.nozzle).name if self.nozzle in _NOZZLE_VALUES else f"UNKNOWN({self.nozzle})",
            "raw_hex": self.raw.hex(" "),
        }


def parse_monitor_v2(data: bytes) -> Optional[MachineStatus]:
    """
    Parse a MonitorV2 (0x75) response packet.
    Expected: [0xD0] [len] [0x75] [0x0F] [nozzle] [sw_lo] [sw_hi]
              [alarm0] [alarm1] [state] [sub_state] [?] [alarm2] [alarm3] ...
    """
    if len(data) < 14 or data[2] != 0x75:
        return None

    status = MachineStatus()
    status.raw = data

    status.nozzle = NozzleState(data[4]) if data[4] in _NOZZLE_VALUES else data[4]
    status.switches = data[5] + (data[6] << 8)

    alarm_bits = data[7] + (data[8] << 8)
    if len(data) > 13:
        alarm_bits += (data[12] << 16) + (data[13] << 24)

    for alarm in Alarm:
        if alarm_bits & (1 << alarm.value):
            status.alarms.append(alarm)

    status.state = data[9]
    status.sub_state = data[10]

    return status


def parse_recipe_response(data: bytes) -> Optional[dict]:
    """Parse a RecipeQuantityRead (0xA6) response.
    Response: [0xD0] [len] [0xA6] [0x0F] [profile] [beverage_id] [...ingredients] [CRC] [CRC]
    Returns dict with profile, beverage_id, and parsed recipe.
    """
    if len(data) < 8 or data[2] != 0xA6:
        return None

    profile = data[4]
    beverage_id = data[5]

    # Ingredients start at byte 6, end before CRC (last 2 bytes)
    ingredient_bytes = data[6:-2]
    ingredients = decode_recipe(ingredient_bytes)

    return {
        "profile": profile,
        "beverage_id": beverage_id,
        "ingredients": ingredients,
        "recipe": recipe_to_dict(ingredients),
    }


def parse_packet(data: bytes) -> dict:
    """Parse any incoming packet into a structured dict."""
    if len(data) < 4 or data[0] != START_BYTE_IN:
        return {"type": "unknown", "raw": data.hex(" ")}

    cmd_id = data[2]

    if cmd_id == 0x75:  # MonitorV2
        status = parse_monitor_v2(data)
        if status:
            return {"type": "monitor", **status.to_dict()}

    elif cmd_id == 0x83:  # Beverage dispensing response
        return {
            "type": "beverage_response",
            "raw": data.hex(" "),
        }

    elif cmd_id == 0xA2:  # Statistics
        return {
            "type": "statistics",
            "raw": data.hex(" "),
        }

    elif cmd_id == 0xA4:  # Profile names
        return {
            "type": "profiles",
            "raw": data.hex(" "),
        }

    elif cmd_id == 0xA6:  # RecipeQuantityRead response
        recipe = parse_recipe_response(data)
        if recipe:
            return {"type": "recipe", **recipe}

    return {
        "type": f"response_0x{cmd_id:02x}",
        "raw": data.hex(" "),
    }


# ── Beverage Helpers ──────────────────────────────────────────────────────────

# Beverages available on the Dinamica Plus (ECAM 370.85/370.95)
DINAMICA_BEVERAGES = [
    BeverageId.ESPRESSO,
    BeverageId.COFFEE,
    BeverageId.ESPRESSO_2X,
    BeverageId.DOPPIO_PLUS,
    BeverageId.AMERICANO,
    BeverageId.CAPPUCCINO,
    BeverageId.LATTE_MACCHIATO,
    BeverageId.CAFFE_LATTE,
    BeverageId.FLAT_WHITE,
    BeverageId.ESPRESSO_MACCHIATO,
    BeverageId.HOT_MILK,
    BeverageId.CAPPUCCINO_DOPPIO,
    BeverageId.CAPPUCCINO_MIX,
    BeverageId.HOT_WATER,
    BeverageId.STEAM,
]

# Fallback defaults — only used when no saved recipe is available
BEVERAGE_DEFAULTS = {
    BeverageId.ESPRESSO:        {"quantity_ml": 40,  "aroma": Aroma.NORMAL},
    BeverageId.COFFEE:          {"quantity_ml": 120, "aroma": Aroma.NORMAL},
    BeverageId.LONG_COFFEE:     {"quantity_ml": 160, "aroma": Aroma.NORMAL},
    BeverageId.ESPRESSO_2X:     {"quantity_ml": 80,  "aroma": Aroma.NORMAL},
    BeverageId.DOPPIO_PLUS:     {"quantity_ml": 120, "aroma": Aroma.NORMAL},
    BeverageId.AMERICANO:       {"quantity_ml": 150, "aroma": Aroma.NORMAL},
    BeverageId.RISTRETTO:       {"quantity_ml": 25,  "aroma": Aroma.STRONG},
    BeverageId.HOT_WATER:       {"quantity_ml": 250, "aroma": Aroma.NORMAL},
}


def get_beverage_names() -> dict[str, int]:
    """Return a dict of beverage name -> id for the HTTP API."""
    return {b.name.lower(): b.value for b in BeverageId}
