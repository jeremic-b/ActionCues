/**
 * ActionCues — Live Link Face Remote Control
 * Dashboard Frontend
 *
 * Two device states only: IDLE / RECORDING
 * No connection status. Commands always fire.
 */

// ══════════════════════════════════════════════════════════════════
// State
// ══════════════════════════════════════════════════════════════════

let ws = null;                  // WebSocket connection
let devices = [];               // current device list from server
let settings = {};              // server settings
let terminalEntries = [];       // system terminal log
const TERMINAL_MAX = 120;       // max terminal entries
let lastPollTime = 0;           // unix timestamp of last poll
let pollInterval = 30;          // poll interval in seconds

// ══════════════════════════════════════════════════════════════════
// WebSocket — real-time updates from server
// ══════════════════════════════════════════════════════════════════

function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws`);

    ws.onopen = () => {
        document.getElementById('wsDot').classList.add('connected');
        document.getElementById('wsLabel').textContent = 'Live';
        addTerminalEntry('success', 'WebSocket connected', 'SYSTEM');
    };

    ws.onclose = () => {
        document.getElementById('wsDot').classList.remove('connected');
        document.getElementById('wsLabel').textContent = 'Reconnecting';
        addTerminalEntry('warning', 'WebSocket lost — reconnecting...', 'SYSTEM');
        setTimeout(connectWS, 2000);
    };

    ws.onerror = () => ws.close();
    ws.onmessage = (e) => {
        try { handleMessage(JSON.parse(e.data)); }
        catch (err) { console.error('Bad WS message:', err); }
    };
}

// ══════════════════════════════════════════════════════════════════
// Message handler — dispatches all WebSocket messages
// ══════════════════════════════════════════════════════════════════

function handleMessage(msg) {
    switch (msg.type) {
        case 'init':
            devices = msg.devices || [];
            settings = msg.settings || {};
            if (msg.last_poll_time) lastPollTime = msg.last_poll_time;
            if (msg.poll_interval) pollInterval = msg.poll_interval;
            if (msg.slate) document.getElementById('slateInput').value = msg.slate;
            renderDevices();
            renderSettings();
            addTerminalEntry('info', `Init: ${devices.length} device(s), poll=${pollInterval}s`, 'SYSTEM');
            break;

        case 'device_update':
            devices = msg.devices || [];
            renderDevices();
            break;

        case 'recording_started':
            showNotification('info', `Recording started: ${msg.slate} — ${msg.results.length} device(s)`);
            if (msg.errors && msg.errors.length > 0) {
                msg.errors.forEach(err => showNotification('error', `${err.actor}: ${err.error}`));
            }
            break;

        case 'record_confirmed':
            showNotification('success', `RecordStartConfirm: ${msg.actor}`);
            break;

        case 'record_stop_confirmed':
            showNotification('success', `RecordStopConfirm: ${msg.actor} TC=${msg.timecode || 'N/A'}`);
            break;

        case 'slate_changed':
            document.getElementById('slateInput').value = msg.slate;
            break;

        case 'device_discovered':
            showNotification('info', `Device discovered at ${msg.device.ip}`);
            addTerminalEntry('info', `Discovered: ${msg.device.ip}`, 'DISCOVERY');
            break;

        case 'sys_terminal':
            addTerminalEntry(msg.level, msg.message, msg.device || '');
            break;

        case 'poll_tick':
            lastPollTime = msg.last_poll_time || 0;
            pollInterval = msg.poll_interval || 30;
            break;

        case 'alert':
            showNotification(msg.level || 'warning', msg.message);
            break;
    }
    updateStatus();
}

// ══════════════════════════════════════════════════════════════════
// Tabs
// ══════════════════════════════════════════════════════════════════

document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
        if (tab.dataset.tab === 'log') refreshLog();
        if (tab.dataset.tab === 'history') refreshHistory();
        if (tab.dataset.tab === 'settings') renderSettings();
    });
});

// ══════════════════════════════════════════════════════════════════
// Device rendering — builds the device card grid
// ══════════════════════════════════════════════════════════════════

function renderDevices() {
    const grid = document.getElementById('deviceGrid');
    const discoveredPanel = document.getElementById('discoveredPanel');
    const discoveredGrid = document.getElementById('discoveredGrid');

    const confirmed = devices.filter(d => d.actor_name);
    const discovered = devices.filter(d => !d.actor_name);

    // ── Discovered devices (unconfirmed) ─────────────────────
    if (discovered.length > 0) {
        discoveredPanel.style.display = 'block';
        discoveredGrid.innerHTML = discovered.map(dev => {
            const bat = dev.battery_percent < 0 ? '--' : `${dev.battery_percent}%`;
            const dn = dev.device_name
                ? `<div style="font-size:10px;color:var(--purple);margin-bottom:2px;">${esc(dev.device_name)}</div>`
                : '';
            return `
                <div class="discovered-card">
                    <div class="device-ip">${dev.ip}:${dev.port}</div>
                    ${dn}
                    <div style="font-size:11px;color:var(--text-dim);margin-bottom:4px;">
                        Battery: ${bat} | Last seen: ${timeSince(dev.last_seen)}
                    </div>
                    <div class="confirm-row">
                        <input type="text" id="confirm-name-${CSS.escape(dev.id)}" placeholder="Actor name" spellcheck="false">
                        <button class="btn-small btn-accent" onclick="confirmDevice('${escAttr(dev.id)}')">Confirm</button>
                        <button class="btn-small btn-danger" onclick="removeDevice('${escAttr(dev.id)}')">Dismiss</button>
                    </div>
                </div>`;
        }).join('');
    } else {
        discoveredPanel.style.display = 'none';
    }

    // ── Empty state ──────────────────────────────────────────
    if (confirmed.length === 0 && discovered.length === 0) {
        grid.innerHTML = `
            <div class="empty-state" style="grid-column:1/-1;">
                <h3>No Devices</h3>
                <p>Add a device manually or open Live Link Face pointing its OSC target here.</p>
                <p style="margin-top:8px;font-size:11px;color:var(--orange);">
                    Note: Start ActionCues before opening Live Link Face on your devices for automatic discovery.
                    Devices only broadcast their presence when the app is first opened.
                </p>
            </div>`;
        return;
    }

    if (confirmed.length === 0) { grid.innerHTML = ''; return; }

    // ── Confirmed device cards ───────────────────────────────
    grid.innerHTML = confirmed.map(dev => {
        const isRec = dev.is_recording;
        const statusClass = isRec ? 'status-recording' : 'status-idle';
        const badgeClass = isRec ? 'badge-recording' : 'badge-idle';
        const statusLabel = isRec ? 'RECORDING' : 'IDLE';

        // Battery color
        const batClass = dev.battery_percent < 0 ? '' :
            dev.battery_percent <= 15 ? 'battery-low' :
            dev.battery_percent <= 30 ? 'battery-mid' : 'battery-ok';
        const bat = dev.battery_percent < 0 ? '--' : `${dev.battery_percent}%`;

        // Recording info bar
        let recHtml = '';
        if (isRec) {
            const elapsed = dev.recording_start_time > 0
                ? formatDuration(Math.floor(Date.now() / 1000 - dev.recording_start_time))
                : '--:--:--';
            recHtml = `<div class="device-recording-info">
                <span>REC: ${esc(dev.current_slate)} — Take ${dev.current_take}</span>
                <span style="float:right;font-size:11px;">${elapsed}</span>
            </div>`;
        }

        // Device name line
        const deviceNameLine = dev.device_name
            ? `<span class="device-detail-label">Device</span>
               <span class="device-detail-value" style="color:var(--purple)">${esc(dev.device_name)}</span>`
            : '';

        // Action buttons — always show Stop, disable Ping/Rename/Remove during recording
        return `
            <div class="device-card ${statusClass}">
                <div class="device-header">
                    <span class="device-actor">${esc(dev.actor_name)}</span>
                    <span class="device-status-badge ${badgeClass}">${statusLabel}</span>
                </div>
                ${recHtml}
                <div class="device-details">
                    <span class="device-detail-label">IP</span>
                    <span class="device-detail-value">${dev.ip}:${dev.port}</span>
                    ${deviceNameLine}
                    <span class="device-detail-label">Battery</span>
                    <span class="device-detail-value ${batClass}">${bat}</span>
                    <span class="device-detail-label">Last Seen</span>
                    <span class="device-detail-value">${timeSince(dev.last_seen)}</span>
                </div>
                <div class="device-poll-bar">
                    <div class="device-poll-fill"></div>
                </div>
                <div class="device-actions">
                    <button class="btn-small btn-accent" onclick="recordDevice('${escAttr(dev.id)}')"
                        ${isRec ? 'disabled' : ''}>Record</button>
                    <button class="btn-small btn-danger" onclick="stopDevice('${escAttr(dev.id)}')">Stop</button>
                    ${isRec
                        ? `<button class="btn-small btn-warning" onclick="forceClearRecording('${escAttr(dev.id)}')">Force Clear</button>`
                        : ''
                    }
                    <button class="btn-small" onclick="pingDevice('${escAttr(dev.id)}')"
                        ${isRec ? 'disabled' : ''}>Ping</button>
                    <button class="btn-small" onclick="videoDisplayOff('${escAttr(dev.id)}')">Scrn Off</button>
                    <button class="btn-small" onclick="videoDisplayOn('${escAttr(dev.id)}')">Scrn On</button>
                    <button class="btn-small" onclick="renameDevice('${escAttr(dev.id)}', '${escAttr(dev.actor_name)}')"
                        ${isRec ? 'disabled' : ''}>Rename</button>
                    <button class="btn-small btn-danger" onclick="removeDevice('${escAttr(dev.id)}')"
                        ${isRec ? 'disabled' : ''}>Remove</button>
                </div>
            </div>`;
    }).join('');
}

// ══════════════════════════════════════════════════════════════════
// Helpers
// ══════════════════════════════════════════════════════════════════

/** Format seconds as HH:MM:SS */
function formatDuration(sec) {
    if (sec < 0) sec = 0;
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}

/** Human-readable time since a unix timestamp */
function timeSince(unixTs) {
    if (!unixTs || unixTs <= 0) return 'Never';
    const diff = Math.floor(Date.now() / 1000 - unixTs);
    if (diff < 5) return 'Just now';
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    return `${Math.floor(diff / 3600)}h ago`;
}

/** Escape HTML to prevent XSS */
function esc(str) {
    if (!str) return '';
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}

/** Escape for use inside onclick='...' attribute strings */
function escAttr(str) {
    return esc(str).replace(/'/g, '&#39;');
}

// ══════════════════════════════════════════════════════════════════
// Poll progress bar — visual countdown to next server poll
// ══════════════════════════════════════════════════════════════════

function updatePollProgress() {
    const now = Date.now() / 1000;
    const elapsed = now - lastPollTime;
    const pct = lastPollTime > 0 ? Math.min(100, (elapsed / pollInterval) * 100) : 0;
    const remaining = Math.max(0, Math.ceil(pollInterval - elapsed));

    const fill = document.getElementById('pollBarFill');
    const countdown = document.getElementById('pollCountdown');
    if (fill) fill.style.width = `${pct}%`;
    if (countdown) countdown.textContent = lastPollTime > 0 ? `${remaining}s` : '--s';

    // Also update per-device poll bars
    document.querySelectorAll('.device-poll-fill').forEach(el => {
        el.style.width = `${pct}%`;
    });
}

// ══════════════════════════════════════════════════════════════════
// System Terminal — scrolling log in Command Log tab
// ══════════════════════════════════════════════════════════════════

function addTerminalEntry(level, message, device) {
    const time = new Date().toTimeString().split(' ')[0];
    terminalEntries.push({ level, message, device: device || '', time });
    if (terminalEntries.length > TERMINAL_MAX) terminalEntries.shift();
    renderTerminal();
}

function renderTerminal() {
    const scroll = document.getElementById('sysTerminalScroll');
    if (!scroll) return;
    scroll.innerHTML = terminalEntries.map(e => {
        const cls = `term-${e.level}`;
        const pfx = getPrefix(e.level);
        const dev = e.device ? `<span class="term-device">[${esc(e.device)}]</span>` : '';
        return `<div class="term-line ${cls}">` +
            `<span class="term-time">${e.time}</span>` +
            `<span class="term-prefix">${pfx}</span>` +
            `${dev}<span class="term-msg">${esc(e.message)}</span></div>`;
    }).join('');
    scroll.scrollTop = scroll.scrollHeight;
}

function getPrefix(level) {
    const m = {
        osc_in:  '<span style="color:var(--green)">&lt;&lt; IN</span>',
        osc_out: '<span style="color:var(--accent)">&gt;&gt; OUT</span>',
        success: '<span style="color:var(--green)">[OK]</span>',
        warning: '<span style="color:var(--orange)">[WARN]</span>',
        error:   '<span style="color:var(--red)">[ERR]</span>',
        info:    '<span style="color:var(--text-dim)">[SYS]</span>',
    };
    return m[level] || m.info;
}

function clearTerminal() {
    terminalEntries = [];
    renderTerminal();
}

// ══════════════════════════════════════════════════════════════════
// Notifications — scrollable box, persistent, never deleted
// ══════════════════════════════════════════════════════════════════

function showNotification(level, message) {
    const feed = document.getElementById('notificationFeed');
    if (!feed) return;

    const el = document.createElement('div');
    el.className = `notif notif-${level}`;
    const time = new Date().toTimeString().split(' ')[0];
    el.innerHTML = `<span class="notif-time">${time}</span><span class="notif-msg">${esc(message)}</span>`;
    feed.appendChild(el);
    feed.scrollTop = feed.scrollHeight;
}

// ══════════════════════════════════════════════════════════════════
// API calls — all device and recording operations
// ══════════════════════════════════════════════════════════════════

async function api(path, body = null) {
    const opts = { headers: { 'Content-Type': 'application/json' } };
    if (body !== null) {
        opts.method = 'POST';
        opts.body = JSON.stringify(body);
    }
    const res = await fetch(`/api/${path}`, opts);
    return await res.json();
}

/** Add a device manually */
async function addDevice() {
    const ip = document.getElementById('addIp').value.trim();
    const port = parseInt(document.getElementById('addPort').value) || 8000;
    const actor = document.getElementById('addActor').value.trim();
    if (!ip) return showNotification('error', 'IP address is required');
    if (!actor) return showNotification('error', 'Actor name is required');
    try {
        const result = await api('devices/add', { ip, port, actor_name: actor });
        if (result.ok) {
            showNotification('success', `Added: ${actor} @ ${ip}:${port}`);
            document.getElementById('addIp').value = '';
            document.getElementById('addActor').value = '';
        } else {
            showNotification('error', result.error);
        }
    } catch (e) {
        showNotification('error', 'Failed to add device');
    }
}

/** Remove a device */
async function removeDevice(id) {
    if (!confirm('Remove this device?')) return;
    try {
        const r = await api('devices/remove', { device_id: id });
        if (!r.ok) showNotification('error', r.error || 'Failed');
    } catch (e) {
        showNotification('error', 'Failed to remove device');
    }
}

/** Rename a device's actor name */
async function renameDevice(id, cur) {
    const n = prompt('Enter new actor name:', cur);
    if (!n || n.trim() === cur) return;
    try {
        const r = await api('devices/rename', { device_id: id, actor_name: n.trim() });
        if (r.ok) showNotification('success', `Renamed: ${n.trim()}`);
        else showNotification('error', r.error);
    } catch (e) {
        showNotification('error', 'Failed to rename device');
    }
}

/** Ping a device (BatteryQuery + SetTarget if idle) */
async function pingDevice(id) {
    try {
        await api('devices/ping', { device_id: id });
        showNotification('info', 'Ping sent');
    } catch (e) {
        showNotification('error', 'Failed to ping device');
    }
}

/** Turn device screen on */
async function videoDisplayOn(id) {
    try {
        await api('devices/video-display', { device_ids: [id], enabled: true });
        showNotification('info', 'Screen On sent');
    } catch (e) {
        showNotification('error', 'Failed to send Screen On');
    }
}

/** Turn device screen off (saves battery) */
async function videoDisplayOff(id) {
    try {
        await api('devices/video-display', { device_ids: [id], enabled: false });
        showNotification('info', 'Screen Off sent');
    } catch (e) {
        showNotification('error', 'Failed to send Screen Off');
    }
}

/** Turn ALL device screens off */
async function videoDisplayOffAll() {
    try {
        await api('devices/video-display', { enabled: false });
        showNotification('info', 'Screen Off sent to all devices');
    } catch (e) {
        showNotification('error', 'Failed to send Screen Off');
    }
}

/** Turn ALL device screens on */
async function videoDisplayOnAll() {
    try {
        await api('devices/video-display', { enabled: true });
        showNotification('info', 'Screen On sent to all devices');
    } catch (e) {
        showNotification('error', 'Failed to send Screen On');
    }
}

/** Confirm a discovered device with an actor name */
async function confirmDevice(id) {
    const input = document.getElementById('confirm-name-' + CSS.escape(id));
    const name = input ? input.value.trim() : '';
    if (!name) return showNotification('error', 'Actor name is required');
    try {
        const r = await api('devices/confirm-discovered', { device_id: id, actor_name: name });
        if (r.ok) showNotification('success', `Confirmed: ${name}`);
        else showNotification('error', r.error);
    } catch (e) {
        showNotification('error', 'Failed to confirm device');
    }
}

/** Scan network via Zeroconf */
async function scanDevices() {
    await api('discovery/scan', {});
    showNotification('info', 'Scanning network...');
}

/** Record ALL confirmed devices */
async function recordAll() {
    const slate = document.getElementById('slateInput').value.trim();
    if (!slate) return showNotification('error', 'Enter a slate name');
    const takeStr = document.getElementById('takeOverride').value;
    try {
        const r = await api('record/start', { slate, take_override: takeStr ? parseInt(takeStr) : null });
        if (!r.ok) showNotification('error', r.error || 'Failed');
    } catch (e) {
        showNotification('error', 'Failed to start recording');
    }
}

/** Stop ALL confirmed devices */
async function stopAll() {
    try {
        await api('record/stop', {});
        showNotification('info', 'Stop sent to all devices');
    } catch (e) {
        showNotification('error', 'Failed to send stop');
    }
}

/** Record a single device */
async function recordDevice(id) {
    const slate = document.getElementById('slateInput').value.trim();
    if (!slate) return showNotification('error', 'Enter a slate name');
    const takeStr = document.getElementById('takeOverride').value;
    try {
        const r = await api('record/start', { slate, device_ids: [id], take_override: takeStr ? parseInt(takeStr) : null });
        if (!r.ok) showNotification('error', r.error || 'Failed');
    } catch (e) {
        showNotification('error', 'Failed to start recording');
    }
}

/** Stop a single device */
async function stopDevice(id) {
    try {
        await api('record/stop', { device_ids: [id] });
        showNotification('info', 'Stop sent to device');
    } catch (e) {
        showNotification('error', 'Failed to send stop');
    }
}

/** Force-clear a stuck recording state */
async function forceClearRecording(id) {
    if (!confirm('Force-clear recording state? Use only if the device stopped but the dashboard still shows RECORDING.')) return;
    try {
        const r = await api('record/force-clear', { device_id: id });
        if (r.ok) showNotification('warning', 'Recording force-cleared');
        else showNotification('error', r.error);
    } catch (e) {
        showNotification('error', 'Failed to force-clear');
    }
}

/** Restart the server process */
async function restartServer() {
    if (!confirm('Restart the server? WebSocket connections will drop and reconnect.')) return;
    try {
        await api('server/restart', {});
        showNotification('warning', 'Server restarting...');
    } catch (e) {
        showNotification('warning', 'Server restarting...');
    }
}

// ══════════════════════════════════════════════════════════════════
// Command Log
// ══════════════════════════════════════════════════════════════════

async function refreshLog() {
    const result = await api('log');
    const scroll = document.getElementById('logScroll');
    const entries = result.log || [];
    scroll.innerHTML = entries.map(e => `
        <div class="log-entry">
            <span class="log-time">${esc(e.timestamp.split('T')[1] || e.timestamp)}</span>
            <span class="log-dir ${e.direction.toLowerCase()}">${esc(e.direction)}</span>
            <span class="log-device">${esc(e.device || '--')}</span>
            <span class="log-addr">${esc(e.address)}</span>
            <span class="log-args">${esc(e.args.join(', '))}</span>
        </div>
    `).join('');
    if (document.getElementById('logAutoScroll').checked) {
        scroll.scrollTop = scroll.scrollHeight;
    }
}

async function clearLog() {
    if (!confirm('Clear the command log?')) return;
    await api('log/clear', {});
    refreshLog();
}

// ══════════════════════════════════════════════════════════════════
// Session History
// ══════════════════════════════════════════════════════════════════

async function refreshHistory() {
    const result = await api('history');
    const tbody = document.getElementById('historyBody');
    const entries = result.history || [];
    tbody.innerHTML = entries.reverse().map(e => {
        const time = e.timestamp ? e.timestamp.split('T')[1]?.split('.')[0] || e.timestamp : '--';
        const isStart = e.event === 'record_start';
        const label = isStart ? 'REC START' : 'REC STOP';
        const cls = isStart ? 'color:var(--red)' : 'color:var(--yellow)';
        const rowCls = isStart ? '' : 'class="history-stop-row"';
        const tc = e.timecode || '';
        return `<tr ${rowCls}>
            <td>${time}</td>
            <td style="${cls};font-weight:600">${label}</td>
            <td>${esc(e.slate || '')}</td>
            <td style="color:var(--accent)">${esc(e.actor || '')}</td>
            <td>${e.take || ''}</td>
            <td style="${tc ? 'color:var(--yellow)' : 'color:var(--text-dim)'}">${esc(tc) || '--'}</td>
            <td style="color:var(--text-dim)">${e.device_ip || ''}</td>
        </tr>`;
    }).join('');
}

async function clearHistory() {
    if (!confirm('Clear ALL session data? Resets take counters and history.')) return;
    await api('history/clear', {});
    refreshHistory();
    showNotification('info', 'Session cleared');
}

// ══════════════════════════════════════════════════════════════════
// Settings
// ══════════════════════════════════════════════════════════════════

const SETTING_META = {
    osc_listen_port:          { label: 'OSC Listen Port',     desc: 'UDP port for incoming OSC messages',       type: 'number' },
    osc_default_device_port:  { label: 'Default Device Port', desc: 'Default OSC port for new devices',         type: 'number' },
    http_port:                { label: 'HTTP Port',           desc: 'Dashboard port (restart required)',         type: 'number' },
    confirm_timeout_sec:      { label: 'Confirm Timeout',    desc: 'Seconds to wait for confirm',              type: 'number' },
    battery_poll_interval_sec:{ label: 'Poll Interval',      desc: 'Seconds between battery/keepalive polls',  type: 'number' },
    auto_discover_devices:    { label: 'Auto-Discover',      desc: 'Auto-detect devices sending OSC',           type: 'checkbox' },
};

function renderSettings() {
    const grid = document.getElementById('settingsGrid');
    grid.innerHTML = Object.entries(SETTING_META).map(([key, meta]) => {
        const val = settings[key] !== undefined ? settings[key] : '';
        if (meta.type === 'checkbox') {
            return `<div class="setting-item"><div class="setting-info">
                <span class="setting-name">${meta.label}</span>
                <span class="setting-desc">${meta.desc}</span>
            </div><input type="checkbox" class="setting-input" data-key="${key}" ${val ? 'checked' : ''}></div>`;
        }
        return `<div class="setting-item"><div class="setting-info">
            <span class="setting-name">${meta.label}</span>
            <span class="setting-desc">${meta.desc}</span>
        </div><input type="${meta.type}" class="setting-input" data-key="${key}" value="${val}" style="width:80px"></div>`;
    }).join('');
}

async function saveSettings() {
    const updates = {};
    document.querySelectorAll('.setting-input').forEach(input => {
        const key = input.dataset.key;
        let val = input.type === 'checkbox' ? input.checked :
                  input.type === 'number' ? parseInt(input.value) : input.value;
        if (settings[key] !== val) updates[key] = val;
    });
    if (!Object.keys(updates).length) return showNotification('info', 'No changes');
    try {
        const r = await api('settings', updates);
        if (r.ok) {
            settings = r.settings;
            pollInterval = settings.battery_poll_interval_sec || pollInterval;
            document.getElementById('settingsSaved').style.display = 'inline';
            setTimeout(() => document.getElementById('settingsSaved').style.display = 'none', 4000);
            showNotification('success', 'Settings saved');
        } else {
            showNotification('error', r.error);
        }
    } catch (e) {
        showNotification('error', 'Failed to save settings');
    }
}

// ══════════════════════════════════════════════════════════════════
// Server status bar — updates every 5 seconds
// ══════════════════════════════════════════════════════════════════

async function updateStatus() {
    try {
        const s = await api('status');
        const ipEl = document.getElementById('statusIp');
        ipEl.textContent = s.server_ip;
        if (s.ip_warning) {
            ipEl.style.color = 'var(--red)';
            ipEl.title = s.ip_warning;
            if (!window._ipWarningShown) {
                showNotification('error', s.ip_warning);
                window._ipWarningShown = true;
            }
        } else {
            ipEl.style.color = '';
            ipEl.title = '';
        }
        document.getElementById('statusOscPort').textContent = s.osc_listen_port;
        document.getElementById('statusHttpPort').textContent = s.http_port;
        document.getElementById('statusUptime').textContent = formatDuration(s.uptime_sec);
        const recCount = s.recording_devices || 0;
        const totalCount = s.total_devices || 0;
        document.getElementById('statusDevices').textContent =
            `${totalCount} device(s)` + (recCount > 0 ? ` (${recCount} recording)` : '');
        document.getElementById('statusOsc').textContent = s.osc_running ? 'Running' : 'Stopped';
        document.getElementById('statusOsc').style.color = s.osc_running ? 'var(--green)' : 'var(--red)';
        document.getElementById('statusSlate').textContent = s.current_slate || '--';
    } catch (e) { /* reconnecting */ }
}

// ══════════════════════════════════════════════════════════════════
// Keyboard shortcuts
// ══════════════════════════════════════════════════════════════════

document.addEventListener('keydown', (e) => {
    // Escape = emergency stop all
    if (e.key === 'Escape') {
        if (devices.some(d => d.is_recording)) { stopAll(); e.preventDefault(); }
    }
});

// Enter in slate input = set slate
document.getElementById('slateInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
        const slate = e.target.value.trim();
        if (slate) api('slate/set', { slate });
    }
});

// ══════════════════════════════════════════════════════════════════
// Theme — light/dark mode toggle
// ══════════════════════════════════════════════════════════════════

function toggleTheme() {
    const isLight = document.documentElement.classList.toggle('light-mode');
    localStorage.setItem('actioncues-theme', isLight ? 'light' : 'dark');
    updateThemeButton();
}

function updateThemeButton() {
    const btn = document.getElementById('themeToggle');
    if (!btn) return;
    const isLight = document.documentElement.classList.contains('light-mode');
    btn.textContent = isLight ? 'Dark' : 'Light';
}

// ══════════════════════════════════════════════════════════════════
// Init — start WebSocket, status polling, poll progress, theme
// ══════════════════════════════════════════════════════════════════

updateThemeButton();
connectWS();
updateStatus();
setInterval(updateStatus, 5000);
setInterval(updatePollProgress, 1000);
