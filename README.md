<img width="1521" height="900" alt="acimage" src="https://github.com/user-attachments/assets/238b8e18-d793-4120-bba0-8f2ce6dcb80e" />
# ActionCues — Live Link Face Remote Control

Production-grade OSC remote controller for Epic Games' **Live Link Face** iOS app. Control multiple devices simultaneously from a real-time web dashboard on any machine on your local network.

Built for film, motion capture, and virtual production workflows where reliable multi-device recording control is critical.

> **Recommended setup:** ActionCues is designed for use in an **offline environment** — a dedicated laptop connected to a router with only your recording devices on the network. This ensures maximum stability and eliminates network interference during long recording sessions.

## Features

- **Multi-device control** — Record and stop multiple devices running Live Link Face at once
- **Two states only** — Devices are either IDLE or RECORDING. No connection complexity
- **Unique filenames** — Each device gets `SlateName_ActorName` for organized file management
- **Auto-discovery** — Finds devices via Zeroconf/Bonjour and OSC auto-detection
- **Session management** — Automatic take numbering, recording history with timecodes
- **Remote display control** — Turn device screens on/off remotely to save battery
- **Real-time dashboard** — WebSocket-powered UI with live status, battery levels, and command log
- **Card & list view** — Toggle between card grid and compact list view for the device panel
- **Light & dark mode** — Switch themes from the header (persisted across sessions)
- **Recording lock** — Optionally lock slate/take inputs while any device is recording
- **Keepalive polling** — Prevents device timeouts during long recording sessions
- **Cross-platform** — Runs on macOS and Windows (Python 3.9+)

## Quick Start

### 1. Install Python 3.9+

- **macOS**: `brew install python3` or download from [python.org](https://www.python.org/downloads/)
- **Windows**: Download from [python.org](https://www.python.org/downloads/) (check "Add to PATH" during install)

### 2. Install dependencies

```bash
cd ActionCues
pip3 install -r requirements.txt
```

### 3. Run the server

```bash
python3 server.py
```

### 4. Open the dashboard

Navigate to **http://localhost:7100** in any browser on your network.

### 5. Connect your devices

> **Important:** Start ActionCues *before* opening Live Link Face on your devices.
> Devices only broadcast their presence when the app is first opened. If a device was
> launched before the server, you will need to add it manually or restart Live Link Face.

**Option A — Auto-discover**: Open Live Link Face on your device, set the OSC target to this computer's IP (shown in the dashboard status bar), port 8000. The device will appear automatically in the Discovered Devices panel.

**Option B — Manual**: Enter the device's IP address and an actor name in the dashboard's Add Device panel.

## How It Works

```
┌─────────────┐     OSC/UDP      ┌──────────────┐
│  Dashboard   │◄── WebSocket ──►│   Server     │
│  (Browser)   │                 │  (Python)    │
└─────────────┘                 └──────┬───────┘
                                       │ OSC/UDP
                          ┌────────────┼────────────┐
                          ▼            ▼            ▼
                     ┌────────┐  ┌────────┐  ┌────────┐
                     │Device 1│  │Device 2│  │Device 3│
                     └────────┘  └────────┘  └────────┘
```

The server communicates with Live Link Face devices over **OSC (Open Sound Control)** via UDP. The web dashboard connects to the server via WebSocket for real-time status updates. All commands fire unconditionally to all confirmed devices — there is no connection handshake or status gating.

### OSC Commands

| Command | Direction | Description |
|---------|-----------|-------------|
| `/OSCSetSendTarget` | Server → Device | Tell device where to send responses |
| `/Slate` | Server → Device | Set the filename (slate_actorname) |
| `/RecordStart` | Server → Device | Start recording with slate_actorname + take |
| `/RecordStop` | Server → Device | Stop recording |
| `/BatteryQuery` | Server → Device | Request battery level |
| `/VideoDisplayOn` | Server → Device | Turn on device screen |
| `/VideoDisplayOff` | Server → Device | Turn off device screen (saves battery) |
| `/RecordStartConfirm` | Device → Server | Device confirmed recording started |
| `/RecordStopConfirm` | Device → Server | Device confirmed recording stopped |
| `/SlateConfirm` | Device → Server | Device confirmed slate was set |
| `/Battery` / `/BatteryResponse` | Device → Server | Battery level response |
| `/Alive` | Device → Server | Heartbeat |

## Project Structure

```
ActionCues/
├── server.py          # FastAPI server, API endpoints, WebSocket, poll loop
├── osc_engine.py      # OSC send/receive, UDP listener, command log
├── device_manager.py  # Device CRUD, state management, persistence
├── session.py         # Slate naming, take numbering, recording history
├── config.py          # Persistent settings (data/settings.json)
├── discovery.py       # Zeroconf/Bonjour network discovery
├── requirements.txt   # Python dependencies
├── LICENSE            # MIT License
├── static/
│   ├── index.html     # Dashboard HTML
│   ├── app.js         # Dashboard JavaScript
│   └── style.css      # Dashboard styles
├── data/              # Runtime data (auto-created, gitignored)
│   ├── devices.json   # Saved devices
│   ├── settings.json  # User settings
│   ├── takes.json     # Take counters
│   └── session_history.jsonl
└── logs/              # Server logs (auto-created, gitignored)
```

## Settings

Configurable via the Settings tab in the dashboard:

| Setting | Default | Description |
|---------|---------|-------------|
| OSC Listen Port | 8000 | UDP port for incoming device messages |
| Default Device Port | 8000 | Default port when adding devices |
| HTTP Port | 7100 | Dashboard web server port |
| Confirm Timeout | 3s | Wait time for device confirmation |
| Poll Interval | 30s | Keepalive/battery poll frequency |
| Auto-Discover | On | Detect devices sending OSC to us |
| Lock During Recording | On | Disable slate/take inputs while recording |
| Keyboard Shortcuts | Off | Enable keyboard shortcuts (see below) |

## Keyboard Shortcuts

Keyboard shortcuts are **disabled by default** to prevent accidental disruptions during long recording sessions. Enable them in Settings.

| Key | Action |
|-----|--------|
| `Escape` | Emergency stop all recordings |
| `Enter` (in slate field) | Set the slate name (always active) |

## Troubleshooting

**Devices not appearing?**
- Start ActionCues before opening Live Link Face on your devices
- Ensure device and computer are on the same WiFi network
- For best connection stability, use a **5 GHz WiFi network** — 2.4 GHz is more congested and prone to latency/dropouts during recording sessions
- In Live Link Face, set OSC target to this computer's IP, port 8000
- Check your firewall allows UDP port 8000
- If the device was opened first, restart Live Link Face or add it manually

**Windows: Devices not responding?**
- Windows Firewall blocks incoming UDP traffic by default. When you first run ActionCues, Windows may show a firewall popup — click **"Allow access"**
- If you missed the popup, open PowerShell as Administrator and run:
  ```powershell
  New-NetFirewallRule -DisplayName "ActionCues OSC" -Direction Inbound -Protocol UDP -LocalPort 8000 -Action Allow
  ```
- Alternatively, go to Windows Defender Firewall → Allow an app → find Python and allow it on Private networks

**Commands not working?**
- Ping the device first to establish the OSC connection
- Check the Command Log tab for outgoing/incoming messages
- Try restarting the server via Settings → Restart Server

**Stuck in RECORDING state?**
- Use the "Force Clear" button on the device card
- This only clears the dashboard state — the device may still be recording

**Device manually stopped from the phone?**
- Live Link Face does not send a stop confirmation when recording is ended
  directly on the device. Use Force Clear to reset the dashboard state.

## Disclaimer

This software is an independent open-source project and is not affiliated with, endorsed by, or associated with Epic Games, Inc. "Live Link Face" and "Unreal Engine" are trademarks of Epic Games, Inc. All trademarks are the property of their respective owners.

## License

MIT License — see [LICENSE](LICENSE) for details.

---

Created by [jeremic-b](https://github.com/jeremic-b)
