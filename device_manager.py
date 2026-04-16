"""
ActionCues — Device Manager

Tracks all devices running Live Link Face. Thread-safe.

STATUS MODEL (two states only):
  - "idle"      = device has an actor name, not recording
  - "recording" = phone confirmed it is recording

No connected/disconnected concept. Commands always fire to all confirmed devices.
Manually-added devices persist to disk; auto-discovered ones are ephemeral.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Dict

# ── Paths ─────────────────────────────────────────────────────────

DATA_DIR = Path(__file__).parent / "data"
DEVICES_FILE = DATA_DIR / "devices.json"


# ── Device dataclass ──────────────────────────────────────────────

@dataclass
class Device:
    """Represents a single device running Live Link Face."""

    ip: str                                # device IP address
    port: int                              # OSC port on the device
    actor_name: str                        # empty string = discovered but not confirmed
    battery_percent: int = -1              # -1 = unknown, 0-100 = percentage
    last_seen: float = 0.0                 # unix timestamp of last OSC message received
    is_recording: bool = False             # True only after phone sends RecordStartConfirm
    current_slate: str = ""                # active slate name during recording
    current_take: int = 0                  # active take number during recording
    recording_start_time: float = 0.0      # unix timestamp when recording confirmed
    auto_discovered: bool = False          # True = found via OSC/Zeroconf, not yet persisted
    device_name: str = ""                  # mDNS/Bonjour device name if available
    pending_slate: str = ""                # slate sent to phone, waiting for confirm
    pending_take: int = 0                  # take sent to phone, waiting for confirm

    @property
    def id(self) -> str:
        """Unique identifier: ip:port."""
        return f"{self.ip}:{self.port}"

    def to_dict(self) -> dict:
        """Serialize for JSON/WebSocket. Status computed from is_recording flag."""
        if self.is_recording:
            status = "recording"
        elif self.actor_name:
            status = "idle"
        else:
            status = "discovered"
        return {
            "id": self.id,
            "ip": self.ip,
            "port": self.port,
            "actor_name": self.actor_name,
            "device_name": self.device_name,
            "status": status,
            "battery_percent": self.battery_percent,
            "last_seen": self.last_seen,
            "is_recording": self.is_recording,
            "current_slate": self.current_slate,
            "current_take": self.current_take,
            "recording_start_time": self.recording_start_time,
            "auto_discovered": self.auto_discovered,
        }


# ── Device Manager ────────────────────────────────────────────────

class DeviceManager:
    """Thread-safe CRUD and state management for the device list."""

    def __init__(self):
        self._lock = threading.Lock()
        self._devices: Dict[str, Device] = {}
        self._load()

    # ── Persistence ───────────────────────────────────────────────

    def _load(self):
        """Load manually-added devices from disk on startup."""
        if DEVICES_FILE.exists():
            try:
                with open(DEVICES_FILE, "r") as f:
                    data = json.load(f)
                for d in data:
                    dev = Device(
                        ip=d["ip"], port=d["port"], actor_name=d["actor_name"],
                        device_name=d.get("device_name", ""),
                    )
                    self._devices[dev.id] = dev
            except (json.JSONDecodeError, IOError, KeyError):
                pass

    def _save(self):
        """Persist only manually-added devices (not auto-discovered)."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            devices = [
                {"ip": d.ip, "port": d.port, "actor_name": d.actor_name, "device_name": d.device_name}
                for d in self._devices.values() if not d.auto_discovered
            ]
            tmp = DEVICES_FILE.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(devices, f, indent=2)
            tmp.replace(DEVICES_FILE)
        except Exception as e:
            logging.getLogger("device_manager").error(f"Failed to persist devices: {e}")

    # ── CRUD ──────────────────────────────────────────────────────

    def add_device(self, ip: str, port: int, actor_name: str) -> dict:
        """Manually add a device. Rejects duplicate actor names."""
        with self._lock:
            device_id = f"{ip}:{port}"
            for dev in self._devices.values():
                if dev.actor_name == actor_name and dev.id != device_id:
                    return {"ok": False, "error": f"Name '{actor_name}' already used by {dev.ip}"}
            dev = Device(ip=ip, port=port, actor_name=actor_name)
            self._devices[device_id] = dev
            self._save()
            return {"ok": True, "device": dev.to_dict()}

    def remove_device(self, device_id: str) -> bool:
        """Remove a device by id. No guards — always works."""
        with self._lock:
            if device_id in self._devices:
                del self._devices[device_id]
                self._save()
                return True
            return False

    def rename_actor(self, device_id: str, new_name: str) -> dict:
        """Change actor name. Rejects duplicates."""
        with self._lock:
            if device_id not in self._devices:
                return {"ok": False, "error": "Device not found"}
            for dev in self._devices.values():
                if dev.actor_name == new_name and dev.id != device_id:
                    return {"ok": False, "error": f"Name '{new_name}' already in use"}
            self._devices[device_id].actor_name = new_name
            self._save()
            return {"ok": True}

    # ── Queries ───────────────────────────────────────────────────

    def get_device(self, device_id: str) -> Optional[Device]:
        """Get a single device by id."""
        with self._lock:
            return self._devices.get(device_id)

    def get_all_devices(self) -> List[Device]:
        """Get all devices (confirmed + discovered)."""
        with self._lock:
            return list(self._devices.values())

    def get_all_dicts(self) -> List[dict]:
        """Get all devices as serializable dicts for the frontend."""
        with self._lock:
            return [d.to_dict() for d in self._devices.values()]

    def get_confirmed_devices(self) -> List[Device]:
        """Get only devices with an actor name (eligible for commands)."""
        with self._lock:
            return [d for d in self._devices.values() if d.actor_name]

    def find_by_ip(self, ip: str) -> List[Device]:
        """Find all devices matching an IP address."""
        with self._lock:
            return [d for d in self._devices.values() if d.ip == ip]

    # ── State updates ─────────────────────────────────────────────

    def mark_seen(self, device_id: str):
        """Update last_seen timestamp when any OSC message arrives from this device."""
        with self._lock:
            if device_id in self._devices:
                self._devices[device_id].last_seen = time.time()

    def update_battery(self, device_id: str, percent: int):
        """Update battery percentage from /Battery or /BatteryResponse."""
        with self._lock:
            if device_id in self._devices:
                self._devices[device_id].battery_percent = percent

    def stage_recording(self, device_id: str, slate: str, take: int):
        """Store pending slate/take after sending commands. Does NOT set is_recording."""
        with self._lock:
            if device_id in self._devices:
                dev = self._devices[device_id]
                dev.pending_slate = slate
                dev.pending_take = take

    def confirm_recording(self, device_id: str):
        """Called on RecordStartConfirm — phone confirmed it is recording."""
        with self._lock:
            if device_id in self._devices:
                dev = self._devices[device_id]
                dev.is_recording = True
                dev.current_slate = dev.pending_slate
                dev.current_take = dev.pending_take
                dev.recording_start_time = time.time()
                dev.pending_slate = ""
                dev.pending_take = 0

    def clear_recording(self, device_id: str):
        """Called on RecordStopConfirm — phone confirmed it stopped recording."""
        with self._lock:
            if device_id in self._devices:
                dev = self._devices[device_id]
                dev.is_recording = False
                dev.recording_start_time = 0.0
                dev.current_slate = ""
                dev.current_take = 0
                dev.pending_slate = ""
                dev.pending_take = 0

    def force_clear_recording(self, device_id: str) -> bool:
        """Force-clear all recording and pending state. For stuck devices."""
        with self._lock:
            if device_id in self._devices:
                dev = self._devices[device_id]
                dev.is_recording = False
                dev.recording_start_time = 0.0
                dev.current_slate = ""
                dev.current_take = 0
                dev.pending_slate = ""
                dev.pending_take = 0
                return True
            return False

    # ── Auto-discovery ────────────────────────────────────────────

    def add_discovered_device(self, ip: str, port: int, device_name: str = "") -> dict:
        """Add a device found via OSC or Zeroconf. Not persisted until confirmed."""
        with self._lock:
            existing = [d for d in self._devices.values() if d.ip == ip]
            if existing:
                if device_name and not existing[0].device_name:
                    existing[0].device_name = device_name
                return {"ok": True, "device": existing[0].to_dict(), "is_new": False}
            device_id = f"{ip}:{port}"
            dev = Device(
                ip=ip, port=port, actor_name="", auto_discovered=True,
                last_seen=time.time(), device_name=device_name,
            )
            self._devices[device_id] = dev
            return {"ok": True, "device": dev.to_dict(), "is_new": True}

    def confirm_discovered_device(self, device_id: str, actor_name: str) -> dict:
        """Promote a discovered device to confirmed by assigning an actor name."""
        with self._lock:
            if device_id not in self._devices:
                return {"ok": False, "error": "Device not found"}
            if not actor_name.strip():
                return {"ok": False, "error": "Actor name is required"}
            for dev in self._devices.values():
                if dev.actor_name == actor_name and dev.id != device_id:
                    return {"ok": False, "error": f"Name '{actor_name}' already in use"}
            dev = self._devices[device_id]
            dev.actor_name = actor_name.strip()
            dev.auto_discovered = False
            self._save()
            return {"ok": True, "device": dev.to_dict()}
