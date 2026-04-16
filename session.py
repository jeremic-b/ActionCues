"""
ActionCues — Session Manager

Slate naming, take numbering, and recording history.
Take numbers are tracked per (slate, actor) pair and persisted to disk.
No take number is ever reused for a given (slate, actor) pair.
History is append-only JSON-lines for crash safety.
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Set

# ── Paths ─────────────────────────────────────────────────────────

DATA_DIR = Path(__file__).parent / "data"
TAKES_FILE = DATA_DIR / "takes.json"
HISTORY_FILE = DATA_DIR / "session_history.jsonl"


class SessionManager:
    """Manages slate names, take counters, and recording history."""

    def __init__(self):
        self._lock = threading.Lock()
        self._takes: Dict[Tuple[str, str], int] = {}     # (slate, actor) -> highest take
        self._used: Set[Tuple[str, str, int]] = set()     # all recorded (slate, actor, take)
        self._current_slate = ""
        self._history: List[dict] = []
        self._load()

    # ── Persistence ───────────────────────────────────────────────

    def _load(self):
        """Load take counters and history from disk."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # Load takes
        if TAKES_FILE.exists():
            try:
                with open(TAKES_FILE, "r") as f:
                    data = json.load(f)
                for key_str, val in data.get("takes", {}).items():
                    parts = key_str.split("|", 1)
                    if len(parts) == 2:
                        self._takes[(parts[0], parts[1])] = val
                for entry in data.get("used", []):
                    if len(entry) == 3:
                        self._used.add((entry[0], entry[1], entry[2]))
            except (json.JSONDecodeError, IOError):
                pass
        # Load history (JSON-lines format — skip malformed lines)
        if HISTORY_FILE.exists():
            try:
                with open(HISTORY_FILE, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                self._history.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue  # skip bad line, keep reading
            except IOError:
                pass

    def _save_takes(self):
        """Persist take counters to disk (atomic write)."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        serializable = {
            "takes": {f"{k[0]}|{k[1]}": v for k, v in self._takes.items()},
            "used": [[s, a, t] for s, a, t in self._used],
        }
        tmp = TAKES_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(serializable, f, indent=2)
        tmp.replace(TAKES_FILE)

    def _append_history(self, entry: dict):
        """Append a history entry to memory and disk. Must be called with _lock held."""
        self._history.append(entry)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")

    # ── Slate ─────────────────────────────────────────────────────

    @property
    def current_slate(self) -> str:
        with self._lock:
            return self._current_slate

    @current_slate.setter
    def current_slate(self, value: str):
        with self._lock:
            self._current_slate = value.strip()

    # ── Takes ─────────────────────────────────────────────────────

    def get_next_take(self, slate: str, actor: str) -> int:
        """Return the next available take number for this slate+actor."""
        with self._lock:
            return self._takes.get((slate, actor), 0) + 1

    def get_current_take(self, slate: str, actor: str) -> int:
        """Return the last used take number, or 0 if none."""
        with self._lock:
            return self._takes.get((slate, actor), 0)

    def is_take_used(self, slate: str, actor: str, take: int) -> bool:
        """Check if a specific take was already recorded."""
        with self._lock:
            return (slate, actor, take) in self._used

    def reserve_take(self, slate: str, actor: str, take: Optional[int] = None) -> int:
        """
        Reserve the next take number (or a specific one) for recording.
        Raises ValueError if the take was already used.
        """
        with self._lock:
            key = (slate, actor)
            if take is None:
                take = self._takes.get(key, 0) + 1
            if (slate, actor, take) in self._used:
                raise ValueError(
                    f"Take {take} already used for slate='{slate}', actor='{actor}'. "
                    f"Next available: {self._takes.get(key, 0) + 1}"
                )
            self._used.add((slate, actor, take))
            old_take = self._takes.get(key)
            if take > self._takes.get(key, 0):
                self._takes[key] = take
            try:
                self._save_takes()
            except Exception as e:
                # Rollback in-memory state to stay consistent with disk
                self._used.discard((slate, actor, take))
                if old_take is None:
                    self._takes.pop(key, None)
                else:
                    self._takes[key] = old_take
                raise RuntimeError(f"Failed to persist take: {e}") from e
            return take

    def set_take_number(self, slate: str, actor: str, take: int) -> dict:
        """Manually set the next take number for a slate+actor pair."""
        with self._lock:
            if take < 1:
                return {"ok": False, "error": "Take number must be >= 1"}
            if (slate, actor, take) in self._used:
                return {"ok": False, "error": f"Take {take} already recorded for '{slate}'/'{actor}'"}
            self._takes[(slate, actor)] = take - 1
            self._save_takes()
            return {"ok": True, "next_take": take}

    # ── Recording history ─────────────────────────────────────────

    def record_started(self, slate: str, actor: str, take: int, device_ip: str):
        """Log that a recording started."""
        with self._lock:
            self._append_history({
                "event": "record_start",
                "slate": slate, "actor": actor, "take": take,
                "device_ip": device_ip,
                "timestamp": datetime.now().isoformat(),
                "unix_ts": time.time(),
            })

    def record_stopped(self, slate: str, actor: str, take: int, device_ip: str, timecode: str = ""):
        """Log that a recording stopped."""
        with self._lock:
            self._append_history({
                "event": "record_stop",
                "slate": slate, "actor": actor, "take": take,
                "device_ip": device_ip, "timecode": timecode,
                "timestamp": datetime.now().isoformat(),
                "unix_ts": time.time(),
            })

    def get_history(self, limit: int = 200) -> List[dict]:
        """Return recent session history entries."""
        with self._lock:
            return list(self._history[-limit:])

    def get_all_takes(self) -> dict:
        """Return all take counters for the UI."""
        with self._lock:
            return {f"{k[0]}|{k[1]}": v for k, v in self._takes.items()}

    def clear_session(self):
        """Reset all take counters and history. Use between shooting days."""
        with self._lock:
            self._takes.clear()
            self._used.clear()
            self._history.clear()
            self._save_takes()
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(HISTORY_FILE, "w") as f:
                pass  # truncate
