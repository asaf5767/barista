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
    Ingredient,
    Temperature,
    BEVERAGE_DEFAULTS,
    DINAMICA_BEVERAGES,
    VERIFIED_COMMANDS,
    VERIFIED_STOP_COMMANDS,
    cmd_brew,
    cmd_brew_recipe,
    cmd_brew_verified,
    cmd_brew_stop,
    cmd_stop_verified,
    cmd_hot_water,
    cmd_monitor,
    cmd_power_on,
    cmd_recipe_read,
    cmd_steam,
    cmd_profile_select,
    get_beverage_names,
    recipe_to_dict,
    recipe_from_dict,
)
from barista.ble import DelonghiBLE
from barista.scheduler import setup_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("delonghi.server")

# ── Globals ───────────────────────────────────────────────────────────────────

machine = DelonghiBLE()
PORT = 8080
BIND_HOST = "127.0.0.1"
CORS_ORIGIN = "http://localhost:8080"  # Updated at startup; never wildcard
MACHINE_ADDRESS = None
START_TIME = time.time()

# Recipe cache: beverage_name -> list of (ingredient_id, value) tuples
recipe_cache: dict[str, list[tuple[int, int]]] = {}
current_profile: int = 1


# ── Recipe Management ────────────────────────────────────────────────────────

async def fetch_recipe(beverage: BeverageId, profile: int = 1) -> Optional[list[tuple[int, int]]]:
    """Fetch saved recipe for a beverage from the machine."""
    cmd = cmd_recipe_read(profile, beverage)
    response = await machine.send_and_wait(cmd, "recipe", timeout=3.0)
    if response and "ingredients" in response:
        return response["ingredients"]
    return None


async def fetch_all_recipes(profile: int = 1):
    """Fetch recipes for all Dinamica Plus beverages and cache them."""
    global recipe_cache
    logger.info(f"Fetching recipes for profile {profile}...")
    fetched = 0

    for bev in DINAMICA_BEVERAGES:
        # Skip steam/hot_water — they don't have editable recipes
        if bev in (BeverageId.STEAM, BeverageId.HOT_WATER):
            continue

        try:
            ingredients = await fetch_recipe(bev, profile)
            if ingredients:
                recipe_cache[bev.name.lower()] = ingredients
                readable = recipe_to_dict(ingredients)
                logger.info(f"  {bev.name}: {readable}")
                fetched += 1
            else:
                logger.warning(f"  {bev.name}: no response")
        except Exception as e:
            logger.warning(f"  {bev.name}: error {e}")

        # Small delay between requests to not overwhelm BLE
        await asyncio.sleep(0.3)

    logger.info(f"Fetched {fetched}/{len(DINAMICA_BEVERAGES) - 2} recipes")


def get_recipe_display(bev_name: str) -> Optional[dict]:
    """Get a human-readable recipe summary for display in the UI."""
    ingredients = recipe_cache.get(bev_name)
    if not ingredients:
        return None

    readable = recipe_to_dict(ingredients)
    display = {}

    if "coffee" in readable:
        display["coffee_ml"] = readable["coffee"]
    if "milk" in readable:
        display["milk_seconds"] = readable["milk"]
    if "hot_water" in readable:
        display["hot_water_ml"] = readable["hot_water"]
    if "taste" in readable:
        try:
            display["taste"] = Aroma(readable["taste"]).name.replace("_", " ").title()
        except ValueError:
            display["taste"] = str(readable["taste"])
    if "temperature" in readable:
        try:
            display["temperature"] = Temperature(readable["temperature"]).name.replace("_", " ").title()
        except ValueError:
            display["temperature"] = str(readable["temperature"])

    return display


# ── HTTP Handlers ─────────────────────────────────────────────────────────────

def json_response(data: dict, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data, indent=2),
        content_type="application/json",
        status=status,
        headers={"Access-Control-Allow-Origin": CORS_ORIGIN},
    )


def _require_connected() -> Optional[web.Response]:
    """Return a 503 response if not connected, None if OK."""
    if not machine.connected:
        return json_response({"error": "Not connected", "connected": False}, 503)
    return None


async def _parse_body(request: web.Request) -> dict:
    """Safely parse JSON body, defaulting to empty dict on missing/malformed input."""
    try:
        return await request.json()
    except Exception as e:
        logger.debug(f"Could not parse JSON body: {e}")
        return {}


def _normalize_beverage_name(name: str) -> str:
    """Normalize a beverage name for lookup."""
    return name.lower().replace(" ", "_")


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
        "profile": current_profile,
        "recipes_loaded": len(recipe_cache),
        "endpoints": {
            "GET /":                "Web UI",
            "GET /api":             "API info",
            "GET /api/status":      "Machine status",
            "POST /api/power":      "Turn machine on",
            "POST /api/brew":       "Brew a beverage (uses saved profile recipe)",
            "POST /api/brew/stop":  "Stop current brew",
            "POST /api/steam":      "Start steam",
            "POST /api/hot-water":  "Dispense hot water",
            "POST /api/profile":    "Select profile (re-fetches recipes)",
            "GET /api/beverages":   "List available beverages with recipes",
            "GET /api/recipes":     "All cached recipes",
            "POST /api/recipes/refresh": "Re-fetch all recipes from machine",
            "GET /api/scan":        "Scan for machines",
            "POST /api/reconnect":  "Force reconnect",
        },
    })


async def handle_status(request: web.Request) -> web.Response:
    """GET /api/status — Get machine status."""
    base = {
        "connected": machine.connected,
        "uptime_seconds": round(time.time() - START_TIME),
        "status_age_seconds": round(machine.get_status_age(), 1),
        "profile": current_profile,
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
    if err := _require_connected():
        return err

    ok = await machine.send(cmd_power_on())
    return json_response({"success": ok, "action": "power_on"})


async def handle_brew(request: web.Request) -> web.Response:
    """POST /api/brew — Brew a beverage using saved profile recipe."""
    if err := _require_connected():
        return err

    body = await _parse_body(request)
    bev_name = _normalize_beverage_name(body.get("beverage", "coffee"))

    # Resolve beverage ID
    names = get_beverage_names()
    if bev_name in names:
        bev_id = BeverageId(names[bev_name])
    elif bev_name.isdigit():
        bev_id = BeverageId(int(bev_name))
    else:
        return json_response({
            "error": f"Unknown beverage: {bev_name}",
            "available": list(names.keys()),
        }, 400)

    # Prefer saved recipe from machine profile
    cached_recipe = recipe_cache.get(bev_name)
    if cached_recipe:
        cmd = cmd_brew_recipe(bev_id, cached_recipe)
        ok = await machine.send(cmd)

        display = get_recipe_display(bev_name)
        return json_response({
            "success": ok,
            "action": "brew",
            "beverage": bev_id.name,
            "method": "profile",
            "profile": current_profile,
            **(display or {}),
        })

    # Fallback to verified commands
    verified = cmd_brew_verified(bev_name)
    if verified:
        ok = await machine.send(verified)
        return json_response({
            "success": ok,
            "action": "brew",
            "beverage": bev_name,
            "method": "verified",
        })

    # Final fallback to computed command with defaults
    defaults = BEVERAGE_DEFAULTS.get(bev_id, {"quantity_ml": 100, "aroma": Aroma.NORMAL})
    quantity = body.get("quantity_ml", defaults["quantity_ml"])
    try:
        quantity = int(quantity)
        if not (0 <= quantity <= 500):
            return json_response({"error": "quantity_ml must be between 0 and 500"}, 400)
    except (TypeError, ValueError):
        return json_response({"error": "quantity_ml must be an integer"}, 400)
    try:
        aroma = Aroma(body.get("aroma", defaults["aroma"]))
    except (ValueError, KeyError):
        return json_response({"error": f"Invalid aroma value. Valid values: {[e.value for e in Aroma]}"}, 400)
    try:
        temp = Temperature(body.get("temperature", Temperature.HIGH))
    except (ValueError, KeyError):
        return json_response({"error": f"Invalid temperature value. Valid values: {[e.value for e in Temperature]}"}, 400)

    cmd = cmd_brew(bev_id, quantity_ml=quantity, aroma=aroma, temperature=temp)
    ok = await machine.send(cmd)

    return json_response({
        "success": ok,
        "action": "brew",
        "beverage": bev_id.name,
        "quantity_ml": quantity,
        "aroma": aroma.name,
        "temperature": temp.name,
        "method": "fallback",
    })


async def handle_brew_stop(request: web.Request) -> web.Response:
    """POST /api/brew/stop — Stop current brew."""
    if err := _require_connected():
        return err

    body = await _parse_body(request)
    bev_name = _normalize_beverage_name(body.get("beverage", "coffee"))

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
    if err := _require_connected():
        return err
    ok = await machine.send(cmd_steam())
    return json_response({"success": ok, "action": "steam"})


async def handle_hot_water(request: web.Request) -> web.Response:
    """POST /api/hot-water — Dispense hot water."""
    if err := _require_connected():
        return err
    body = await _parse_body(request)
    quantity = body.get("quantity_ml", 250)
    try:
        quantity = int(quantity)
        if not (0 <= quantity <= 500):
            return json_response({"error": "quantity_ml must be between 0 and 500"}, 400)
    except (TypeError, ValueError):
        return json_response({"error": "quantity_ml must be an integer"}, 400)
    ok = await machine.send(cmd_hot_water(quantity))
    return json_response({"success": ok, "action": "hot_water", "quantity_ml": quantity})


async def handle_profile(request: web.Request) -> web.Response:
    """POST /api/profile — Select profile and re-fetch recipes."""
    if err := _require_connected():
        return err

    global current_profile
    body = await _parse_body(request)
    profile_id = body.get("profile_id", 1)
    try:
        profile_id = int(profile_id)
        if not (1 <= profile_id <= 4):
            return json_response({"error": "profile_id must be between 1 and 4"}, 400)
    except (TypeError, ValueError):
        return json_response({"error": "profile_id must be an integer"}, 400)

    ok = await machine.send(cmd_profile_select(profile_id))
    if ok:
        current_profile = profile_id
        # Re-fetch all recipes for the new profile
        await fetch_all_recipes(profile_id)

    return json_response({
        "success": ok,
        "action": "profile_select",
        "profile_id": profile_id,
        "recipes_loaded": len(recipe_cache),
    })


async def handle_beverages(request: web.Request) -> web.Response:
    """GET /api/beverages — List available beverages with recipe details."""
    beverages = []

    for bev in DINAMICA_BEVERAGES:
        name = bev.name.lower()
        entry = {
            "name": name,
            "id": bev.value,
            "verified": name in VERIFIED_COMMANDS,
            "has_recipe": name in recipe_cache,
        }

        # Add recipe display info
        display = get_recipe_display(name)
        if display:
            entry["recipe"] = display

        # Fallback defaults if no recipe
        if not display and bev in BEVERAGE_DEFAULTS:
            defaults = BEVERAGE_DEFAULTS[bev]
            entry["defaults"] = {
                "coffee_ml": defaults.get("quantity_ml"),
            }

        beverages.append(entry)

    return json_response({
        "beverages": beverages,
        "profile": current_profile,
    })


async def handle_recipes(request: web.Request) -> web.Response:
    """GET /api/recipes — All cached recipes."""
    recipes = {}
    for bev_name, ingredients in recipe_cache.items():
        recipes[bev_name] = {
            "ingredients": recipe_to_dict(ingredients),
            "display": get_recipe_display(bev_name),
        }
    return json_response({
        "profile": current_profile,
        "recipes": recipes,
    })


async def handle_recipes_refresh(request: web.Request) -> web.Response:
    """POST /api/recipes/refresh — Re-fetch all recipes from the machine."""
    if err := _require_connected():
        return err

    await fetch_all_recipes(current_profile)
    return json_response({
        "success": True,
        "profile": current_profile,
        "recipes_loaded": len(recipe_cache),
    })


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
        if ok:
            # Re-fetch recipes after reconnecting
            await fetch_all_recipes(current_profile)
        return json_response({"success": ok, "connected": machine.connected})
    return json_response({"error": "No machine address configured"}, 400)


async def handle_cors(request: web.Request) -> web.Response:
    """Handle CORS preflight."""
    return web.Response(
        status=200,
        headers={
            "Access-Control-Allow-Origin": CORS_ORIGIN,
            "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
    )


# ── Security Middleware ────────────────────────────────────────────────────────

@web.middleware
async def security_headers_middleware(request: web.Request, handler):
    """Add security headers to every response."""
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'",
    )
    return response


# ── Server Setup ──────────────────────────────────────────────────────────────

def create_app() -> web.Application:
    app = web.Application(middlewares=[security_headers_middleware])

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
    app.router.add_get("/api/recipes", handle_recipes)
    app.router.add_post("/api/recipes/refresh", handle_recipes_refresh)
    app.router.add_get("/api/scan", handle_scan)
    app.router.add_post("/api/reconnect", handle_reconnect)

    # CORS
    app.router.add_route("OPTIONS", "/{path:.*}", handle_cors)

    # Scheduler
    setup_scheduler(app, machine, fetch_recipe, fetch_all_recipes)

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


async def cmd_serve(address: str, port: int = 8080, bind_host: str = "127.0.0.1"):
    """Connect to machine and start HTTP server."""
    global MACHINE_ADDRESS, BIND_HOST, CORS_ORIGIN
    MACHINE_ADDRESS = address
    BIND_HOST = bind_host
    CORS_ORIGIN = f"http://localhost:{port}"

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

        # Fetch all saved recipes from the machine
        print("\nLoading beverage recipes from machine...")
        await fetch_all_recipes(current_profile)

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
    print(f"  Web UI:  http://{BIND_HOST}:{port}")
    print(f"  API:     http://{BIND_HOST}:{port}/api")
    print(f"  Profile: {current_profile} ({len(recipe_cache)} recipes loaded)")
    if BIND_HOST != "127.0.0.1":
        print(f"  WARNING: Server is bound to {BIND_HOST} (accessible on network)")
    print(f"{'='*60}")
    print(f"\n  Press Ctrl+C to stop\n")

    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, BIND_HOST, port)
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


# ── Main (delegates to cli.py for direct invocation) ─────────────────────────

def main():
    """Legacy entry point — delegates to barista.cli."""
    from barista.cli import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
