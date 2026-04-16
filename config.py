"""
ActionCues — Configuration

Persistent key/value settings stored in data/settings.json.
Thread-safe. Auto-initializes on import. Atomic file writes (tmp+rename).
"""

import json
import threading
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────

DATA_DIR = Path(__file__).parent / "data"
SETTINGS_FILE = DATA_DIR / "settings.json"

# ── Default values ────────────────────────────────────────────────

DEFAULTS = {
    "osc_listen_port": 8000,            # UDP port for incoming device OSC messages
    "osc_default_device_port": 8000,    # default OSC port when adding a new device
    "http_port": 7100,                  # dashboard web server port
    "confirm_timeout_sec": 3,           # seconds to wait for a confirm reply
    "battery_poll_interval_sec": 30,    # seconds between keepalive/battery polls
    "auto_discover_devices": True,      # auto-detect devices sending OSC to us
    "lock_during_recording": True,      # disable slate/take/send-slate while any device is recording
    "keyboard_shortcuts_enabled": False, # enable keyboard shortcuts (Esc = stop all)
}

# ── Internal state ────────────────────────────────────────────────

_lock = threading.Lock()
_settings: dict = {}


# ── File I/O ──────────────────────────────────────────────────────

def _load() -> dict:
    """Load settings from disk, return empty dict on failure."""
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def _save(data: dict):
    """Atomic write: write to .tmp then rename."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(SETTINGS_FILE)


# ── Public API ────────────────────────────────────────────────────

def init():
    """Merge stored settings with defaults and persist."""
    global _settings
    with _lock:
        stored = _load()
        _settings = {k: stored.get(k, v) for k, v in DEFAULTS.items()}
        _save(_settings)


def get(key: str):
    """Get a single setting value."""
    with _lock:
        return _settings.get(key, DEFAULTS.get(key))


def get_all() -> dict:
    """Get a copy of all settings."""
    with _lock:
        return dict(_settings)


def update(updates: dict):
    """Update one or more settings and persist. Validates types and rejects bad values."""
    import math
    global _settings
    with _lock:
        clean = {}
        for k, v in updates.items():
            if k not in DEFAULTS:
                continue
            expected = type(DEFAULTS[k])
            if expected is int and isinstance(v, (int, float)):
                if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                    continue  # reject nan/inf
                clean[k] = int(v)
            elif expected is bool:
                if isinstance(v, bool):
                    clean[k] = v
                # skip non-bool values for bool settings
            else:
                clean[k] = v
        _settings.update(clean)
        _save(_settings)


# ── Auto-init on import ──────────────────────────────────────────

init()
