"""
ActionCues — Live Link Face Remote Control
Main Server — github.com/jeremic-b/ActionCues

DESIGN PRINCIPLES:
  1. Only two device states: "idle" or "recording" — no connection status
  2. Commands ALWAYS fire to ALL confirmed devices, unconditionally
  3. Recording state only changes on phone confirmation (RecordStartConfirm/StopConfirm)
  4. Poll loop keeps phones alive during long sessions via BatteryQuery
  5. /Slate and /RecordStart both get slate_actorname for unique filenames per device
"""

import asyncio
import json
import logging
import os
import socket
import threading
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from device_manager import DeviceManager
from discovery import ZeroconfDiscovery
from osc_engine import OSCEngine
from session import SessionManager


# ══════════════════════════════════════════════════════════════════
# Logging
# ══════════════════════════════════════════════════════════════════

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "server.log"), logging.StreamHandler()],
)
logger = logging.getLogger("server")


# ══════════════════════════════════════════════════════════════════
# Global state
# ══════════════════════════════════════════════════════════════════

device_mgr = DeviceManager()
session_mgr = SessionManager()
osc: Optional[OSCEngine] = None
ws_clients: set = set()                      # active WebSocket connections
start_time: float = time.time()
_poll_task: Optional[asyncio.Task] = None
_discovery: Optional[ZeroconfDiscovery] = None
_recently_seen: dict = {}                    # IP -> timestamp, rate-limiting discovery
_recently_seen_lock = threading.Lock()       # protects _recently_seen from OSC threads
_main_loop: Optional[asyncio.AbstractEventLoop] = None
_last_poll_time: float = 0.0


# ══════════════════════════════════════════════════════════════════
# Utilities
# ══════════════════════════════════════════════════════════════════

def get_server_ip() -> str:
    """Detect this machine's LAN IP address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ══════════════════════════════════════════════════════════════════
# WebSocket broadcast
# ══════════════════════════════════════════════════════════════════

async def broadcast(msg: dict):
    """Send a message to all connected dashboard WebSocket clients."""
    dead = set()
    text = json.dumps(msg)
    for ws in list(ws_clients):  # snapshot to avoid concurrent modification
        try:
            await ws.send_text(text)
        except Exception:
            dead.add(ws)
    ws_clients.difference_update(dead)


def broadcast_sync(msg: dict):
    """Thread-safe broadcast — called from OSC handler threads."""
    if _main_loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(broadcast(msg), _main_loop)
    except RuntimeError:
        pass


def emit_terminal(level: str, message: str, device: str = ""):
    """Push a system terminal entry to all dashboard clients."""
    broadcast_sync({
        "type": "sys_terminal", "level": level, "message": message,
        "device": device, "timestamp": datetime.now().isoformat(timespec="milliseconds"),
    })


def push_devices():
    """Push the full device list to all dashboard clients."""
    broadcast_sync({"type": "device_update", "devices": device_mgr.get_all_dicts()})


# ══════════════════════════════════════════════════════════════════
# OSC event handler — called from OSC listener thread
# ══════════════════════════════════════════════════════════════════

def handle_osc_event(event_type: str, data: dict):
    """
    Central handler for all incoming OSC messages.
    Resolves source IP to a device, handles auto-discovery,
    and dispatches to the appropriate handler.
    """
    source_ip = data.get("source_ip", "")

    # ── Resolve device from IP ────────────────────────────────
    devices = device_mgr.find_by_ip(source_ip) if source_ip else []
    device = devices[0] if devices else None
    device_id = device.id if device else ""

    # ── Auto-discovery (unknown sender) ───────────────────────
    if not device and source_ip and config.get("auto_discover_devices"):
        now = time.time()
        with _recently_seen_lock:
            seen_ago = now - _recently_seen.get(source_ip, 0)
            _recently_seen[source_ip] = now
        if seen_ago > 30:
            result = device_mgr.add_discovered_device(source_ip, config.get("osc_default_device_port"))
            if result["ok"] and result["is_new"]:
                logger.info(f"Auto-discovered {source_ip}")
                broadcast_sync({"type": "device_discovered", "device": result["device"]})
                push_devices()
            devices = device_mgr.find_by_ip(source_ip)
            device = devices[0] if devices else None
            device_id = device.id if device else ""

    # ── Mark device as seen ───────────────────────────────────
    if device:
        device_mgr.mark_seen(device_id)

    label = device.actor_name if device and device.actor_name else source_ip

    # ── Dispatch by event type ────────────────────────────────

    if event_type == "alive":
        if device:
            emit_terminal("osc_in", "/Alive", label)
            push_devices()

    elif event_type == "record_start_confirm":
        if device:
            slate = device.pending_slate or device.current_slate
            take = device.pending_take or device.current_take
            # NOW set is_recording=True — phone confirmed
            device_mgr.confirm_recording(device_id)
            session_mgr.record_started(slate, device.actor_name, take, device.ip)
            logger.info(f"RecordStartConfirm from {label}")
            emit_terminal("success", "RecordStartConfirm", label)
            broadcast_sync({"type": "record_confirmed", "device_id": device_id, "actor": label})
            push_devices()

    elif event_type == "record_stop_confirm":
        if device:
            tc = data.get("timecode", "")
            session_mgr.record_stopped(device.current_slate, device.actor_name, device.current_take, device.ip, tc)
            device_mgr.clear_recording(device_id)
            logger.info(f"RecordStopConfirm from {label} TC={tc}")
            emit_terminal("warning", f"RecordStopConfirm TC={tc or 'N/A'}", label)
            broadcast_sync({"type": "record_stop_confirmed", "device_id": device_id, "actor": label, "timecode": tc})
            push_devices()

    elif event_type == "slate_confirm":
        if device:
            emit_terminal("osc_in", "SlateConfirm", label)

    elif event_type == "battery":
        if device:
            pct = data.get("percent", -1)
            device_mgr.update_battery(device_id, pct)
            emit_terminal("osc_in", f"Battery {pct}%", label)
            push_devices()

    elif event_type == "target_confirm":
        if device:
            emit_terminal("osc_in", "SetSendTargetConfirm", label)
            push_devices()

    elif event_type == "unknown_message":
        addr = data.get("address", "?")
        emit_terminal("osc_in", f"{addr}", label)


# ══════════════════════════════════════════════════════════════════
# Poll loop — keeps phones alive during long recording sessions
# ══════════════════════════════════════════════════════════════════

async def poll_devices():
    """
    Periodic keepalive loop. Sends BatteryQuery to ALL confirmed devices.
    Only sends SetTarget to non-recording devices (SetTarget disrupts Live Link Face).
    """
    global _last_poll_time
    while True:
        try:
            interval = config.get("battery_poll_interval_sec")
            await asyncio.sleep(interval)
            _last_poll_time = time.time()

            confirmed = device_mgr.get_confirmed_devices()
            server_ip = get_server_ip()
            listen_port = config.get("osc_listen_port")

            for dev in confirmed:
                try:
                    # SetTarget only for idle devices — disrupts recording
                    if not dev.is_recording:
                        osc.send_set_target(dev.ip, dev.port, server_ip, listen_port)
                    # BatteryQuery is harmless — always send
                    osc.send_battery_query(dev.ip, dev.port)
                except Exception as e:
                    logger.warning(f"Poll failed for {dev.actor_name or dev.ip}: {e}")

            if confirmed:
                emit_terminal("info", f"Poll — {len(confirmed)} device(s)", "SYSTEM")

            await broadcast({"type": "poll_tick", "last_poll_time": _last_poll_time, "poll_interval": interval})
            await broadcast({"type": "device_update", "devices": device_mgr.get_all_dicts()})

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Poll error: {e}")
            await asyncio.sleep(5)


# ══════════════════════════════════════════════════════════════════
# Zeroconf callback
# ══════════════════════════════════════════════════════════════════

def _handle_zeroconf_found(ip: str, port: int, name: str):
    """Called when Zeroconf discovers a new device on the network."""
    result = device_mgr.add_discovered_device(ip, port, device_name=name)
    if result["ok"] and result["is_new"]:
        logger.info(f"Zeroconf: {name} at {ip}:{port}")
        broadcast_sync({"type": "device_discovered", "device": result["device"]})
        push_devices()


# ══════════════════════════════════════════════════════════════════
# App lifespan
# ══════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start OSC engine, discovery, and poll loop on startup; stop on shutdown."""
    global osc, _poll_task, _discovery, start_time, _main_loop
    start_time = time.time()
    _main_loop = asyncio.get_running_loop()

    # Start OSC listener
    listen_port = config.get("osc_listen_port")
    osc = OSCEngine(listen_port=listen_port, on_event=handle_osc_event)
    osc.start()
    logger.info(f"ActionCues starting — OSC port {listen_port}")

    # Start Zeroconf discovery
    _discovery = ZeroconfDiscovery(on_found=_handle_zeroconf_found)
    _discovery.start()

    # Start poll loop
    _poll_task = asyncio.create_task(poll_devices())

    yield

    # Shutdown
    _poll_task.cancel()
    if _discovery:
        _discovery.stop()
    osc.stop()
    logger.info("ActionCues stopped")


# ══════════════════════════════════════════════════════════════════
# FastAPI app
# ══════════════════════════════════════════════════════════════════

app = FastAPI(title="ActionCues — Live Link Face Remote Control", lifespan=lifespan)
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    """Serve the dashboard."""
    return FileResponse(STATIC_DIR / "index.html")


# ══════════════════════════════════════════════════════════════════
# API — Device CRUD
# ══════════════════════════════════════════════════════════════════

class AddDeviceRequest(BaseModel):
    ip: str
    port: int = 8000
    actor_name: str


class RenameActorRequest(BaseModel):
    device_id: str
    actor_name: str


class ConfirmDiscoveredRequest(BaseModel):
    device_id: str
    actor_name: str


@app.post("/api/devices/add")
async def add_device(req: AddDeviceRequest):
    """Add a device manually. Validates IP, then sends SetTarget + BatteryQuery."""
    # Validate IP address format
    try:
        socket.inet_aton(req.ip)
    except socket.error:
        return {"ok": False, "error": f"Invalid IP address: {req.ip}"}
    actor = req.actor_name.strip()
    if "|" in actor:
        return {"ok": False, "error": "Actor name cannot contain '|'"}
    result = device_mgr.add_device(req.ip, req.port, actor)
    if result["ok"]:
        server_ip = get_server_ip()
        listen_port = config.get("osc_listen_port")
        osc.send_set_target(req.ip, req.port, server_ip, listen_port)
        osc.send_battery_query(req.ip, req.port)
        emit_terminal("success", f"Added {req.actor_name} @ {req.ip}", "SYSTEM")
        await broadcast({"type": "device_update", "devices": device_mgr.get_all_dicts()})
    return result


@app.post("/api/devices/remove")
async def remove_device(req: Request):
    """Remove a device by id."""
    body = await req.json()
    device_id = body.get("device_id", "")
    dev = device_mgr.get_device(device_id)
    ok = device_mgr.remove_device(device_id)
    if ok:
        # Clean up cached UDP client
        if dev:
            osc.remove_client(dev.ip, dev.port)
        await broadcast({"type": "device_update", "devices": device_mgr.get_all_dicts()})
    return {"ok": ok}


@app.post("/api/devices/rename")
async def rename_actor(req: RenameActorRequest):
    """Rename a device's actor name."""
    new_name = req.actor_name.strip()
    if "|" in new_name:
        return {"ok": False, "error": "Actor name cannot contain '|'"}
    result = device_mgr.rename_actor(req.device_id, new_name)
    if result["ok"]:
        await broadcast({"type": "device_update", "devices": device_mgr.get_all_dicts()})
    return result


@app.get("/api/devices")
async def get_devices():
    """Get all devices."""
    return {"devices": device_mgr.get_all_dicts()}


@app.post("/api/devices/ping")
async def ping_device(req: Request):
    """
    Ping a single device. Sends BatteryQuery (always safe).
    Only sends SetTarget to non-recording devices.
    """
    body = await req.json()
    device_id = body.get("device_id", "")
    dev = device_mgr.get_device(device_id)
    if not dev:
        return {"ok": False, "error": "Not found"}
    if not dev.is_recording:
        server_ip = get_server_ip()
        listen_port = config.get("osc_listen_port")
        osc.send_set_target(dev.ip, dev.port, server_ip, listen_port)
    osc.send_battery_query(dev.ip, dev.port)
    emit_terminal("osc_out", "Ping", dev.actor_name or dev.ip)
    return {"ok": True}


@app.post("/api/devices/confirm-discovered")
async def confirm_discovered(req: ConfirmDiscoveredRequest):
    """Confirm a discovered device by assigning an actor name."""
    actor = req.actor_name.strip()
    if "|" in actor:
        return {"ok": False, "error": "Actor name cannot contain '|'"}
    result = device_mgr.confirm_discovered_device(req.device_id, actor)
    if result["ok"]:
        dev = device_mgr.get_device(req.device_id)
        if dev:
            server_ip = get_server_ip()
            listen_port = config.get("osc_listen_port")
            osc.send_set_target(dev.ip, dev.port, server_ip, listen_port)
            osc.send_battery_query(dev.ip, dev.port)
        await broadcast({"type": "device_update", "devices": device_mgr.get_all_dicts()})
    return result


# ══════════════════════════════════════════════════════════════════
# API — Recording
# ══════════════════════════════════════════════════════════════════

class RecordStartRequest(BaseModel):
    slate: str
    device_ids: Optional[List[str]] = None
    take_override: Optional[int] = None


@app.post("/api/record/start")
async def record_start(req: RecordStartRequest):
    """
    Start recording on target devices. NO STATE FILTERING.
    If a device is stuck in is_recording, we force-clear and re-record.
    Both /Slate and /RecordStart get slate_actorname for unique filenames.
    """
    slate = req.slate.strip()
    if not slate:
        return {"ok": False, "error": "Slate name required"}
    if "|" in slate:
        return {"ok": False, "error": "Slate name cannot contain '|'"}
    session_mgr.current_slate = slate

    # Get target devices — NO filtering by is_recording
    targets = device_mgr.get_confirmed_devices()
    if req.device_ids:
        targets = [d for d in targets if d.id in req.device_ids]
    if not targets:
        return {"ok": False, "error": "No confirmed devices"}

    results = []
    errors = []
    for dev in targets:
        try:
            # If device is stuck in recording state, force-clear first
            if dev.is_recording:
                device_mgr.force_clear_recording(dev.id)

            take = session_mgr.reserve_take(slate, dev.actor_name, req.take_override)
            # Composite name: slate_actorname — unique filename per device
            record_name = f"{slate}_{dev.actor_name}"
            # /Slate sets the filename on the phone
            osc.send_slate(dev.ip, dev.port, record_name)
            # /RecordStart triggers recording
            osc.send_record_start(dev.ip, dev.port, record_name, take)
            # Stage only — is_recording stays False until phone confirms
            device_mgr.stage_recording(dev.id, slate, take)
            emit_terminal("osc_out", f"REC START: {record_name} T{take}", dev.actor_name)
            results.append({"device_id": dev.id, "actor": dev.actor_name, "slate": slate, "take": take})
            logger.info(f"Record -> {dev.actor_name}: {record_name} T{take}")
        except Exception as e:
            errors.append({"device_id": dev.id, "actor": dev.actor_name, "error": str(e)})

    await broadcast({"type": "device_update", "devices": device_mgr.get_all_dicts()})
    await broadcast({"type": "recording_started", "slate": slate, "results": results, "errors": errors})
    return {"ok": len(results) > 0, "results": results, "errors": errors}


@app.post("/api/record/stop")
async def record_stop(req: Request):
    """
    Stop recording. Sends /RecordStop to ALL confirmed devices unconditionally.
    The phone ignores it if not recording — no harm done.
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    device_ids = body.get("device_ids") if body else None

    targets = device_mgr.get_confirmed_devices()
    if device_ids:
        targets = [d for d in targets if d.id in device_ids]

    for dev in targets:
        osc.send_record_stop(dev.ip, dev.port)
        emit_terminal("osc_out", "REC STOP", dev.actor_name or dev.ip)

    return {"ok": True, "sent_to": len(targets)}


@app.post("/api/record/force-clear")
async def force_clear_recording(req: Request):
    """Force-clear the recording flag on a stuck device."""
    body = await req.json()
    device_id = body.get("device_id", "")
    dev = device_mgr.get_device(device_id)
    if not dev:
        return {"ok": False, "error": "Not found"}
    if dev.is_recording:
        session_mgr.record_stopped(dev.current_slate, dev.actor_name, dev.current_take, dev.ip, "(force-cleared)")
    device_mgr.force_clear_recording(device_id)
    emit_terminal("warning", "Force-cleared", dev.actor_name)
    await broadcast({"type": "device_update", "devices": device_mgr.get_all_dicts()})
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# API — Slate
# ══════════════════════════════════════════════════════════════════

class SlateRequest(BaseModel):
    slate: str


@app.post("/api/slate/set")
async def set_slate(req: SlateRequest):
    """Store slate name locally. Phones get it during record start with composite name."""
    slate = req.slate.strip()
    if not slate:
        return {"ok": False, "error": "Slate name required"}
    if "|" in slate:
        return {"ok": False, "error": "Slate name cannot contain '|'"}
    session_mgr.current_slate = slate
    await broadcast({"type": "slate_changed", "slate": slate})
    return {"ok": True, "slate": slate}


@app.get("/api/slate")
async def get_slate():
    return {"slate": session_mgr.current_slate}


# ══════════════════════════════════════════════════════════════════
# API — Takes
# ══════════════════════════════════════════════════════════════════

class SetTakeRequest(BaseModel):
    slate: str
    actor: str
    take: int


@app.get("/api/takes")
async def get_takes():
    return {"takes": session_mgr.get_all_takes(), "current_slate": session_mgr.current_slate}


@app.post("/api/takes/set")
async def set_take(req: SetTakeRequest):
    return session_mgr.set_take_number(req.slate, req.actor, req.take)


# ══════════════════════════════════════════════════════════════════
# API — History
# ══════════════════════════════════════════════════════════════════

@app.get("/api/history")
async def get_history():
    return {"history": session_mgr.get_history()}


@app.post("/api/history/clear")
async def clear_history():
    session_mgr.clear_session()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# API — Command Log
# ══════════════════════════════════════════════════════════════════

@app.get("/api/log")
async def get_log():
    return {"log": osc.command_log.get()}


@app.post("/api/log/clear")
async def clear_log():
    osc.command_log.clear()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# API — Settings
# ══════════════════════════════════════════════════════════════════

@app.get("/api/settings")
async def get_settings():
    return {"settings": config.get_all()}


@app.post("/api/settings")
async def update_settings(req: Request):
    body = await req.json()
    allowed = set(config.DEFAULTS.keys())
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        return {"ok": False, "error": "No valid settings"}
    config.update(updates)
    return {"ok": True, "settings": config.get_all()}


# ══════════════════════════════════════════════════════════════════
# API — Server management
# ══════════════════════════════════════════════════════════════════

@app.post("/api/server/restart")
async def restart_server():
    """Restart the server process via re-exec."""
    logger.info("Server restart requested")
    emit_terminal("warning", "Server restarting...", "SYSTEM")
    asyncio.get_running_loop().call_later(1.0, _do_restart)
    return {"ok": True, "message": "Restarting..."}


def _do_restart():
    """Gracefully stop services, then re-exec the current Python process."""
    if _poll_task:
        _poll_task.cancel()
    if _discovery:
        _discovery.stop()
    if osc:
        osc.stop()
    logger.info("Services stopped, re-execing...")
    python = sys.executable
    os.execv(python, [python] + sys.argv)


# ══════════════════════════════════════════════════════════════════
# API — Discovery
# ══════════════════════════════════════════════════════════════════

@app.post("/api/discovery/scan")
async def discovery_scan():
    """Restart Zeroconf discovery to force a fresh network scan."""
    if _discovery:
        if _discovery.is_running:
            _discovery.stop()
        _discovery.start()
    return {"ok": True}


@app.get("/api/discovery/status")
async def discovery_status():
    return {"running": _discovery.is_running if _discovery else False}


# ══════════════════════════════════════════════════════════════════
# API — Server status
# ══════════════════════════════════════════════════════════════════

@app.get("/api/status")
async def get_status():
    all_d = device_mgr.get_all_dicts()
    confirmed = sum(1 for d in all_d if d["actor_name"])
    recording = sum(1 for d in all_d if d["is_recording"])
    return {
        "server_ip": get_server_ip(),
        "osc_listen_port": config.get("osc_listen_port"),
        "http_port": config.get("http_port"),
        "uptime_sec": round(time.time() - start_time),
        "total_devices": confirmed,
        "recording_devices": recording,
        "osc_running": osc.is_running if osc else False,
        "current_slate": session_mgr.current_slate,
    }


# ══════════════════════════════════════════════════════════════════
# WebSocket
# ══════════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Dashboard WebSocket — sends init payload then streams updates."""
    await ws.accept()
    ws_clients.add(ws)
    # Send full state on connect
    await ws.send_text(json.dumps({
        "type": "init",
        "devices": device_mgr.get_all_dicts(),
        "slate": session_mgr.current_slate,
        "settings": config.get_all(),
        "last_poll_time": _last_poll_time,
        "poll_interval": config.get("battery_poll_interval_sec"),
    }))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_clients.discard(ws)


# ══════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    port = config.get("http_port")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
