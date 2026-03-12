<div align="center">

# barista ☕

**Control your De'Longhi coffee machine from your terminal, browser, or any automation.**

An open-source BLE-to-HTTP bridge that turns your De'Longhi ECAM coffee machine into a fully automatable, API-driven appliance.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB.svg?style=flat&logo=python&logoColor=white)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg?style=flat)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/barista-coffee.svg?style=flat)](https://pypi.org/project/barista-coffee/)

---

**Brew from your terminal** · **Beautiful web UI** · **HTTP API for automations** · **Works on Windows, macOS, Linux**

</div>

<br>

## The Problem

De'Longhi makes incredible coffee machines with Bluetooth — then locks you into their app. No automations. No API. No way to:

- Brew coffee when your morning alarm goes off
- Trigger an espresso from a keyboard shortcut
- Monitor your machine from a smart home dashboard
- Build *anything* custom around your coffee workflow

## The Solution

**Barista** talks to your De'Longhi over Bluetooth Low Energy and exposes everything through a clean HTTP API and a beautiful web UI.

```bash
pip install barista-coffee
barista scan                           # Find your machine
barista start --address AA:BB:CC:DD    # Start the server
```

Then open **http://localhost:8080** — or hit the API from anywhere on your network.

<br>

## Quick Start

### 1. Install

```bash
pip install barista-coffee
```

### 2. Find Your Machine

Make sure:
- Your coffee machine is **powered on**
- The De'Longhi app on your phone is **disconnected** (only one BLE connection at a time)
- You're within **Bluetooth range** (~10 meters)

```bash
barista scan
```

```
Scanning for De'Longhi coffee machines...

Found 1 device(s):

  Name:    D1533270
  Address: 00:A0:50:2A:D2:8F
```

### 3. Start Barista

```bash
barista start --address 00:A0:50:2A:D2:8F
```

```
============================================================
  De'Longhi Coffee Machine Control
  Web UI:  http://localhost:8080
  API:     http://localhost:8080/api
============================================================
```

### 4. Brew

**From the web UI:**

Open http://localhost:8080 and tap a drink.

**From the terminal:**

```bash
# Espresso
curl -X POST http://localhost:8080/api/brew \
  -H "Content-Type: application/json" \
  -d '{"beverage": "espresso"}'

# Coffee with custom settings
curl -X POST http://localhost:8080/api/brew \
  -H "Content-Type: application/json" \
  -d '{"beverage": "coffee", "quantity_ml": 150, "aroma": 4}'

# Check status
curl http://localhost:8080/api/status
```

<br>

## Web UI

Barista ships with a built-in web interface — dark-themed, mobile-friendly, real-time status updates.

- **Tap to brew** any of 14+ beverages
- **Live machine status** — state, alarms, nozzle detection
- **Connection management** — reconnect with one click
- **Activity log** — see what's happening

> Access it at `http://localhost:8080` from any device on your network.

<br>

## HTTP API

All endpoints return JSON. Use from any language, any automation platform.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/status` | Machine state, alarms, readiness |
| `POST` | `/api/brew` | Brew a beverage |
| `POST` | `/api/brew/stop` | Stop current brew |
| `POST` | `/api/power` | Turn machine on |
| `POST` | `/api/steam` | Start steam wand |
| `POST` | `/api/hot-water` | Dispense hot water |
| `POST` | `/api/profile` | Select user profile (1-4) |
| `GET` | `/api/beverages` | List available drinks |
| `GET` | `/api/scan` | Scan for BLE machines |
| `POST` | `/api/reconnect` | Force BLE reconnect |

### Brew Parameters

```json
{
  "beverage": "espresso",
  "quantity_ml": 40,
  "aroma": 3,
  "temperature": 2
}
```

| Parameter | Values |
|-----------|--------|
| `beverage` | `espresso`, `coffee`, `long_coffee`, `espresso_2x`, `doppio_plus`, `americano`, `cappuccino`, `latte_macchiato`, `flat_white`, `caffe_latte`, `hot_water`, `steam`, `ristretto`, `cortado`, ... |
| `aroma` | `1` (extra mild) → `5` (extra strong) |
| `temperature` | `0` (low) → `3` (very high) |
| `quantity_ml` | Depends on beverage (e.g., 25-250) |

### Status Response

```json
{
  "connected": true,
  "state": "READY",
  "is_ready": true,
  "alarms": [],
  "nozzle": "DETACHED"
}
```

<br>

## Automation Examples

### Morning Coffee Cron Job (Linux/macOS)

```cron
# Brew a coffee every weekday at 7:00 AM
0 7 * * 1-5 curl -s -X POST http://localhost:8080/api/brew -H "Content-Type: application/json" -d '{"beverage":"coffee"}'
```

### Python Script

```python
import requests

# Check if machine is ready
status = requests.get("http://localhost:8080/api/status").json()
if status["is_ready"]:
    requests.post("http://localhost:8080/api/brew", json={
        "beverage": "espresso",
        "aroma": 4,  # strong
    })
```

### Home Assistant (REST command)

```yaml
rest_command:
  brew_espresso:
    url: "http://YOUR_PC_IP:8080/api/brew"
    method: POST
    content_type: "application/json"
    payload: '{"beverage": "espresso"}'

  brew_coffee:
    url: "http://YOUR_PC_IP:8080/api/brew"
    method: POST
    content_type: "application/json"
    payload: '{"beverage": "coffee"}'

  coffee_machine_power:
    url: "http://YOUR_PC_IP:8080/api/power"
    method: POST
```

### Apple Shortcuts / HTTP Shortcut (Android)

Just point an HTTP action at:
```
POST http://YOUR_PC_IP:8080/api/brew
Body: {"beverage": "espresso"}
```

### Node-RED

Use an HTTP Request node pointed at `http://localhost:8080/api/brew` with a JSON payload.

<br>

## Supported Machines

Barista works with De'Longhi machines that use the **ECAM Bluetooth protocol** via the "De'Longhi Coffee Link" app.

### Tested

| Model | Status |
|-------|--------|
| **Dinamica Plus** (ECAM 370.85 / 370.95) | ✅ Fully working |

### Should Work (same protocol)

| Model | Notes |
|-------|-------|
| Primadonna Elite (ECAM 650.x) | Same ECAM protocol, likely works |
| Primadonna Soul (ECAM 610.x) | Same ECAM protocol |
| Dinamica (ECAM 350.x) | Same ECAM protocol |
| Magnifica Evo (ECAM 290.x) | Same ECAM protocol |
| Eletta Explore (ECAM 450.x) | Same ECAM protocol |

> **Your machine isn't listed?** If it works with the "De'Longhi Coffee Link" app over Bluetooth, it likely uses the same ECAM protocol. Try `barista scan` — if it finds your machine, there's a good chance it works. Please [open an issue](https://github.com/assafakiva/barista/issues) to report compatibility!

<br>

## Platform Notes

### Windows

Works out of the box with Windows 10/11 and built-in Bluetooth. Uses the Windows BLE stack via [bleak](https://github.com/hbldh/bleak).

### macOS

Works out of the box. You may need to grant Bluetooth permissions to your terminal app.

### Linux

Requires BlueZ. You may need to grant BLE capabilities:
```bash
sudo setcap cap_net_raw+eip $(readlink -f $(which python3))
```

<br>

## How It Works

Barista reverse-engineers the De'Longhi ECAM Bluetooth Low Energy protocol:

1. **Connects** to your machine via BLE using a known service UUID
2. **Sends commands** as structured binary packets with CRC-16 checksums
3. **Receives status** via BLE notifications (machine state, alarms, brew progress)
4. **Exposes everything** through a local HTTP API and web UI

The protocol uses:
- **Service UUID**: `00035b03-58e6-07dd-021a-08123a000300`
- **Characteristic**: `00035b03-58e6-07dd-021a-08123a000301`
- **Packet format**: `[0x0D] [length] [command] [params...] [CRC16]`
- **CRC**: CRC-16/CCITT with init `0x1D0F`

> For full protocol documentation, see [docs/PROTOCOL.md](docs/PROTOCOL.md).

<br>

## Troubleshooting

### "No machines found" during scan

1. Make sure the coffee machine is **powered on** (display lit up)
2. **Disconnect** the De'Longhi app on your phone — only one BLE connection is allowed
3. Move **closer** to the machine (BLE range is ~10m)
4. On Linux, make sure BlueZ is running and you have BLE permissions

### "Connection failed"

- The machine may have gone to BLE sleep — press any button on it to wake it up
- Try again — BLE connections can be flaky on the first attempt
- Barista has auto-reconnect built in; it will keep trying

### Command sent but machine doesn't brew

- Check that `is_ready` is `true` in the status response
- The machine needs water, beans, and an empty drip tray
- Some beverages (cappuccino, latte) require the milk frother to be attached

<br>

## Contributing

Contributions are welcome! Especially:

- **Testing on other ECAM models** — report which machines work
- **Protocol discoveries** — new commands, parameters, or status fields
- **Platform fixes** — BLE quirks on different OS versions
- **Integrations** — Home Assistant components, Alexa skills, etc.

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

<br>

## Acknowledgments

The ECAM Bluetooth protocol was decoded thanks to the work of the open-source community:

- [mmastrac/longshot](https://github.com/mmastrac/longshot) — Rust ECAM CLI/API with comprehensive protocol enums
- [Arbuzov/home_assistant_delonghi_primadonna](https://github.com/Arbuzov/home_assistant_delonghi_primadonna) — Home Assistant integration with verified command bytes
- [manekinekko/cafy](https://github.com/manekinekko/cafy) — TypeScript implementation with BLE packet documentation

Barista is an original implementation built from scratch, informed by the protocol knowledge documented by these projects.

<br>

## License

MIT — see [LICENSE](LICENSE).

<br>

<div align="center">

Made with ☕ and a De'Longhi Dinamica Plus.

</div>
