# De'Longhi ECAM Bluetooth Protocol

Complete documentation of the BLE protocol used by De'Longhi ECAM coffee machines.

## BLE Service

| Parameter | UUID |
|-----------|------|
| Service | `00035b03-58e6-07dd-021a-08123a000300` |
| Control Characteristic | `00035b03-58e6-07dd-021a-08123a000301` |
| Descriptor (CCCD) | `00002902-0000-1000-8000-00805f9b34fb` |

The control characteristic supports **Write**, **Read**, and **Indicate** (notifications). All communication happens through this single characteristic.

## Packet Format

### Outbound (Host → Machine)

```
[0x0D] [length] [command_id] [0xF0] [params...] [CRC_hi] [CRC_lo]
```

- `0x0D` — Start byte (always)
- `length` — Number of bytes that follow (including CRC), i.e., total packet length - 1
- `command_id` — The request type (see Command IDs below)
- `0xF0` — Response flag
- `params` — Command-specific parameters
- `CRC` — CRC-16/CCITT big-endian (see Checksum section)

### Inbound (Machine → Host)

```
[0xD0] [length] [response_id] [0x0F] [data...] [CRC_hi] [CRC_lo]
```

- `0xD0` — Start byte (always)
- `length` — Number of bytes that follow
- `response_id` — Response type (mirrors request IDs)
- `0x0F` — Response flag
- `data` — Response-specific payload

## Checksum

**CRC-16/CCITT** with initial value `0x1D0F`, big-endian output.

Applied to all bytes **except** the last two (which are the CRC itself).

```python
from binascii import crc_hqx

def compute_crc(packet_without_crc: bytes) -> bytes:
    crc = crc_hqx(packet_without_crc, 0x1D0F)
    return crc.to_bytes(2, byteorder="big")
```

## Command IDs

| ID | Name | Direction |
|----|------|-----------|
| `0x75` | MonitorV2 | Request/Response |
| `0x83` | BeverageDispensingMode | Request/Response |
| `0x84` | AppControl (Power) | Request |
| `0x90` | ParameterWrite | Request |
| `0xA2` | StatisticsRead | Request/Response |
| `0xA4` | ProfileNameRead | Request/Response |
| `0xA6` | RecipeQuantityRead | Request/Response |
| `0xA9` | ProfileSelection | Request |
| `0xB0` | RecipeMinMaxSync | Request/Response |
| `0xE2` | SetTime | Request |

## Beverage IDs

| ID | Beverage |
|----|----------|
| `0x01` | Espresso |
| `0x02` | Regular Coffee |
| `0x03` | Long Coffee |
| `0x04` | Espresso 2x |
| `0x05` | Doppio+ |
| `0x06` | Americano |
| `0x07` | Cappuccino |
| `0x08` | Latte Macchiato |
| `0x09` | Caffè Latte |
| `0x0A` | Flat White |
| `0x0B` | Espresso Macchiato |
| `0x0C` | Hot Milk |
| `0x0D` | Cappuccino Doppio+ |
| `0x0E` | Cold Milk |
| `0x0F` | Cappuccino Mix (CappuccinoReverse — milk-first cappuccino) |
| `0x10` | Hot Water |
| `0x11` | Steam |
| `0x13` | Ristretto |
| `0x14` | Long Espresso |
| `0x16` | Tea |
| `0x17` | Coffee Pot |
| `0x18` | Cortado |
| `0x19` | Long Black |
| `0x1A` | Travel Mug |
| `0x1B` | Brew Over Ice |

## Beverage Command Structure

### Start Brew (with recipe from profile)

The preferred method is to read the saved recipe from the machine, then send it back:

```
[0x0D] [len] [0x83] [0xF0] [bev_id] [0x01] [...ingredients] [taste_type] [CRC_hi] [CRC_lo]
```

- `bev_id` — Beverage ID from table above
- `0x01` (trigger) — Start
- `ingredients` — Variable-length ingredient list (see Ingredient Encoding)
- `taste_type` — `0x02` (Prepare) or `0x06` (PrepareInversion, for milk-first drinks)

### Taste Types (EcamBeverageTasteType)

| Value | Name |
|-------|------|
| `0x00` | Delete |
| `0x01` | Save |
| `0x02` | Prepare |
| `0x03` | PrepareAndSave |
| `0x05` | SaveInversion |
| `0x06` | PrepareInversion |
| `0x07` | PrepareAndSaveInversion |

### Legacy Start Brew (hardcoded parameters)

```
[0x0D] [len] [0x83] [0xF0] [bev_id] [0x01] [0x01] [0x00]
[quantity] [0x02] [aroma] [0x08] [temp] [0x00] [0x00] [0x06]
[CRC_hi] [CRC_lo]
```

- `quantity` — Volume in ml as single byte (e.g., `0x28` = 40ml)
- `aroma` — `0x01` (extra mild) to `0x05` (extra strong)
- `temp` — `0x00` (low) to `0x03` (very high)

### Stop Brew

```
[0x0D] [0x08] [0x83] [0xF0] [bev_id] [0x02] [0x06] [CRC_hi] [CRC_lo]
```

## Machine Control Commands

### Power On

```
0D 07 84 0F 02 01 55 12
```

### Monitor (Status Request)

```
0D 05 75 0F DA 25
```

## Monitor Response (0x75)

```
[0xD0] [len] [0x75] [0x0F] [nozzle] [switches_lo] [switches_hi]
[alarms_0] [alarms_1] [state] [sub_state] [?] [alarms_2] [alarms_3] ...
```

### Machine States

| Code | State |
|------|-------|
| `0` | Standby |
| `1` | Turning On |
| `2` | Shutting Down |
| `4` | Descaling |
| `5` | Steam Preparation |
| `6` | Recovery |
| `7` | Ready / Dispensing |
| `8` | Rinsing |
| `10` | Milk Preparation |
| `11` | Hot Water Delivery |
| `12` | Milk Cleaning |

### Alarm Bits

Alarms are encoded as a 32-bit bitmask across bytes 7-8 and 12-13:

```
alarm_bits = byte[7] + (byte[8] << 8) + (byte[12] << 16) + (byte[13] << 24)
```

| Bit | Alarm |
|-----|-------|
| 0 | Empty water tank |
| 1 | Waste container full |
| 2 | Descale needed |
| 3 | Replace water filter |
| 4 | Coffee ground too fine |
| 5 | Beans empty |
| 6 | Machine needs service |
| 7 | Heater probe failure |
| 8 | Too much coffee |
| 9 | Infuser motor failure |
| 10 | Steamer probe failure |
| 11 | Drip tray missing |
| 12 | Hydraulic problem |
| 13 | Tank in position |
| 15 | Beans empty (secondary) |
| 16 | Tank too full |
| 17 | Bean hopper absent |

### Nozzle State

| Value | State |
|-------|-------|
| `0` | Detached |
| `1` | Steam |
| `2` | Milk frother |
| `3` | Milk frother (cleaning) |

## Ingredient Parameters

Ingredients are encoded as variable-length fields. **Wide** ingredients use a 2-byte big-endian value; **narrow** ingredients use a 1-byte value.

| ID | Ingredient | Encoding | Notes |
|----|------------|----------|-------|
| `0x00` | Temperature | Narrow (1 byte) | `0x00`=Low, `0x01`=Mid, `0x02`=High, `0x03`=Very High |
| `0x01` | Coffee quantity | **Wide (2 bytes)** | Value in ml |
| `0x02` | Taste/Aroma | Narrow (1 byte) | `0x01`=Extra Mild to `0x05`=Extra Strong |
| `0x08` | DueXPer | Narrow (1 byte) | Double-shot parameter |
| `0x09` | Milk quantity | **Wide (2 bytes)** | Duration in seconds |
| `0x0C` | Inversion | Narrow (1 byte) | `0x00`=normal, `0x01`=milk first |
| `0x0F` | Hot water quantity | **Wide (2 bytes)** | Value in ml |
| `0x18` | Programmable | Narrow (1 byte) | |
| `0x19` | Visible | Narrow (1 byte) | |
| `0x1B` | IndexLength | Narrow (1 byte) | |
| `0x1C` | Accessorio | Narrow (1 byte) | |

### Ingredient Encoding Example

Cappuccino with coffee=65ml, milk=190s, taste=Normal, inversion=off:
```
01 00 41   # Coffee (wide): 0x0041 = 65
09 00 BE   # Milk (wide): 0x00BE = 190
02 03      # Taste (narrow): 0x03 = Normal
0C 00      # Inversion (narrow): 0x00 = off
```

## Recipe Reading (RecipeQuantityRead — 0xA6)

Read the saved recipe for a beverage from a machine profile.

### Request

```
[0x0D] [len] [0xA6] [0xF0] [profile] [beverage_id] [CRC_hi] [CRC_lo]
```

- `profile` — Profile number (typically `0x01`)
- `beverage_id` — Beverage ID from table above

### Response

```
[0xD0] [len] [0xA6] [0x0F] [profile] [beverage_id] [...ingredients] [CRC_hi] [CRC_lo]
```

The ingredient list uses the same encoding as brew commands. Parse until you run out of bytes (before CRC).

## Settings Commands

### Switch Toggles (Energy save, Cup light, Sounds)

```
0D 0B 90 0F 00 3F 00 00 00 [bitmask] [CRC_hi] [CRC_lo]
```

### Water Hardness

```
0D 0B 90 0F 00 32 00 00 00 [level] [CRC_hi] [CRC_lo]
```

Level: `0x01` to `0x04`

### Profile Selection

```
0D [len] A9 F0 [profile_id] [CRC_hi] [CRC_lo]
```

Profile IDs: `0x01` to `0x04`

## Verified Commands

These exact byte sequences have been tested on real hardware:

```python
# Espresso (40ml, aroma=normal, temp=mid)
ESPRESSO = bytes([0x0d, 0x11, 0x83, 0xf0, 0x01, 0x01, 0x01, 0x00,
                  0x28, 0x02, 0x03, 0x08, 0x00, 0x00, 0x00, 0x06, 0x8f, 0xfc])

# Coffee (103ml, aroma=mild)
COFFEE = bytes([0x0d, 0x0f, 0x83, 0xf0, 0x02, 0x01, 0x01,
                0x00, 0x67, 0x02, 0x02, 0x00, 0x00, 0x06, 0x77, 0xff])

# Americano
AMERICANO = bytes([0x0d, 0x12, 0x83, 0xf0, 0x06, 0x01, 0x01, 0x00,
                   0x28, 0x02, 0x03, 0x0f, 0x00, 0x6e, 0x00, 0x00,
                   0x06, 0x47, 0x8b])

# Power On
POWER_ON = bytes([0x0d, 0x07, 0x84, 0x0f, 0x02, 0x01, 0x55, 0x12])

# Monitor (Status Poll)
MONITOR = bytes([0x0d, 0x05, 0x75, 0x0f, 0xda, 0x25])
```
