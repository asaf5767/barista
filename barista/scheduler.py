"""
Barista Scheduler — Automated brew scheduling with persistence.

Runs inside the aiohttp server process. Uses the existing BLE connection
to wake the machine and brew drinks at a scheduled time.

Architecture:
  - Schedules persisted to auto_brew_schedules.json (next to this file)
  - 30-second check loop: compares now >= trigger_time (drift-proof, restart-safe)
  - Brew execution engine: power on → wait for ready → prep time → brew sequence
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Callable, Any

from aiohttp import web

from barista.protocol import (
    BeverageId,
    MachineState,
    Alarm,
    DINAMICA_BEVERAGES,
    cmd_power_on,
    cmd_profile_select,
    cmd_recipe_read,
    cmd_brew_recipe,
    cmd_brew_verified,
    cmd_monitor,
    get_beverage_names,
    recipe_to_dict,
)

# ── Logging ────────────────────────────────────────────────────────────────────

logger = logging.getLogger("delonghi.scheduler")

_brew_logger: Optional[logging.Logger] = None


def _get_brew_logger() -> logging.Logger:
    """Get or create a dedicated logger for brew execution (file + console)."""
    global _brew_logger
    if _brew_logger:
        return _brew_logger

    _brew_logger = logging.getLogger("delonghi.scheduler.brew")
    _brew_logger.setLevel(logging.DEBUG)

    # File handler — rotates at 1MB, keeps 3 backups
    log_path = Path(__file__).parent / "auto_brew.log"
    fh = RotatingFileHandler(str(log_path), maxBytes=1_000_000, backupCount=3)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    _brew_logger.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s [BREW] %(message)s"))
    _brew_logger.addHandler(ch)

    return _brew_logger


# ── Persistence ────────────────────────────────────────────────────────────────

SCHEDULES_FILE = Path(__file__).parent / "auto_brew_schedules.json"

# Critical alarms that should abort a brew
CRITICAL_ALARMS = {
    Alarm.EMPTY_WATER_TANK,
    Alarm.WASTE_CONTAINER_FULL,
    Alarm.BEANS_EMPTY,
    Alarm.BEANS_EMPTY_2,
    Alarm.MACHINE_TO_SERVICE,
    Alarm.HEATER_PROBE_FAIL,
    Alarm.INFUSER_MOTOR_FAIL,
    Alarm.STEAMER_PROBE_FAIL,
    Alarm.HYDRAULIC_PROBLEM,
}


def _load_schedules() -> list[dict]:
    """Load schedules from disk."""
    if SCHEDULES_FILE.exists():
        try:
            data = json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to load schedules: {e}")
    return []


def _save_schedules(schedules: list[dict]) -> None:
    """Persist schedules to disk."""
    try:
        SCHEDULES_FILE.write_text(
            json.dumps(schedules, indent=2, default=str),
            encoding="utf-8",
        )
    except OSError as e:
        logger.error(f"Failed to save schedules: {e}")


# ── Scheduler State ───────────────────────────────────────────────────────────

class SchedulerState:
    """Holds all scheduler runtime state. Passed around instead of globals."""

    def __init__(self):
        self.schedules: list[dict] = []
        self.active_task: Optional[asyncio.Task] = None
        self.active_schedule_id: Optional[str] = None
        self.execution_status: dict = {}  # Live status for UI polling
        self.machine = None  # Set by setup_scheduler
        self.fetch_recipe_fn: Optional[Callable] = None  # Set by setup_scheduler
        self.fetch_all_recipes_fn: Optional[Callable] = None
        self._check_loop_task: Optional[asyncio.Task] = None

    def load(self):
        self.schedules = _load_schedules()
        logger.info(f"Loaded {len(self.schedules)} schedule(s)")

    def save(self):
        _save_schedules(self.schedules)

    def find_schedule(self, schedule_id: str) -> Optional[dict]:
        for s in self.schedules:
            if s.get("id") == schedule_id:
                return s
        return None


state = SchedulerState()


# ── Check Loop ─────────────────────────────────────────────────────────────────

async def _scheduler_check_loop():
    """Runs every 10 seconds. Checks if any schedule should fire."""
    blog = _get_brew_logger()
    cleanup_counter = 0

    while True:
        try:
            now = datetime.now()
            for schedule in state.schedules:
                if schedule.get("status") in ("running", "completed", "cancelled", "error"):
                    continue
                if not schedule.get("enabled", True):
                    continue

                trigger_dt = _parse_trigger(schedule.get("trigger", {}))
                if trigger_dt and now >= trigger_dt:
                    # Time to fire!
                    blog.info(f"Schedule '{schedule['id']}' triggered at {now}")
                    schedule["status"] = "running"
                    state.save()
                    state.active_schedule_id = schedule["id"]
                    state.active_task = asyncio.create_task(
                        _execute_brew_sequence(schedule)
                    )

            # Auto-cleanup old completed/error/cancelled schedules every ~5 min
            cleanup_counter += 1
            if cleanup_counter >= 30:  # 30 * 10s = 5 min
                cleanup_counter = 0
                _auto_cleanup_schedules()

        except Exception as e:
            logger.error(f"Scheduler check error: {e}")

        await asyncio.sleep(10)


def _auto_cleanup_schedules():
    """Remove completed/error/cancelled schedules older than 24 hours."""
    cutoff = datetime.now() - timedelta(hours=24)
    original_count = len(state.schedules)
    state.schedules = [
        s for s in state.schedules
        if not (
            s.get("status") in ("completed", "error", "cancelled")
            and _parse_datetime(s.get("completed_at") or s.get("created_at", "")) is not None
            and _parse_datetime(s.get("completed_at") or s.get("created_at", "")) < cutoff
        )
    ]
    removed = original_count - len(state.schedules)
    if removed > 0:
        state.save()
        logger.info(f"Auto-cleaned {removed} old schedule(s)")


def _parse_datetime(dt_str: str) -> Optional[datetime]:
    """Parse ISO datetime string, returning None on failure."""
    try:
        return datetime.fromisoformat(dt_str)
    except (ValueError, TypeError):
        return None


def _parse_trigger(trigger: dict) -> Optional[datetime]:
    """Parse trigger dict to a datetime."""
    date_str = trigger.get("date")
    time_str = trigger.get("time")
    if not date_str or not time_str:
        return None
    try:
        return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None


# ── Brew Execution Engine ──────────────────────────────────────────────────────

async def _execute_brew_sequence(schedule: dict):
    """Execute a full brew sequence: power on → prep → brew drinks."""
    blog = _get_brew_logger()
    machine = state.machine
    drinks = schedule.get("drinks", [])
    timing = schedule.get("timing", {})
    profile = schedule.get("profile", 1)
    prep_minutes = timing.get("prep_minutes", 5)
    between_minutes = timing.get("between_drinks_minutes", 3.5)
    dry_run = schedule.get("dry_run", False)

    def update_status(step: str, detail: str = "", progress: float = 0):
        state.execution_status = {
            "schedule_id": schedule["id"],
            "step": step,
            "detail": detail,
            "progress": progress,
            "timestamp": datetime.now().isoformat(),
        }
        blog.info(f"[{step}] {detail}")

    try:
        # ── Step 1: Power on ──────────────────────────────────────────────
        update_status("powering_on", "Sending power-on command", 0.05)

        if not dry_run:
            # Retry BLE connection up to 3 times
            connected = machine.connected
            if not connected:
                for attempt in range(1, 4):
                    update_status("connecting", f"BLE connect attempt {attempt}/3", 0.02)
                    blog.info(f"BLE not connected, attempt {attempt}/3...")
                    connected = await machine.connect(machine._address)
                    if connected:
                        break
                    await asyncio.sleep(10)

                if not connected:
                    update_status("error", "Failed to connect to machine after 3 attempts")
                    schedule["status"] = "error"
                    schedule["error"] = "BLE connection failed"
                    state.save()
                    return

            # Check if machine is already on (READY or TURNING_ON)
            status = machine.get_last_status()
            machine_state = status.get("state", "") if status else ""

            if machine_state == "READY":
                blog.info("Machine is already READY — skipping power-on")
            elif machine_state == "TURNING_ON":
                blog.info("Machine is already turning on — skipping power-on")
            else:
                ok = await machine.send(cmd_power_on())
                if not ok:
                    update_status("error", "Failed to send power-on command")
                    schedule["status"] = "error"
                    schedule["error"] = "Power-on send failed"
                    state.save()
                    return
        else:
            blog.info("[DRY RUN] Would send power-on command")

        # ── Step 2: Wait for READY ────────────────────────────────────────
        update_status("heating", "Waiting for machine to reach READY state", 0.10)

        if not dry_run:
            ready = await _wait_for_ready(machine, timeout=180, blog=blog)
            if not ready:
                update_status("error", "Machine did not reach READY state within 3 minutes")
                schedule["status"] = "error"
                schedule["error"] = "Machine not ready (timeout)"
                state.save()
                return

            # Check for critical alarms
            alarm_check = _check_alarms(machine)
            if alarm_check:
                update_status("error", f"Critical alarm: {alarm_check}")
                schedule["status"] = "error"
                schedule["error"] = f"Alarm: {alarm_check}"
                state.save()
                return
        else:
            blog.info("[DRY RUN] Would wait for READY state")

        # ── Step 3: Prep time ─────────────────────────────────────────────
        prep_seconds = int(prep_minutes * 60)
        update_status("prepping", f"Prep time: {prep_minutes} min — prepare the milk frother!", 0.20)

        for elapsed in range(0, prep_seconds, 10):
            remaining = prep_seconds - elapsed
            mins = remaining // 60
            secs = remaining % 60
            progress = 0.20 + (elapsed / prep_seconds) * 0.15
            update_status("prepping", f"Prep time remaining: {mins}m {secs}s", progress)
            await asyncio.sleep(min(10, remaining))

        # ── Step 4: Select profile ────────────────────────────────────────
        update_status("profile", f"Selecting profile {profile}", 0.35)

        if not dry_run:
            await machine.send(cmd_profile_select(profile))
            await asyncio.sleep(1)

            # Fetch recipes for this profile
            if state.fetch_all_recipes_fn:
                await state.fetch_all_recipes_fn(profile)
        else:
            blog.info(f"[DRY RUN] Would select profile {profile}")

        # ── Step 5: Brew each drink ───────────────────────────────────────
        total_drinks = len(drinks)
        for i, drink in enumerate(drinks):
            drink_num = i + 1
            bev_name = drink.get("beverage", "coffee")
            bev_label = drink.get("label", bev_name)
            drink_progress_base = 0.40 + (i / total_drinks) * 0.55

            update_status(
                "brewing",
                f"Brewing {bev_label} ({drink_num}/{total_drinks})",
                drink_progress_base,
            )

            if not dry_run:
                # Resolve beverage ID
                names = get_beverage_names()
                if bev_name not in names:
                    blog.warning(f"Unknown beverage: {bev_name}, skipping")
                    continue
                bev_id = BeverageId(names[bev_name])

                # Strategy: use cached recipe first (from fetch_all_recipes),
                # then try BLE fetch, then fall back to verified commands.
                # This avoids BLE contention that causes "In Progress" errors.
                recipe = None

                # 1. Check server's recipe cache (already populated in Step 4)
                from barista.server import recipe_cache
                cached = recipe_cache.get(bev_name)
                if cached:
                    recipe = cached
                    blog.info(f"Using cached recipe for {bev_label}")

                # 2. Try BLE fetch if no cache
                if not recipe and state.fetch_recipe_fn:
                    try:
                        recipe = await state.fetch_recipe_fn(bev_id, profile)
                    except Exception as e:
                        blog.warning(f"BLE recipe fetch failed for {bev_label}: {e}")

                if recipe:
                    cmd = cmd_brew_recipe(bev_id, recipe)
                    # Retry brew command up to 3 times — BLE can be flaky
                    brew_sent = False
                    for attempt in range(1, 4):
                        ok = await machine.send(cmd)
                        if ok:
                            blog.info(f"Brew command sent for {bev_label} (recipe, attempt {attempt})")
                            brew_sent = True
                            break
                        blog.warning(f"Brew send failed for {bev_label}, attempt {attempt}/3")
                        await asyncio.sleep(3)

                    if not brew_sent:
                        blog.error(f"Failed to send brew after 3 attempts for {bev_label}")
                        continue
                else:
                    # 3. Fallback: use verified/hardcoded command
                    verified = cmd_brew_verified(bev_name)
                    if verified:
                        blog.info(f"No recipe for {bev_label}, using verified command")
                        brew_sent = False
                        for attempt in range(1, 4):
                            ok = await machine.send(verified)
                            if ok:
                                blog.info(f"Verified brew sent for {bev_label} (attempt {attempt})")
                                brew_sent = True
                                break
                            await asyncio.sleep(3)
                        if not brew_sent:
                            blog.error(f"Failed to send verified brew for {bev_label}")
                            continue
                    else:
                        blog.warning(f"No recipe or verified command for {bev_label}, skipping")
                        continue

                # Wait for machine to leave READY (confirms brew started)
                update_status(
                    "brewing",
                    f"Waiting for {bev_label} to start...",
                    drink_progress_base + 0.05,
                )
                brew_started = await _wait_for_state_change(
                    machine, from_state="READY", timeout=30, blog=blog
                )
                if not brew_started:
                    blog.warning(f"{bev_label} didn't start — machine stayed READY. Retrying send...")
                    # One more attempt
                    ok = await machine.send(cmd if recipe else verified)
                    if ok:
                        brew_started = await _wait_for_state_change(
                            machine, from_state="READY", timeout=30, blog=blog
                        )
                    if not brew_started:
                        blog.error(f"{bev_label} brew never started — skipping")
                        continue

                # Now wait for brew to complete (machine returns to READY)
                update_status(
                    "brewing",
                    f"Brewing {bev_label}...",
                    drink_progress_base + 0.10,
                )
                brew_done = await _wait_for_ready(machine, timeout=300, blog=blog)
                if not brew_done:
                    blog.warning(f"{bev_label} didn't complete within 5 min timeout")

                # Check alarms after brew
                alarm_check = _check_alarms(machine)
                if alarm_check:
                    blog.error(f"Alarm after brewing {bev_label}: {alarm_check}")
                    update_status("error", f"Alarm during brew: {alarm_check}")
                    # Continue to next drink — don't abort entire sequence
            else:
                blog.info(f"[DRY RUN] Would brew {bev_label}")

            # Wait between drinks (skip after last)
            if drink_num < total_drinks:
                wait_seconds = int(between_minutes * 60)
                update_status(
                    "waiting",
                    f"Waiting {between_minutes} min before next drink...",
                    drink_progress_base + 0.20,
                )
                for elapsed in range(0, wait_seconds, 10):
                    remaining = wait_seconds - elapsed
                    mins = remaining // 60
                    secs = remaining % 60
                    update_status(
                        "waiting",
                        f"Next drink in {mins}m {secs}s",
                        drink_progress_base + 0.20 + (elapsed / wait_seconds) * 0.05,
                    )
                    await asyncio.sleep(min(10, remaining))

        # ── Done ──────────────────────────────────────────────────────────
        update_status("completed", "All drinks brewed successfully! ☕", 1.0)
        schedule["status"] = "completed"
        schedule["completed_at"] = datetime.now().isoformat()
        state.save()
        blog.info("Brew sequence completed successfully")

    except asyncio.CancelledError:
        blog.warning("Brew sequence cancelled")
        schedule["status"] = "cancelled"
        state.save()
        raise
    except Exception as e:
        blog.error(f"Brew sequence error: {e}", exc_info=True)
        schedule["status"] = "error"
        schedule["error"] = str(e)
        state.save()
    finally:
        state.active_schedule_id = None
        state.active_task = None
        # Keep execution_status for 60s so UI can show final state,
        # then clear it on next poll
        if state.execution_status.get("step") in ("completed", "error"):
            asyncio.get_running_loop().call_later(
                60, lambda: state.execution_status.clear()
            )


async def _wait_for_ready(machine, timeout: int = 180, blog=None) -> bool:
    """Poll machine status until READY state, with timeout.

    Checks for state == READY rather than is_ready, since is_ready also
    requires zero alarms — and the ECAM often has persistent non-critical
    alarms like DESCALE_ALARM that don't prevent brewing.

    Uses cached status when available to avoid blocking BLE writes.
    """
    if blog is None:
        blog = _get_brew_logger()

    start = time.time()
    while time.time() - start < timeout:
        try:
            # Prefer cached status to avoid blocking BLE with Write Requests
            status = machine.get_last_status()
            if not status or machine.get_status_age() > 15:
                # Cache is stale — request fresh status
                status = await machine.request_status()

            if status:
                machine_state = status.get("state", "")
                is_ready = status.get("is_ready", False)
                alarms = status.get("alarms", [])

                # Check for critical alarms that should abort
                critical = [a for a in alarms if a in {
                    "EMPTY_WATER_TANK", "WASTE_CONTAINER_FULL",
                    "BEANS_EMPTY", "BEANS_EMPTY_2",
                }]
                if critical:
                    blog.error(f"Critical alarm: {critical}")
                    return False

                blog.debug(f"Machine state: {machine_state}, ready: {is_ready}, alarms: {alarms}")

                # Accept READY state even with non-critical alarms (e.g. DESCALE)
                if machine_state == "READY":
                    blog.info(f"Machine is READY! (alarms: {alarms or 'none'})")
                    return True

        except Exception as e:
            blog.warning(f"Status poll error: {e}")

        await asyncio.sleep(5)

    return False


async def _wait_for_state_change(machine, from_state: str, timeout: int = 30, blog=None) -> bool:
    """Wait for the machine to leave a specific state (e.g. READY → BREWING).

    Returns True if the machine transitioned away from from_state within timeout.
    Used to confirm a brew command was actually received by the machine.
    """
    if blog is None:
        blog = _get_brew_logger()

    start = time.time()
    while time.time() - start < timeout:
        status = machine.get_last_status()
        if status:
            current = status.get("state", "")
            if current != from_state:
                blog.info(f"Machine state changed: {from_state} → {current}")
                return True
        await asyncio.sleep(2)

    return False


def _check_alarms(machine) -> Optional[str]:
    """Check last known status for critical alarms. Returns alarm name or None."""
    status = machine.get_last_status()
    if not status:
        return None

    alarms = status.get("alarms", [])
    for alarm_name in alarms:
        try:
            alarm = Alarm[alarm_name]
            if alarm in CRITICAL_ALARMS:
                return alarm_name
        except (KeyError, ValueError):
            pass

    return None


# ── HTTP Handlers ──────────────────────────────────────────────────────────────

def _json_response(data: dict, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data, indent=2, default=str),
        content_type="application/json",
        status=status,
        headers={"Access-Control-Allow-Origin": "*"},
    )


async def handle_schedule_list(request: web.Request) -> web.Response:
    """GET /api/schedule — List all schedules."""
    schedules = []
    now = datetime.now()

    for s in state.schedules:
        entry = {**s}
        # Add countdown for pending schedules
        trigger_dt = _parse_trigger(s.get("trigger", {}))
        if trigger_dt and s.get("status") not in ("completed", "cancelled", "error"):
            delta = trigger_dt - now
            entry["countdown_seconds"] = max(0, int(delta.total_seconds()))
        schedules.append(entry)

    return _json_response({"schedules": schedules})


async def handle_schedule_create(request: web.Request) -> web.Response:
    """POST /api/schedule — Create a new scheduled brew."""
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON body"}, 400)

    # Validate required fields
    trigger = body.get("trigger", {})
    drinks = body.get("drinks", [])

    if not trigger.get("date") or not trigger.get("time"):
        return _json_response({"error": "trigger.date and trigger.time are required"}, 400)

    if not drinks:
        return _json_response({"error": "At least one drink is required"}, 400)

    # Validate trigger datetime
    trigger_dt = _parse_trigger(trigger)
    if not trigger_dt:
        return _json_response({"error": "Invalid trigger date/time format (use YYYY-MM-DD and HH:MM)"}, 400)

    # Create schedule
    schedule_id = f"brew_{trigger['date'].replace('-', '')}_{trigger['time'].replace(':', '')}"
    # Ensure unique ID
    existing_ids = {s["id"] for s in state.schedules}
    if schedule_id in existing_ids:
        schedule_id += f"_{uuid.uuid4().hex[:4]}"

    schedule = {
        "id": schedule_id,
        "enabled": True,
        "status": "pending",
        "trigger": trigger,
        "drinks": drinks,
        "timing": body.get("timing", {"prep_minutes": 5, "between_drinks_minutes": 3.5}),
        "profile": body.get("profile", 1),
        "created_at": datetime.now().isoformat(),
    }

    state.schedules.append(schedule)
    state.save()

    logger.info(f"Created schedule '{schedule_id}' for {trigger['date']} {trigger['time']}")

    return _json_response({
        "success": True,
        "schedule": schedule,
    }, 201)


async def handle_schedule_delete(request: web.Request) -> web.Response:
    """DELETE /api/schedule/{id} — Delete/cancel a schedule."""
    schedule_id = request.match_info.get("id")
    schedule = state.find_schedule(schedule_id)

    if not schedule:
        return _json_response({"error": f"Schedule '{schedule_id}' not found"}, 404)

    # If it's currently running, cancel the task
    if state.active_schedule_id == schedule_id and state.active_task:
        state.active_task.cancel()
        try:
            await state.active_task
        except asyncio.CancelledError:
            pass

    state.schedules = [s for s in state.schedules if s["id"] != schedule_id]
    state.save()

    logger.info(f"Deleted schedule '{schedule_id}'")
    return _json_response({"success": True, "deleted": schedule_id})


async def handle_schedule_test(request: web.Request) -> web.Response:
    """POST /api/schedule/{id}/test — Dry-run a schedule."""
    schedule_id = request.match_info.get("id")
    schedule = state.find_schedule(schedule_id)

    if not schedule:
        return _json_response({"error": f"Schedule '{schedule_id}' not found"}, 404)

    if state.active_task and not state.active_task.done():
        return _json_response({"error": "A brew is already in progress"}, 409)

    # Create a dry-run copy
    test_schedule = {**schedule, "dry_run": True, "status": "running"}
    state.active_schedule_id = schedule_id
    state.active_task = asyncio.create_task(_execute_brew_sequence(test_schedule))

    return _json_response({"success": True, "message": "Dry-run started", "schedule_id": schedule_id})


async def handle_schedule_active(request: web.Request) -> web.Response:
    """GET /api/schedule/active — Get status of currently executing brew."""
    if not state.execution_status:
        return _json_response({
            "active": False,
            "message": "No brew in progress",
        })

    return _json_response({
        "active": state.active_task is not None and not state.active_task.done(),
        **state.execution_status,
    })


async def handle_schedule_cancel_active(request: web.Request) -> web.Response:
    """POST /api/schedule/active/cancel — Cancel the currently running brew."""
    if not state.active_task or state.active_task.done():
        return _json_response({"error": "No active brew to cancel"}, 404)

    state.active_task.cancel()
    return _json_response({"success": True, "message": "Cancelling active brew..."})


async def handle_schedule_clear(request: web.Request) -> web.Response:
    """POST /api/schedule/clear — Remove all completed/error/cancelled schedules."""
    original = len(state.schedules)
    state.schedules = [
        s for s in state.schedules
        if s.get("status") not in ("completed", "error", "cancelled")
    ]
    removed = original - len(state.schedules)
    state.save()

    # Also clear stale execution_status if it references a removed schedule
    if state.execution_status:
        sid = state.execution_status.get("schedule_id")
        if sid and not state.find_schedule(sid):
            state.execution_status = {}

    logger.info(f"Cleared {removed} finished schedule(s)")
    return _json_response({"success": True, "removed": removed})


# ── Setup ──────────────────────────────────────────────────────────────────────

def setup_scheduler(app: web.Application, machine, fetch_recipe_fn, fetch_all_recipes_fn):
    """Initialize the scheduler and register routes.

    Args:
        app: The aiohttp application
        machine: The DelonghiBLE instance
        fetch_recipe_fn: async function(BeverageId, profile) -> recipe
        fetch_all_recipes_fn: async function(profile) -> None
    """
    state.machine = machine
    state.fetch_recipe_fn = fetch_recipe_fn
    state.fetch_all_recipes_fn = fetch_all_recipes_fn
    state.load()

    # Register routes
    app.router.add_get("/api/schedule", handle_schedule_list)
    app.router.add_post("/api/schedule", handle_schedule_create)
    app.router.add_post("/api/schedule/clear", handle_schedule_clear)
    app.router.add_delete("/api/schedule/{id}", handle_schedule_delete)
    app.router.add_post("/api/schedule/{id}/test", handle_schedule_test)
    app.router.add_get("/api/schedule/active", handle_schedule_active)
    app.router.add_post("/api/schedule/active/cancel", handle_schedule_cancel_active)

    # Start the check loop on app startup
    async def start_scheduler(app):
        state._check_loop_task = asyncio.create_task(_scheduler_check_loop())
        logger.info("Scheduler check loop started (10s interval)")

    async def stop_scheduler(app):
        if state._check_loop_task:
            state._check_loop_task.cancel()
            try:
                await state._check_loop_task
            except asyncio.CancelledError:
                pass
        if state.active_task:
            state.active_task.cancel()
            try:
                await state.active_task
            except asyncio.CancelledError:
                pass
        logger.info("Scheduler stopped")

    app.on_startup.append(start_scheduler)
    app.on_cleanup.append(stop_scheduler)
