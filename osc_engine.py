"""
ActionCues — OSC Engine

All Open Sound Control communication with Live Link Face devices.
Runs a UDP listener in a background thread for incoming OSC messages.
Provides send methods for outgoing commands.
Maintains a circular command log for the dashboard.
"""

import logging
import threading
from collections import deque
from datetime import datetime
from typing import Optional, Dict, List

from pythonosc import udp_client
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

logger = logging.getLogger("osc_engine")


# ══════════════════════════════════════════════════════════════════
# Command Log — thread-safe circular buffer
# ══════════════════════════════════════════════════════════════════

class CommandLog:
    """Records every OSC message in/out for the dashboard log tab."""

    def __init__(self, max_entries: int = 2000):
        self._lock = threading.Lock()
        self._entries: deque = deque(maxlen=max_entries)

    def add(self, direction: str, address: str, args: list, device: str = ""):
        """Add a log entry. direction = 'OUT' or 'IN'."""
        entry = {
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "direction": direction,
            "address": address,
            "args": [str(a) for a in args],
            "device": device,
        }
        with self._lock:
            self._entries.append(entry)

    def get(self, limit: int = 200) -> List[dict]:
        """Return the last N entries."""
        with self._lock:
            items = list(self._entries)
        return items[-limit:]

    def clear(self):
        """Clear all entries."""
        with self._lock:
            self._entries.clear()


# ══════════════════════════════════════════════════════════════════
# OSC Engine
# ══════════════════════════════════════════════════════════════════

class OSCEngine:
    """Manages all OSC send/receive for Live Link Face devices."""

    def __init__(self, listen_port: int, on_event=None):
        """
        Args:
            listen_port: UDP port to listen for incoming OSC messages.
            on_event:    callback(event_type: str, data: dict) for incoming events.
        """
        self.listen_port = listen_port
        self.on_event = on_event
        self.command_log = CommandLog()
        self._server: Optional[ThreadingOSCUDPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._clients: Dict[str, udp_client.SimpleUDPClient] = {}
        self._clients_lock = threading.Lock()
        self._running = False

    # ── Client pool ───────────────────────────────────────────────

    def _get_client(self, ip: str, port: int) -> udp_client.SimpleUDPClient:
        """Get or create a cached UDP client for a device. Thread-safe."""
        key = f"{ip}:{port}"
        with self._clients_lock:
            if key not in self._clients:
                self._clients[key] = udp_client.SimpleUDPClient(ip, port)
            return self._clients[key]

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self):
        """Start the OSC listener in a background thread."""
        if self._running:
            return

        # Map all Live Link Face response addresses
        dispatcher = Dispatcher()
        dispatcher.map("/RecordStartConfirm", self._on_record_start_confirm, needs_reply_address=True)
        dispatcher.map("/RecordStopConfirm", self._on_record_stop_confirm, needs_reply_address=True)
        dispatcher.map("/SlateConfirm", self._on_slate_confirm, needs_reply_address=True)
        dispatcher.map("/OSCSetSendTargetConfirm", self._on_target_confirm, needs_reply_address=True)
        dispatcher.map("/Alive", self._on_alive, needs_reply_address=True)
        dispatcher.map("/Battery", self._on_battery, needs_reply_address=True)
        dispatcher.map("/BatteryResponse", self._on_battery, needs_reply_address=True)
        # Catch-all for unknown messages — logged but ignored
        dispatcher.set_default_handler(self._on_unknown, needs_reply_address=True)

        self._server = ThreadingOSCUDPServer(("0.0.0.0", self.listen_port), dispatcher)
        self._running = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="osc-listener",
        )
        self._thread.start()
        logger.info(f"OSC listener started on port {self.listen_port}")

    def stop(self):
        """Stop the listener thread and clear client cache."""
        if self._server:
            try:
                self._server.shutdown()
            except Exception as e:
                logger.error(f"Error shutting down OSC server: {e}")
            self._running = False
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=2.0)
            with self._clients_lock:
                self._clients.clear()
            logger.info("OSC listener stopped")

    def remove_client(self, ip: str, port: int):
        """Remove a cached UDP client when a device is removed."""
        with self._clients_lock:
            self._clients.pop(f"{ip}:{port}", None)

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Outgoing commands ─────────────────────────────────────────

    def send_record_start(self, ip: str, port: int, slate: str, take: int):
        """Send /RecordStart [composite_name, take] to trigger recording."""
        client = self._get_client(ip, port)
        client.send_message("/RecordStart", [slate, take])
        self.command_log.add("OUT", "/RecordStart", [slate, take], f"{ip}:{port}")

    def send_record_stop(self, ip: str, port: int):
        """Send /RecordStop to stop recording."""
        client = self._get_client(ip, port)
        client.send_message("/RecordStop", [])
        self.command_log.add("OUT", "/RecordStop", [], f"{ip}:{port}")

    def send_slate(self, ip: str, port: int, slate: str):
        """Send /Slate [name] to set the slate/filename on the device."""
        client = self._get_client(ip, port)
        client.send_message("/Slate", [slate])
        self.command_log.add("OUT", "/Slate", [slate], f"{ip}:{port}")

    def send_set_target(self, ip: str, port: int, target_ip: str, target_port: int):
        """Send /OSCSetSendTarget to tell the device where to send its responses."""
        client = self._get_client(ip, port)
        client.send_message("/OSCSetSendTarget", [target_ip, target_port])
        self.command_log.add("OUT", "/OSCSetSendTarget", [target_ip, target_port], f"{ip}:{port}")

    def send_battery_query(self, ip: str, port: int):
        """Send /BatteryQuery to request battery level."""
        client = self._get_client(ip, port)
        client.send_message("/BatteryQuery", [])
        self.command_log.add("OUT", "/BatteryQuery", [], f"{ip}:{port}")

    # ── Incoming handlers ─────────────────────────────────────────
    # All handlers extract sender IP via needs_reply_address=True.

    def _emit(self, event_type: str, data: dict):
        """Forward an event to the registered callback."""
        if self.on_event:
            try:
                self.on_event(event_type, data)
            except Exception as e:
                logger.error(f"Event handler error: {e}")

    def _on_record_start_confirm(self, client_address, address, *args):
        """Device confirmed it started recording."""
        ip = client_address[0]
        self.command_log.add("IN", address, list(args), ip)
        self._emit("record_start_confirm", {"source_ip": ip, "args": list(args)})

    def _on_record_stop_confirm(self, client_address, address, *args):
        """Device confirmed it stopped recording. May include timecode + file paths."""
        ip = client_address[0]
        timecode = str(args[0]) if len(args) > 0 else ""
        self.command_log.add("IN", address, list(args), ip)
        self._emit("record_stop_confirm", {"source_ip": ip, "timecode": timecode})

    def _on_slate_confirm(self, client_address, address, *args):
        """Device confirmed the slate name was set."""
        ip = client_address[0]
        self.command_log.add("IN", address, list(args), ip)
        self._emit("slate_confirm", {"source_ip": ip, "args": list(args)})

    def _on_target_confirm(self, client_address, address, *args):
        """Device confirmed it received the SetSendTarget command."""
        ip = client_address[0]
        self.command_log.add("IN", address, list(args), ip)
        self._emit("target_confirm", {"source_ip": ip})

    def _on_alive(self, client_address, address, *args):
        """Device heartbeat — marks device as seen."""
        ip = client_address[0]
        self.command_log.add("IN", address, list(args), ip)
        self._emit("alive", {"source_ip": ip})

    def _on_battery(self, client_address, address, *args):
        """Device battery response. Live Link Face sends 0.0-1.0 float."""
        ip = client_address[0]
        percent = -1
        for a in args:
            if isinstance(a, (int, float)):
                val = float(a)
                # Live Link Face sends 0.0-1.0 fraction; clamp to valid range
                if val <= 1.0:
                    percent = max(0, min(100, int(val * 100)))
                else:
                    percent = max(0, min(100, int(val)))
                break
        self.command_log.add("IN", address, list(args), ip)
        self._emit("battery", {"source_ip": ip, "percent": percent})

    def _on_unknown(self, client_address, address, *args):
        """Catch-all for unknown OSC messages. Logged but ignored."""
        ip = client_address[0]
        self.command_log.add("IN", address, list(args), ip)
        logger.debug(f"Unknown OSC from {ip}: {address} {args}")
        self._emit("unknown_message", {"source_ip": ip, "address": address, "args": list(args)})
