# Barista — De'Longhi ECAM BLE-to-HTTP Bridge

## Project Overview
Open-source BLE-to-HTTP bridge for De'Longhi ECAM coffee machines (tested on Dinamica Plus ECAM 370.85/370.95). Provides a Web UI and REST API to control the machine.

## Architecture
- **`barista/protocol.py`** — ECAM BLE protocol: packet building, CRC, command builders, recipe encode/decode, response parsers
- **`barista/ble.py`** — BLE driver using bleak: scanning, connecting, send/receive, auto-reconnect, notification handling
- **`barista/server.py`** — aiohttp HTTP server: REST API endpoints, recipe caching, profile management, Web UI serving
- **`barista/ui/index.html`** — Single-file Web UI (HTML/CSS/JS): beverage grid, recipe display, profile switcher, brew modal
- **`barista/cli.py`** — CLI entry point: `barista scan`, `barista start --address XX:XX`
- **`docs/PROTOCOL.md`** — Complete ECAM BLE protocol documentation

## Key Concepts

### Profile-Based Brewing
The machine stores beverage recipes per user profile (1-4). On startup, the server reads all saved recipes via `RecipeQuantityRead` (0xA6) and caches them. Brewing sends the exact saved recipe back to the machine — matching the De'Longhi app behavior.

### Ingredient Encoding
Ingredients use variable-length encoding:
- **Wide (2-byte big-endian)**: Coffee (0x01), Milk (0x09), HotWater (0x0F)
- **Narrow (1-byte)**: Temperature (0x00), Taste (0x02), Inversion (0x0C), etc.

Milk values from the machine are in **tenths of seconds** (e.g., 310 = 31.0 sec).

### Beverage Discovery
- Cappuccino Mix = beverage ID `0x0F` (CappuccinoReverse in longshot — confirmed working on Dinamica Plus)
- Cortado (`0x18`) is NOT available on the Dinamica Plus
- The `DINAMICA_BEVERAGES` list in protocol.py defines which beverages appear in the UI

## Machine Address
BLE address: `00:A0:50:2A:D2:8F` (device name: D1533270)

## Testing
```bash
python -m pytest tests/ -v          # All tests (88)
python -m barista scan              # Find machine
python -m barista start --address 00:A0:50:2A:D2:8F  # Start server
curl http://localhost:8080/api/recipes   # Check cached recipes
```

## Reference Implementations
- [mmastrac/longshot](https://github.com/mmastrac/longshot) — Rust ECAM CLI (primary protocol reference)
- [Arbuzov/home_assistant_delonghi_primadonna](https://github.com/Arbuzov/home_assistant_delonghi_primadonna) — HA integration (verified command bytes)
