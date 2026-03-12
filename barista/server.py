"""
De'Longhi Coffee Machine HTTP Server + Web UI
BLE-to-HTTP bridge with a stunning control interface.

Usage:
    python server.py scan                    # Find your machine
    python server.py serve --address XX:XX   # Start HTTP server + UI
"""

import asyncio
import json
import logging
import os
import sys
import time
from typing import Optional
from pathlib import Path

from aiohttp import web

from barista.protocol import (
    BeverageId,
    Aroma,
    Temperature,
    BEVERAGE_DEFAULTS,
    VERIFIED_COMMANDS,
    VERIFIED_STOP_COMMANDS,
    cmd_brew,
    cmd_brew_verified,
    cmd_brew_stop,
    cmd_stop_verified,
    cmd_hot_water,
    cmd_monitor,
    cmd_power_on,
    cmd_steam,
    cmd_profile_select,
    get_beverage_names,
)
from barista.ble import DelonghiBLE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("delonghi.server")

# ── Globals ───────────────────────────────────────────────────────────────────

machine = DelonghiBLE()
PORT = 8080
MACHINE_ADDRESS = None
START_TIME = time.time()


# ── HTTP Handlers ─────────────────────────────────────────────────────────────

def json_response(data: dict, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data, indent=2),
        content_type="application/json",
        status=status,
        headers={"Access-Control-Allow-Origin": "*"},
    )


async def handle_ui(request: web.Request) -> web.Response:
    """GET / - Serve the Web UI."""
    ui_path = Path(__file__).parent / "ui" / "index.html"
    if ui_path.exists():
        return web.Response(
            text=ui_path.read_text(encoding="utf-8"),
            content_type="text/html",
        )
    return web.Response(text="UI not found", status=404)


async def handle_api(request: web.Request) -> web.Response:
    """GET /api — Show available endpoints."""
    return json_response({
        "service": "De'Longhi Coffee Machine API",
        "connected": machine.connected,
        "uptime_seconds": round(time.time() - START_TIME),
        "machine_address": MACHINE_ADDRESS,
        "endpoints": {
            "GET /":               "Web UI",
            "GET /api":            "API info",
            "GET /api/status":     "Machine status",
            "POST /api/power":     "Turn machine on",
            "POST /api/brew":      "Brew a beverage",
            "POST /api/brew/stop": "Stop current brew",
            "POST /api/steam":     "Start steam",
            "POST /api/hot-water": "Dispense hot water",
            "POST /api/profile":   "Select profile",
            "GET /api/beverages":  "List available beverages",
            "GET /api/scan":       "Scan for machines",
            "POST /api/reconnect": "Force reconnect",
        },
    })


async def handle_status(request: web.Request) -> web.Response:
    """GET /api/status — Get machine status."""
    base = {
        "connected": machine.connected,
        "uptime_seconds": round(time.time() - START_TIME),
        "status_age_seconds": round(machine.get_status_age(), 1),
    }

    if not machine.connected:
        return json_response({**base, "state": "DISCONNECTED", "is_ready": False}, 200)

    status = await machine.request_status()
    if status:
        return json_response({**base, **status})
    else:
        cached = machine.get_last_status()
        if cached:
            return json_response({**base, **cached, "note": "cached"})
        return json_response({**base, "state": "NO_RESPONSE", "is_ready": False})


async def handle_power(request: web.Request) -> web.Response:
    """POST /api/power — Turn machine on."""
    if not machine.connected:
        return json_response({"error": "Not connected", "connected": False}, 503)

    ok = await machine.send(cmd_power_on())
    return json_response({"success": ok, "action": "power_on"})


async def handle_brew(request: web.Request) -> web.Response:
    """POST /api/brew — Brew a beverage. Prefers verified commands."""
    if not machine.connected:
        return json_response({"error": "Not connected", "connected": False}, 503)

    try:
        body = await request.json()
    except Exception:
        body = {}

    bev_name = body.get("beverage", "coffee").lower().replace(" ", "_")

    # Try verified command first (known-good from HA integration)
    verified = cmd_brew_verified(bev_name)
    if verified:
        ok = await machine.send(verified)
        return json_response({
            "success": ok,
            "action": "brew",
            "beverage": bev_name,
            "method": "verified",
        })

    # Fallback to computed command
    names = get_beverage_names()
    if bev_name in names:
        bev_id = BeverageId(names[bev_name])
    elif bev_name.isdigit():
        bev_id = BeverageId(int(bev_name))
    else:
        return json_response({
            "error": f"Unknown beverage: {bev_name}",
            "available": list(VERIFIED_COMMANDS.keys()) + list(names.keys()),
        }, 400)

    defaults = BEVERAGE_DEFAULTS.get(bev_id, {"quantity_ml": 100, "aroma": Aroma.NORMAL})
    quantity = body.get("quantity_ml", defaults["quantity_ml"])
    aroma = Aroma(body.get("aroma", defaults["aroma"]))
    temp = Temperature(body.get("temperature", Temperature.HIGH))

    cmd = cmd_brew(bev_id, quantity_ml=quantity, aroma=aroma, temperature=temp)
    ok = await machine.send(cmd)

    return json_response({
        "success": ok,
        "action": "brew",
        "beverage": bev_id.name,
        "quantity_ml": quantity,
        "aroma": aroma.name,
        "temperature": temp.name,
        "method": "computed",
    })


async def handle_brew_stop(request: web.Request) -> web.Response:
    """POST /api/brew/stop — Stop current brew."""
    if not machine.connected:
        return json_response({"error": "Not connected"}, 503)

    try:
        body = await request.json()
    except Exception:
        body = {}

    bev_name = body.get("beverage", "coffee").lower().replace(" ", "_")

    verified = cmd_stop_verified(bev_name)
    if verified:
        ok = await machine.send(verified)
    else:
        names = get_beverage_names()
        bev_id = BeverageId(names.get(bev_name, 0x02))
        ok = await machine.send(cmd_brew_stop(bev_id))

    return json_response({"success": ok, "action": "brew_stop"})


async def handle_steam(request: web.Request) -> web.Response:
    """POST /api/steam — Start steam."""
    if not machine.connected:
        return json_response({"error": "Not connected"}, 503)
    ok = await machine.send(cmd_steam())
    return json_response({"success": ok, "action": "steam"})


async def handle_hot_water(request: web.Request) -> web.Response:
    """POST /api/hot-water — Dispense hot water."""
    if not machine.connected:
        return json_response({"error": "Not connected"}, 503)
    try:
        body = await request.json()
    except Exception:
        body = {}
    quantity = body.get("quantity_ml", 250)
    ok = await machine.send(cmd_hot_water(quantity))
    return json_response({"success": ok, "action": "hot_water", "quantity_ml": quantity})


async def handle_profile(request: web.Request) -> web.Response:
    """POST /api/profile — Select profile."""
    if not machine.connected:
        return json_response({"error": "Not connected"}, 503)
    try:
        body = await request.json()
    except Exception:
        body = {}
    profile_id = body.get("profile_id", 1)
    ok = await machine.send(cmd_profile_select(profile_id))
    return json_response({"success": ok, "action": "profile_select", "profile_id": profile_id})


async def handle_beverages(request: web.Request) -> web.Response:
    """GET /api/beverages — List available beverages."""
    beverages = []

    # Verified beverages first (these are proven to work)
    for name in VERIFIED_COMMANDS:
        defaults = {}
        try:
            bev_id = BeverageId[name.upper()]
            defaults = BEVERAGE_DEFAULTS.get(bev_id, {})
        except (KeyError, ValueError):
            pass
        beverages.append({
            "name": name,
            "verified": True,
            "default_quantity_ml": defaults.get("quantity_ml"),
            "has_stop": name in VERIFIED_STOP_COMMANDS,
        })

    # Additional computed beverages
    names = get_beverage_names()
    verified_set = set(VERIFIED_COMMANDS.keys())
    for name, bid in sorted(names.items(), key=lambda x: x[1]):
        if name not in verified_set:
            defaults = BEVERAGE_DEFAULTS.get(BeverageId(bid), {})
            beverages.append({
                "name": name,
                "verified": False,
                "id": bid,
                "default_quantity_ml": defaults.get("quantity_ml"),
            })

    return json_response({"beverages": beverages})


async def handle_scan(request: web.Request) -> web.Response:
    """GET /api/scan — Scan for BLE coffee machines."""
    devices = await DelonghiBLE.scan(timeout=10.0)
    return json_response({"devices": devices})


async def handle_reconnect(request: web.Request) -> web.Response:
    """POST /api/reconnect — Force reconnect to machine."""
    if MACHINE_ADDRESS:
        await machine.disconnect()
        machine._auto_reconnect = True
        ok = await machine.connect(MACHINE_ADDRESS)
        return json_response({"success": ok, "connected": machine.connected})
    return json_response({"error": "No machine address configured"}, 400)


async def handle_cors(request: web.Request) -> web.Response:
    """Handle CORS preflight."""
    return web.Response(
        status=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
    )


# ── Server Setup ──────────────────────────────────────────────────────────────

def create_app() -> web.Application:
    app = web.Application()

    # Web UI
    app.router.add_get("/", handle_ui)

    # API
    app.router.add_get("/api", handle_api)
    app.router.add_get("/api/status", handle_status)
    app.router.add_post("/api/power", handle_power)
    app.router.add_post("/api/brew", handle_brew)
    app.router.add_post("/api/brew/stop", handle_brew_stop)
    app.router.add_post("/api/steam", handle_steam)
    app.router.add_post("/api/hot-water", handle_hot_water)
    app.router.add_post("/api/profile", handle_profile)
    app.router.add_get("/api/beverages", handle_beverages)
    app.router.add_get("/api/scan", handle_scan)
    app.router.add_post("/api/reconnect", handle_reconnect)

    # CORS
    app.router.add_route("OPTIONS", "/{path:.*}", handle_cors)

    return app


# ── CLI Commands ──────────────────────────────────────────────────────────────

async def cmd_scan():
    """Scan for machines and print results."""
    print("\nScanning for De'Longhi coffee machines...\n")
    devices = await DelonghiBLE.scan(timeout=10.0)

    if not devices:
        print("No machines found!")
        print("\nTroubleshooting:")
        print("  1. Make sure Bluetooth is ON on your PC")
        print("  2. Make sure the coffee machine is powered on")
        print("  3. Disconnect the De'Longhi app from your phone")
        print("  4. Try moving closer to the machine")
        return

    print(f"Found {len(devices)} device(s):\n")
    for d in devices:
        rssi_str = f" (RSSI: {d['rssi']})" if d.get("rssi") else ""
        print(f"  Name:    {d['name']}")
        print(f"  Address: {d['address']}{rssi_str}")
        print()

    print("To start the server, run:")
    print(f"  python server.py serve --address {devices[0]['address']}")


async def cmd_serve(address: str, port: int = 8080):
    """Connect to machine and start HTTP server."""
    global MACHINE_ADDRESS
    MACHINE_ADDRESS = address

    print(f"\nConnecting to {address}...")

    ok = await machine.connect(address)
    if not ok:
        print("Failed to connect! Make sure:")
        print("  1. The machine is ON")
        print("  2. De'Longhi app is DISCONNECTED")
        print("  3. You're close enough (BLE range ~10m)")
        print("\nStarting server anyway (will auto-reconnect)...")

    if ok:
        print("Connected! Requesting initial status...")
        status = await machine.request_status()
        if status:
            print(f"  Machine state: {status.get('state', 'unknown')}")
            print(f"  Ready: {status.get('is_ready', 'unknown')}")
            if status.get("alarms"):
                print(f"  Alarms: {', '.join(status['alarms'])}")

    # Start background monitoring
    def on_status(parsed):
        if parsed.get("type") == "monitor":
            state = parsed.get("state", "?")
            alarms = parsed.get("alarms", [])
            if alarms:
                logger.info(f"Status: {state} | Alarms: {', '.join(alarms)}")

    await machine.start_monitoring(interval=10.0, callback=on_status)

    print(f"\n{'='*60}")
    print(f"  De'Longhi Coffee Machine Control")
    print(f"  Web UI:  http://localhost:{port}")
    print(f"  API:     http://localhost:{port}/api")
    print(f"{'='*60}")
    print(f"\n  Press Ctrl+C to stop\n")

    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        print("\nShutting down...")
        await machine.disconnect()
        await runner.cleanup()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("De'Longhi Coffee Machine HTTP Server")
        print()
        print("Usage:")
        print("  python server.py scan                              Scan for machines")
        print("  python server.py serve --address XX:XX:XX:XX:XX:XX Start server + UI")
        print("  python server.py serve --address XX:XX --port 9090 Custom port")
        print()
        sys.exit(0)

    command = sys.argv[1]

    if command == "scan":
        asyncio.run(cmd_scan())

    elif command == "serve":
        address = None
        port = PORT
        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--address" and i + 1 < len(sys.argv):
                address = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--port" and i + 1 < len(sys.argv):
                port = int(sys.argv[i + 1])
                i += 2
            else:
                i += 1

        if not address:
            print("Error: --address is required. Run 'python server.py scan' first.")
            sys.exit(1)

        asyncio.run(cmd_serve(address, port))

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
