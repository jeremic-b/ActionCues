"""
ActionCues — Zeroconf/Bonjour Discovery

Browses for Live Link Face devices on the local network via mDNS.
Looks for _osc._udp.local. and _oscjson._tcp.local. service types.
Gracefully degrades if the zeroconf package is not installed.
"""

import logging
import socket
import threading
from typing import Callable, Optional

logger = logging.getLogger("discovery")

# ── Optional dependency ───────────────────────────────────────────

try:
    from zeroconf import ServiceBrowser, Zeroconf
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False
    logger.warning("zeroconf package not installed — network discovery disabled")

# ── Service types to browse ───────────────────────────────────────

SERVICE_TYPES = [
    "_osc._udp.local.",
    "_oscjson._tcp.local.",
]


# ══════════════════════════════════════════════════════════════════
# Discovery class
# ══════════════════════════════════════════════════════════════════

class ZeroconfDiscovery:
    """Discovers Live Link Face devices via Bonjour/mDNS."""

    def __init__(self, on_found: Optional[Callable[[str, int, str], None]] = None):
        """
        Args:
            on_found: callback(ip, port, name) when a device is found.
        """
        self.on_found = on_found
        self._zc: Optional["Zeroconf"] = None
        self._browsers: list = []
        self._running = False
        self._lock = threading.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self) -> bool:
        """Start browsing. Returns False if zeroconf is unavailable."""
        if not ZEROCONF_AVAILABLE:
            logger.warning("Cannot start discovery — zeroconf not installed")
            return False
        with self._lock:
            if self._running:
                return True
            try:
                self._zc = Zeroconf()
                for stype in SERVICE_TYPES:
                    browser = ServiceBrowser(self._zc, stype, self)
                    self._browsers.append(browser)
                self._running = True
                logger.info(f"Zeroconf discovery started, browsing: {SERVICE_TYPES}")
                return True
            except Exception as e:
                logger.error(f"Failed to start Zeroconf: {e}")
                self._cleanup()
                return False

    def stop(self):
        """Stop browsing and close Zeroconf."""
        with self._lock:
            self._cleanup()

    def _cleanup(self):
        """Internal cleanup — close browsers and Zeroconf instance."""
        for browser in self._browsers:
            try:
                browser.cancel()
            except Exception:
                pass
        self._browsers.clear()
        if self._zc:
            try:
                self._zc.close()
            except Exception:
                pass
            self._zc = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    # ── ServiceListener interface ─────────────────────────────────

    def add_service(self, zc, type_, name):
        """Called by Zeroconf when a new service is found."""
        try:
            info = zc.get_service_info(type_, name)
            if info and info.addresses:
                ip = socket.inet_ntoa(info.addresses[0])
                port = info.port or 8000
                logger.info(f"Zeroconf found: {name} at {ip}:{port}")
                if self.on_found:
                    self.on_found(ip, port, name)
        except Exception as e:
            logger.error(f"Error resolving service {name}: {e}")

    def update_service(self, zc, type_, name):
        """Called when a service is updated (ignored)."""
        pass

    def remove_service(self, zc, type_, name):
        """Called when a service is removed (logged only)."""
        logger.info(f"Zeroconf service removed: {name}")
