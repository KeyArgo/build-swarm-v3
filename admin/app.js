/**
 * Build Swarm v3 — Admin Dashboard
 * Pure vanilla JS. No frameworks. No build step.
 */

// ── Constants ──
// Use same hostname as current page but on port 8100
// When running locally, fall back to the production control plane
const V3_HOST = location.hostname === 'localhost' || location.hostname === '127.0.0.1'
  ? '10.0.0.199' : location.hostname;
const V3_API = `http://${V3_HOST}:8100/api/v1`;
const ADMIN_API = '/admin/api';
const REFRESH_MS = 5000;

// ── State ──
let adminKey = sessionStorage.getItem('admin_key') || '';
let connected = false;
let refreshTimer = null;

// Chart state
let queueChart = null;
let queueChartData = [[], [], [], []]; // [timestamps, queue_depth, received, blocked]
let buildRateChart = null;
let buildRateData = [[], [], []]; // [timestamps, success/min, failed/min]
const MAX_CHART_POINTS = 120; // 10 minutes at 5s interval

// ── DOM helpers ──
const $ = (sel, ctx) => (ctx || document).querySelector(sel);
const $$ = (sel, ctx) => [...(ctx || document).querySelectorAll(sel)];

// ── API helpers ──

async function v3Get(path) {
  try {
    const res = await fetch(`${V3_API}${path}`, {
      headers: { 'Accept': 'application/json' },
    });
    if (!res.ok) { console.warn('v3Get failed:', path, res.status); return null; }
    return await res.json();
  } catch (e) { console.error('v3Get error:', path, e); return null; }
}

async function v3Post(path, body) {
  try {
    const res = await fetch(`${V3_API}${path}`, {
      method: 'POST',
      headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) { console.warn('v3Post failed:', path, res.status); return null; }
    return await res.json();
  } catch (e) { console.error('v3Post error:', path, e); return null; }
}

async function adminGet(path) {
  try {
    const res = await fetch(`${ADMIN_API}${path}`, {
      headers: { 'Accept': 'application/json', 'X-Admin-Key': adminKey },
    });
    if (res.status === 401) { showLogin(); return null; }
    if (!res.ok) { console.warn('adminGet failed:', path, res.status); return null; }
    return await res.json();
  } catch (e) { console.error('adminGet error:', path, e); return null; }
}

async function adminPost(path, body) {
  try {
    const res = await fetch(`${ADMIN_API}${path}`, {
      method: 'POST',
      headers: {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'X-Admin-Key': adminKey,
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (res.status === 401) { showLogin(); return null; }
    if (!res.ok) { console.warn('adminPost failed:', path, res.status); return null; }
    return await res.json();
  } catch (e) { console.error('adminPost error:', path, e); return null; }
}

// ── Auth ──

function showLogin() {
  $('.login-overlay').classList.remove('hidden');
  $('.dashboard').style.display = 'none';
  $('#login-key').focus();
}

function hideLogin() {
  $('.login-overlay').classList.add('hidden');
  $('.dashboard').style.display = 'flex';
}

async function tryLogin() {
  const input = $('#login-key');
  const err = $('.login-error');
  const key = input.value.trim();
  if (!key) return;

  adminKey = key;
  const result = await adminGet('/auth/check');
  if (result && result.authenticated) {
    sessionStorage.setItem('admin_key', key);
    err.style.display = 'none';
    hideLogin();
    startRefresh();
  } else {
    adminKey = '';
    err.textContent = 'Invalid key. Check /etc/build-swarm/admin.key';
    err.style.display = 'block';
    input.select();
  }
}

// ── Tabs ──

function initTabs() {
  $$('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      $$('.tab').forEach(t => t.classList.remove('active'));
      $$('.tab-content').forEach(c => c.classList.remove('active'));
      tab.classList.add('active');
      const panel = $(`#tab-${tab.dataset.tab}`);
      if (panel) panel.classList.add('active');
    });
  });
}

// ── Formatting helpers ──

function fmtDuration(seconds) {
  if (!seconds || seconds < 0) return '-';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function fmtTime(ts) {
  if (!ts) return '-';
  const d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
  return d.toLocaleTimeString('en-US', { hour12: false });
}

function fmtPct(val) {
  if (val == null || val === undefined) return '-';
  return `${Math.round(val)}%`;
}

// ── Connection status ──

function setConnected(online) {
  connected = online;
  const dot = $('.connection-dot');
  const banner = $('.offline-banner');
  if (dot) dot.classList.toggle('offline', !online);
  if (banner) banner.classList.toggle('visible', !online);
}

// ── Refresh loop ──

async function refresh() {
  // Fetch v3 status
  const status = await v3Get('/status');
  console.log('refresh: status =', status ? 'OK' : 'NULL', status ? `nodes=${status.nodes}` : '');
  if (!status) {
    setConnected(false);
    return;
  }
  setConnected(true);

  // Update header
  const ver = $('.header-version');
  if (ver) ver.textContent = `v${status.version || '3.1.0'}`;

  // ── Row 1: Live counters ──
  updateStatCard('drones-online',
    `${status.nodes_online || 0}/${status.nodes || 0}`, 'cyan');
  updateStatCard('total-cores', status.total_cores ?? 0, 'purple');
  updateStatCard('queue-depth', status.queue_depth ?? 0,
    status.queue_depth > 50 ? 'amber' : 'cyan');
  updateStatCard('received', status.queue_received ?? 0, 'green');
  updateStatCard('blocked', status.queue_blocked ?? 0,
    status.queue_blocked > 0 ? 'red' : 'green');

  // ── Row 2: Build stats ──
  const t = status.timing || {};
  updateStatCard('success-rate',
    t.total_builds ? `${Math.round(t.success_rate || 0)}%` : '-',
    (t.success_rate || 0) >= 90 ? 'green' : (t.success_rate || 0) >= 70 ? 'amber' : 'red');
  updateStatCard('avg-build', fmtDuration(t.avg_duration_s), '');
  updateStatCard('total-builds', t.total_builds ?? 0, '');
  updateStatCard('total-failed', t.failed ?? 0,
    (t.failed || 0) > 0 ? 'red' : 'green');
  updateStatCard('total-duration', fmtDuration(t.total_duration_s), '');

  // ── Progress bar ──
  const rcv = status.queue_received || 0;
  const dlg = status.delegated || 0;
  const ndd = status.needed || 0;
  const blk = status.queue_blocked || 0;
  const total = rcv + dlg + ndd + blk;

  if (total > 0) {
    setProgress('received', rcv / total * 100);
    setProgress('delegated', dlg / total * 100);
    setProgress('needed', ndd / total * 100);
    setProgress('blocked', blk / total * 100);
  } else {
    setProgress('received', 0);
    setProgress('delegated', 0);
    setProgress('needed', 0);
    setProgress('blocked', 0);
  }
  setText('#progress-received-n', rcv ? `(${rcv})` : '');
  setText('#progress-delegated-n', dlg ? `(${dlg})` : '');
  setText('#progress-needed-n', ndd ? `(${ndd})` : '');
  setText('#progress-blocked-n', blk ? `(${blk})` : '');

  // ── Session info ──
  if (status.session) {
    const s = status.session;
    setText('#session-name', s.name || s.id || '-');
    setText('#session-progress', `${s.completed || 0} / ${s.total_packages || 0}`);
    const elapsed = s.started_at ? (Date.now() / 1000 - s.started_at) : 0;
    setText('#session-elapsed', fmtDuration(elapsed));
    setText('#session-state', status.paused ? 'Paused' : 'Active');
  } else {
    setText('#session-name', 'No active session');
    setText('#session-progress', '-');
    setText('#session-elapsed', '-');
    setText('#session-state', status.paused ? 'Paused' : 'Idle');
  }

  // ── Push chart data ──
  const now = Date.now() / 1000;
  pushChartPoint(queueChartData, [now, status.queue_depth || 0, rcv, blk]);
  updateQueueChart();

  // ── Tab-specific refreshes ──
  const active = document.querySelector('.tab-content.active');
  if (!active) return;
  const tab = active.id.replace('tab-', '');

  if (tab === 'fleet') await refreshFleet();
  else if (tab === 'drone-mgmt') await refreshDroneConfigs();
  else if (tab === 'binhost') await refreshBinhost();
  else if (tab === 'queue') await refreshQueue();
  else if (tab === 'history') await refreshHistory();
  else if (tab === 'topology') await refreshTopology();
  else if (tab === 'wire') await refreshWire();
  else if (tab === 'events') await refreshEvents();
}

function updateStatCard(id, value, colorClass) {
  const el = $(`#stat-${id}`);
  if (!el) return;
  const valEl = el.querySelector('.value');
  if (valEl) {
    valEl.textContent = value;
    valEl.className = `value ${colorClass}`;
  }
}

function setProgress(cls, pct) {
  const el = $(`.progress-bar .${cls}`);
  if (el) el.style.width = `${pct}%`;
}

function setText(sel, text) {
  const el = $(sel);
  if (el) el.textContent = text;
}

// ── Charts ──

function pushChartPoint(data, point) {
  for (let i = 0; i < data.length; i++) {
    data[i].push(point[i]);
    if (data[i].length > MAX_CHART_POINTS) data[i] = data[i].slice(-MAX_CHART_POINTS);
  }
}

function makeChartOpts(seriesDefs, container) {
  return {
    width: container.clientWidth - 8,
    height: 230,
    cursor: { show: true },
    scales: {
      x: { time: true },
      y: { min: 0 },
    },
    axes: [
      {
        stroke: '#64748b',
        grid: { stroke: 'rgba(255,255,255,0.04)', width: 1 },
        ticks: { stroke: 'rgba(255,255,255,0.06)', width: 1 },
        font: '10px JetBrains Mono, monospace',
      },
      {
        stroke: '#64748b',
        grid: { stroke: 'rgba(255,255,255,0.04)', width: 1 },
        ticks: { stroke: 'rgba(255,255,255,0.06)', width: 1 },
        font: '10px JetBrains Mono, monospace',
        size: 50,
      },
    ],
    series: [{}, ...seriesDefs],
  };
}

function updateQueueChart() {
  const container = $('#chart-queue');
  if (!container) return;
  if (queueChartData[0].length < 2) return;

  if (!queueChart) {
    const opts = makeChartOpts([
      { label: 'Queue', stroke: '#06b6d4', width: 2, fill: 'rgba(6,182,212,0.1)' },
      { label: 'Received', stroke: '#22c55e', width: 2, fill: 'rgba(34,197,94,0.08)' },
      { label: 'Blocked', stroke: '#f59e0b', width: 2, fill: 'rgba(245,158,11,0.08)' },
    ], container);
    queueChart = new uPlot(opts, queueChartData, container);
  } else {
    queueChart.setData(queueChartData);
  }
}

async function updateBuildRateChart() {
  const container = $('#chart-buildrate');
  if (!container) return;

  const history = await v3Get('/history?limit=500');
  if (!history || !history.history || !history.history.length) {
    if (!buildRateChart) {
      container.innerHTML = '<div class="empty-state" style="padding:2rem">No build history yet</div>';
    }
    return;
  }

  // Bucket builds by minute
  const buckets = {};
  for (const b of history.history) {
    const ts = Math.floor((b.built_at || 0) / 60) * 60;
    if (!ts) continue;
    if (!buckets[ts]) buckets[ts] = { ok: 0, fail: 0 };
    if (b.status === 'success') buckets[ts].ok++;
    else buckets[ts].fail++;
  }

  const times = Object.keys(buckets).map(Number).sort();
  if (times.length < 2) return;

  buildRateData = [
    times,
    times.map(t => buckets[t].ok),
    times.map(t => buckets[t].fail),
  ];

  if (!buildRateChart) {
    const stepped = (u, si, i0, i1) => uPlot.paths.stepped({ align: 1 })(u, si, i0, i1);
    const opts = makeChartOpts([
      { label: 'OK/min', stroke: '#22c55e', width: 2, fill: 'rgba(34,197,94,0.15)', paths: stepped },
      { label: 'Fail/min', stroke: '#ef4444', width: 2, fill: 'rgba(239,68,68,0.12)', paths: stepped },
    ], container);
    buildRateChart = new uPlot(opts, buildRateData, container);
  } else {
    buildRateChart.setData(buildRateData);
  }
}

// ── Fleet tab ──

async function refreshFleet() {
  // Fetch v3 nodes + v2 nodes + drone health in parallel
  const [v3Nodes, v2Data, healthData, droneConfigs] = await Promise.all([
    v3Get('/nodes?all=true'),
    adminGet('/v2/nodes'),
    v3Get('/drone-health'),
    adminGet('/drone-configs'),
  ]);

  const v3List = Array.isArray(v3Nodes) ? v3Nodes : [];
  const v2Drones = v2Data?.drones || [];
  const v2Orchestrators = v2Data?.orchestrators || [];
  const v2All = [...v2Drones, ...v2Orchestrators];
  const healthMap = {};
  if (healthData?.drones) {
    for (const d of healthData.drones) healthMap[d.drone_id || d.name] = d;
  }
  const configMap = {};
  if (Array.isArray(droneConfigs)) {
    for (const c of droneConfigs) configMap[c.node_name] = c;
  }

  // Summary cards
  const totalOnline = v3List.filter(n => n.status === 'online').length +
                      v2Drones.filter(n => n.online || n.status === 'online').length;
  const grounded = Object.values(healthMap).filter(h => h.grounded_until).length;
  updateStatCard('fleet-total', v3List.length + v2All.length, 'cyan');
  updateStatCard('fleet-online', totalOnline, totalOnline > 0 ? 'green' : 'red');
  updateStatCard('fleet-v3-count', v3List.length, 'cyan');
  updateStatCard('fleet-v2-count', v2All.length, v2All.length > 0 ? 'amber' : 'green');
  updateStatCard('fleet-grounded', grounded, grounded > 0 ? 'red' : 'green');

  // ── V3 table ──
  const v3Tbody = $('#fleet-v3-tbody');
  if (v3Tbody) {
    const sorted = v3List.sort((a, b) => {
      if (a.status === 'online' && b.status !== 'online') return -1;
      if (a.status !== 'online' && b.status === 'online') return 1;
      return (a.name || '').localeCompare(b.name || '');
    });

    if (sorted.length === 0) {
      v3Tbody.innerHTML = '<tr><td colspan="12" class="empty-state">No v3 drones registered</td></tr>';
    } else {
      v3Tbody.innerHTML = sorted.map(n => {
        const h = healthMap[n.id] || healthMap[n.name] || {};
        const cfg = configMap[n.name] || {};
        const isGrounded = h.grounded_until && h.grounded_until > Date.now() / 1000;
        const isPaused = n.paused;

        let healthBadge = '<span class="badge online">ok</span>';
        if ((h.upload_failures || 0) >= 3) healthBadge = '<span class="badge grounded">upload err</span>';
        else if (isGrounded) healthBadge = '<span class="badge grounded">grounded</span>';
        else if ((h.failures || 0) > 0) healthBadge = `<span class="badge offline">${h.failures} fails</span>`;

        const lockBadge = cfg.locked ? ' <span class="badge locked" title="Bloat locked">L</span>' : '';
        const protBadge = cfg.protected ? ' <span class="badge v3" title="Protected">P</span>' : '';

        const metrics = n.metrics || {};
        const caps = n.capabilities || {};

        return `<tr>
          <td><strong>${n.name || n.id}</strong>${lockBadge}${protBadge}</td>
          <td class="mono">${n.ip || '-'}</td>
          <td><span class="badge ${n.status || 'offline'}">${isPaused ? 'paused' : (n.status || 'offline')}</span></td>
          <td>${healthBadge}</td>
          <td>${caps.cores || n.cores || '-'}</td>
          <td>${fmtPct(metrics.cpu_percent ?? n.cpu_percent)}</td>
          <td>${fmtPct(metrics.ram_percent ?? n.ram_percent)}</td>
          <td>${metrics.load_1m != null ? metrics.load_1m.toFixed(1) : '-'}</td>
          <td class="mono" style="font-size:0.72rem;max-width:180px;overflow:hidden;text-overflow:ellipsis">${n.current_task || '<span style="color:var(--text-muted)">idle</span>'}</td>
          <td><span style="color:var(--green)">${n.builds_completed || 0}</span>/<span style="color:var(--red)">${n.builds_failed || 0}</span></td>
          <td>${n.version || '-'}</td>
          <td>
            <div class="btn-group">
              ${isPaused
                ? `<button class="btn success" onclick="droneAction('${n.id}','resume')" title="Resume">Resume</button>`
                : `<button class="btn" onclick="droneAction('${n.id}','pause')" title="Pause">Pause</button>`
              }
              ${isGrounded
                ? `<button class="btn success" onclick="droneAction('${n.id}','unground')" title="Unground">Unground</button>`
                : ''
              }
              <button class="btn danger" onclick="droneAction('${n.id}','delete')" title="Remove">X</button>
            </div>
          </td>
        </tr>`;
      }).join('');
    }
  }

  // ── V2 table ──
  const v2Tbody = $('#fleet-v2-tbody');
  if (v2Tbody) {
    if (v2All.length === 0) {
      v2Tbody.innerHTML = '<tr><td colspan="9" class="empty-state">No v2 nodes (gateway may be offline)</td></tr>';
    } else {
      v2Tbody.innerHTML = v2All.map(n => {
        const m = n.metrics || {};
        const statusClass = (n.online || n.status === 'online') ? 'online' : 'offline';
        return `<tr>
          <td><strong>${n.name || n.id}</strong></td>
          <td class="mono">${n.ip || '-'}</td>
          <td><span class="badge ${statusClass}">${n.status || (n.online ? 'online' : 'offline')}</span></td>
          <td><span class="badge v2">${n.type || 'drone'}</span></td>
          <td>${fmtPct(m.cpu_percent)}</td>
          <td>${fmtPct(m.ram_percent)}</td>
          <td>${m.load_1m != null ? m.load_1m.toFixed(1) : '-'}</td>
          <td class="mono" style="font-size:0.72rem">${n.current_task || '<span style="color:var(--text-muted)">idle</span>'}</td>
          <td>${n.version || '-'}</td>
        </tr>`;
      }).join('');
    }
  }
}

async function droneAction(id, action) {
  if (action === 'delete') {
    if (!confirm(`Remove drone ${id} from the fleet? It can re-register on next heartbeat.`)) return;
    await fetch(`${V3_API}/nodes/${encodeURIComponent(id)}`, { method: 'DELETE' });
  } else if (action === 'pause') {
    await v3Post(`/nodes/${encodeURIComponent(id)}/pause`);
  } else if (action === 'resume') {
    await v3Post(`/nodes/${encodeURIComponent(id)}/resume`);
  } else if (action === 'unground') {
    await v3Post('/control', { action: 'unground', drone_id: id });
  }
  setTimeout(refresh, 500);
}

// ── Events tab ──

async function refreshEvents() {
  const filter = ($('#events-filter') || {}).value || '';
  const params = filter ? `?type=${filter}&limit=200` : '?limit=200';
  const data = await v3Get(`/events/history${params}`);
  if (!data) return;

  const events = data.events || [];
  const feed = $('#events-feed');
  if (!feed) return;

  if (events.length === 0) {
    feed.innerHTML = '<div class="empty-state">No events recorded yet</div>';
    return;
  }

  feed.innerHTML = events.map(e => {
    const type = e.event_type || e.type || '';
    const badgeClass = (type === 'fail' || type === 'blocked' || type === 'grounded') ? 'offline' :
                       (type === 'complete' || type === 'register') ? 'online' : 'v3';
    return `<div class="activity-item">
      <span class="time">${fmtTime(e.timestamp || e.created_at)}</span>
      <span class="badge ${badgeClass}">${type}</span>
      <span class="msg">${e.message || ''}</span>
    </div>`;
  }).join('');
}

// ── Control actions ──

async function controlAction(action, btn) {
  // Check for confirmation
  if (btn && btn.dataset.confirm) {
    if (!confirm(btn.dataset.confirm)) return;
  }

  // Find the nearest result display
  const resultEl = btn ? btn.closest('.section-card')?.querySelector('.ctrl-result') : null;
  const fallbackEl = $('#ctrl-result');
  const display = resultEl || fallbackEl;

  if (display) {
    display.textContent = `Sending ${action}...`;
    display.style.color = 'var(--cyan)';
  }

  const result = await v3Post('/control', { action });
  if (display) {
    if (result) {
      // Format result nicely
      const parts = Object.entries(result)
        .filter(([k]) => k !== 'status')
        .map(([k, v]) => `${k}: ${v}`);
      const statusText = result.status || 'ok';
      display.textContent = `${action} → ${statusText}${parts.length ? ' (' + parts.join(', ') + ')' : ''}`;
      display.style.color = 'var(--green)';
    } else {
      display.textContent = `${action}: failed`;
      display.style.color = 'var(--red)';
    }
  }
  setTimeout(refresh, 500);
}

// ── Drone Management tab ──

async function refreshDroneConfigs() {
  const configs = await adminGet('/drone-configs');
  if (!configs) return;

  const tbody = $('#drone-config-tbody');
  if (!tbody) return;

  if (!Array.isArray(configs) || configs.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty-state">No drone configs. Add one below.</td></tr>';
    return;
  }

  tbody.innerHTML = configs.map(c => `
    <tr>
      <td><strong>${c.display_name || c.node_name}</strong>${c.display_name ? `<br><span style="color:var(--text-muted);font-size:0.7rem">${c.node_name}</span>` : ''}</td>
      <td class="mono">${c.ssh_user || 'root'}</td>
      <td>${c.ssh_port || 22}</td>
      <td>${c.ssh_key_path ? '<span class="badge v3">key</span>' : c.ssh_password ? '<span class="badge v2">pass</span>' : '<span class="badge">default</span>'}</td>
      <td>${c.cores_limit || '<span style="color:var(--text-muted)">all</span>'}</td>
      <td>${c.emerge_jobs || 2}</td>
      <td><span class="badge ${c.control_plane || 'v3'}">${c.control_plane || 'v3'}</span></td>
      <td><span class="badge ${c.locked ? 'locked' : 'unlocked'}">${c.locked ? 'locked' : 'unlocked'}</span></td>
      <td>
        <button class="btn" onclick="editDroneConfig('${c.node_name}')">Edit</button>
      </td>
    </tr>
  `).join('');
}

async function editDroneConfig(name) {
  const config = await adminGet(`/drone-config/${encodeURIComponent(name)}`);
  if (!config) return;

  // Populate form
  $('#dc-node-name').value = config.node_name || name;
  $('#dc-ssh-user').value = config.ssh_user || '';
  $('#dc-ssh-port').value = config.ssh_port || '';
  $('#dc-ssh-key').value = config.ssh_key_path || '';
  $('#dc-ssh-pass').value = config.ssh_password || '';
  $('#dc-cores').value = config.cores_limit || '';
  $('#dc-jobs').value = config.emerge_jobs || '';
  $('#dc-ram').value = config.ram_limit_gb || '';
  $('#dc-auto-reboot').value = config.auto_reboot != null ? String(config.auto_reboot) : '1';
  $('#dc-protected').value = config.protected != null ? String(config.protected) : '0';
  $('#dc-max-failures').value = config.max_failures || '';
  $('#dc-locked').value = config.locked != null ? String(config.locked) : '1';
  $('#dc-display-name').value = config.display_name || '';
  $('#dc-v2-name').value = config.v2_name || '';
  $('#dc-control-plane').value = config.control_plane || 'v3';
  $('#dc-binhost-url').value = config.binhost_upload_url || '';
  $('#dc-notes').value = config.notes || '';

  // Show editor
  $('#drone-editor-title').textContent = `Edit: ${config.display_name || name}`;
  $('#drone-config-editor').style.display = 'block';
  $('#dc-save-status').textContent = '';
  $('#dc-delete-btn').style.display = config._unconfigured ? 'none' : 'inline-flex';

  // Scroll to editor
  $('#drone-config-editor').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function closeDroneEditor() {
  $('#drone-config-editor').style.display = 'none';
}

async function saveDroneConfig(e) {
  e.preventDefault();
  const name = $('#dc-node-name').value;
  if (!name) return;

  const fields = {};

  // Only send non-empty fields
  const strField = (id, key) => {
    const val = $(id).value.trim();
    fields[key] = val || null;
  };
  const intField = (id, key) => {
    const val = $(id).value.trim();
    fields[key] = val ? parseInt(val, 10) : null;
  };
  const floatField = (id, key) => {
    const val = $(id).value.trim();
    fields[key] = val ? parseFloat(val) : null;
  };
  const selectField = (id, key) => {
    fields[key] = $(id).value;
  };

  strField('#dc-ssh-user', 'ssh_user');
  intField('#dc-ssh-port', 'ssh_port');
  strField('#dc-ssh-key', 'ssh_key_path');
  strField('#dc-ssh-pass', 'ssh_password');
  intField('#dc-cores', 'cores_limit');
  intField('#dc-jobs', 'emerge_jobs');
  floatField('#dc-ram', 'ram_limit_gb');
  selectField('#dc-auto-reboot', 'auto_reboot');
  selectField('#dc-protected', 'protected');
  intField('#dc-max-failures', 'max_failures');
  selectField('#dc-locked', 'locked');
  strField('#dc-display-name', 'display_name');
  strField('#dc-v2-name', 'v2_name');
  selectField('#dc-control-plane', 'control_plane');
  strField('#dc-binhost-url', 'binhost_upload_url');
  strField('#dc-notes', 'notes');

  // Convert select string values to int
  fields.auto_reboot = parseInt(fields.auto_reboot, 10);
  fields.protected = parseInt(fields.protected, 10);
  fields.locked = parseInt(fields.locked, 10);

  const statusEl = $('#dc-save-status');
  statusEl.textContent = 'Saving...';
  statusEl.style.color = 'var(--cyan)';

  const result = await adminPost(`/drone-config/${encodeURIComponent(name)}`, fields);
  if (result && !result.error) {
    statusEl.textContent = 'Saved successfully.';
    statusEl.style.color = 'var(--green)';
    refreshDroneConfigs();
  } else {
    statusEl.textContent = `Error: ${result?.error || 'Failed to save'}`;
    statusEl.style.color = 'var(--red)';
  }
}

async function deleteDroneConfig() {
  const name = $('#dc-node-name').value;
  if (!name) return;
  if (!confirm(`Delete config for "${name}"? This cannot be undone.`)) return;

  try {
    const res = await fetch(`${ADMIN_API}/drone-config/${encodeURIComponent(name)}`, {
      method: 'DELETE',
      headers: { 'X-Admin-Key': adminKey },
    });
    if (res.ok) {
      closeDroneEditor();
      refreshDroneConfigs();
    }
  } catch {}
}

async function addNewDroneConfig() {
  const input = $('#new-drone-name');
  const name = input.value.trim();
  if (!name) return;

  // Create with defaults, then open editor
  const result = await adminPost(`/drone-config/${encodeURIComponent(name)}`, {
    ssh_user: 'root',
    ssh_port: 22,
    locked: 1,
  });
  if (result && !result.error) {
    input.value = '';
    await refreshDroneConfigs();
    editDroneConfig(name);
  }
}

// ── Binhost tab ──

async function refreshBinhost() {
  const data = await adminGet('/releases');
  if (!data) return;

  const releases = data.releases || [];
  const active = releases.find(r => r.status === 'active');
  const staging = await v3Get('/binhost-stats');

  // Update stat cards
  updateStatCard('rel-active', active ? active.version : 'None', active ? 'cyan' : 'red');
  updateStatCard('rel-staging', staging ? staging.packages : '-', '');
  updateStatCard('rel-total', releases.length, '');

  const totalMB = releases.reduce((sum, r) => sum + (r.size_mb || 0), 0);
  updateStatCard('rel-disk', totalMB > 1024 ? `${(totalMB/1024).toFixed(1)} GB` : `${Math.round(totalMB)} MB`, '');

  // Show migrate button if no releases exist
  const migrateBtn = $('#migrate-btn');
  if (migrateBtn) migrateBtn.style.display = releases.length === 0 ? 'inline-block' : 'none';

  // Render releases table
  const tbody = $('#releases-tbody');
  if (!tbody) return;

  if (releases.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No releases. Click "Migrate to Release System" to start.</td></tr>';
    return;
  }

  tbody.innerHTML = releases.map(r => {
    const statusClass = r.status === 'active' ? 'online' :
                        r.status === 'staging' ? 'v3' :
                        r.status === 'archived' ? 'v2' : 'offline';
    let actions = '';
    if (r.status === 'staging') {
      actions += `<button class="btn success" onclick="promoteRelease('${esc(r.version)}')">Promote</button> `;
      actions += `<button class="btn danger" onclick="deleteRelease('${esc(r.version)}')">Delete</button> `;
    } else if (r.status === 'archived') {
      actions += `<button class="btn" onclick="promoteRelease('${esc(r.version)}')">Promote</button> `;
      actions += `<button class="btn danger" onclick="deleteRelease('${esc(r.version)}')">Delete</button> `;
    } else if (r.status === 'active') {
      actions += `<button class="btn" onclick="archiveRelease('${esc(r.version)}')">Archive</button> `;
    }
    actions += `<button class="btn" onclick="browseRelease('${esc(r.version)}')">Browse</button>`;
    if (active && r.version !== active.version) {
      actions += ` <button class="btn" onclick="diffReleases('${esc(active.version)}','${esc(r.version)}')">Diff</button>`;
    }
    return `<tr>
      <td><strong>${esc(r.version)}</strong>${r.name ? `<br><span style="color:var(--text-dim);font-size:0.7rem">${esc(r.name)}</span>` : ''}</td>
      <td><span class="badge ${statusClass}">${r.status}</span></td>
      <td>${r.package_count || 0}</td>
      <td>${r.size_mb ? Math.round(r.size_mb) + ' MB' : '-'}</td>
      <td style="white-space:nowrap">${fmtTime(r.created_at)}</td>
      <td style="white-space:nowrap">${r.promoted_at ? fmtTime(r.promoted_at) : '-'}</td>
      <td style="white-space:nowrap">${actions}</td>
    </tr>`;
  }).join('');
}

function esc(s) { return String(s).replace(/'/g, "\\'").replace(/"/g, '&quot;').replace(/</g, '&lt;'); }

async function createRelease() {
  const name = prompt('Release version (leave blank for auto YYYY.MM.DD):');
  if (name === null) return;
  const notes = prompt('Release notes (optional):');
  const body = {};
  if (name) body.version = name;
  if (notes) body.notes = notes;
  showReleaseResult('Creating release...', 'cyan');
  const result = await adminPost('/releases', body);
  if (result && result.status === 'ok') {
    showReleaseResult(`Created release ${result.version} (${result.package_count} packages, ${result.size_mb} MB)`, 'green');
  } else {
    showReleaseResult(`Error: ${result?.error || 'Failed'}`, 'red');
  }
  setTimeout(refresh, 500);
}

async function promoteRelease(version) {
  if (!confirm(`Promote release "${version}" to active? This will switch what nginx serves.`)) return;
  showReleaseResult(`Promoting ${version}...`, 'cyan');
  const result = await adminPost(`/releases/${encodeURIComponent(version)}/promote`);
  if (result && result.status === 'ok') {
    showReleaseResult(`Promoted ${version} to active`, 'green');
  } else {
    showReleaseResult(`Error: ${result?.error || 'Failed'}`, 'red');
  }
  setTimeout(refresh, 500);
}

async function archiveRelease(version) {
  showReleaseResult(`Archiving ${version}...`, 'cyan');
  const result = await adminPost(`/releases/${encodeURIComponent(version)}/archive`);
  showReleaseResult(result?.status === 'ok' ? `Archived ${version}` : `Error: ${result?.error || 'Failed'}`,
                    result?.status === 'ok' ? 'green' : 'red');
  setTimeout(refresh, 500);
}

async function deleteRelease(version) {
  if (!confirm(`Delete release "${version}"? This removes the directory from disk.`)) return;
  showReleaseResult(`Deleting ${version}...`, 'cyan');
  const result = await adminDelete(`/releases/${encodeURIComponent(version)}`);
  showReleaseResult(result?.status === 'ok' ? `Deleted ${version}` : `Error: ${result?.error || 'Failed'}`,
                    result?.status === 'ok' ? 'green' : 'red');
  setTimeout(refresh, 500);
}

async function rollbackRelease() {
  if (!confirm('Rollback to the previous active release?')) return;
  showReleaseResult('Rolling back...', 'cyan');
  const result = await adminPost('/releases/rollback');
  showReleaseResult(result?.status === 'ok' ? `Rolled back to ${result.version}` : `Error: ${result?.error || 'Failed'}`,
                    result?.status === 'ok' ? 'green' : 'red');
  setTimeout(refresh, 500);
}

async function migrateReleases() {
  if (!confirm('Migrate /var/cache/binpkgs to the release-based system? This is a one-time operation.')) return;
  showReleaseResult('Migrating...', 'cyan');
  const result = await adminPost('/releases/migrate');
  if (result?.status === 'ok') {
    showReleaseResult(`Migrated: initial release (${result.package_count} packages, ${result.size_mb} MB)`, 'green');
  } else {
    showReleaseResult(`Error: ${result?.error || 'Failed'}`, 'red');
  }
  setTimeout(refresh, 1000);
}

async function browseRelease(version) {
  const data = await adminGet(`/releases/${encodeURIComponent(version)}/packages`);
  if (!data) return;
  const browser = $('#release-pkg-browser');
  if (browser) browser.style.display = 'block';
  setText('#release-pkg-version', version);
  const tbody = $('#release-pkg-tbody');
  const packages = data.packages || [];
  if (!tbody) return;
  if (packages.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-state">No packages found</td></tr>';
    return;
  }
  tbody.innerHTML = packages.map(p => `<tr>
    <td>${esc(p.category)}</td>
    <td>${esc(p.package)}</td>
    <td class="mono">${esc(p.version)}</td>
    <td>${(p.size_bytes / 1048576).toFixed(1)} MB</td>
  </tr>`).join('');
}

function closePackageBrowser() {
  const el = $('#release-pkg-browser');
  if (el) el.style.display = 'none';
}

async function diffReleases(from, to) {
  const data = await adminGet(`/releases/diff?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`);
  if (!data || data.error) return;
  const viewer = $('#release-diff-viewer');
  if (viewer) viewer.style.display = 'block';
  setText('#diff-from', from);
  setText('#diff-to', to);

  const s = data.summary || {};
  const statsEl = $('#diff-stats');
  if (statsEl) {
    statsEl.innerHTML = `
      <div class="stat-card"><div class="label">Added</div><div class="value green">${s.added || 0}</div></div>
      <div class="stat-card"><div class="label">Removed</div><div class="value red">${s.removed || 0}</div></div>
      <div class="stat-card"><div class="label">Changed</div><div class="value amber">${s.changed || 0}</div></div>
      <div class="stat-card"><div class="label">Unchanged</div><div class="value">${s.unchanged || 0}</div></div>
    `;
  }

  let html = '';
  if (data.added && data.added.length > 0) {
    html += '<h4 style="color:var(--green)">+ Added</h4><ul>';
    data.added.forEach(p => { html += `<li>${esc(p.category)}/${esc(p.package)}-${esc(p.version)}</li>`; });
    html += '</ul>';
  }
  if (data.removed && data.removed.length > 0) {
    html += '<h4 style="color:var(--red)">- Removed</h4><ul>';
    data.removed.forEach(p => { html += `<li>${esc(p.category)}/${esc(p.package)}-${esc(p.version)}</li>`; });
    html += '</ul>';
  }
  if (data.changed && data.changed.length > 0) {
    html += '<h4 style="color:var(--amber)">~ Changed</h4><ul>';
    data.changed.forEach(p => { html += `<li>${esc(p.category)}/${esc(p.package)}: ${esc(p.from_version)} &rarr; ${esc(p.to_version)}</li>`; });
    html += '</ul>';
  }
  if (!html) html = '<p style="color:var(--text-dim)">No differences found</p>';
  const contentEl = $('#diff-content');
  if (contentEl) contentEl.innerHTML = html;
}

function closeDiffViewer() {
  const el = $('#release-diff-viewer');
  if (el) el.style.display = 'none';
}

async function adminDelete(path) {
  try {
    const res = await fetch(`${ADMIN_API}${path}`, {
      method: 'DELETE',
      headers: { 'Accept': 'application/json', 'X-Admin-Key': adminKey },
    });
    if (res.status === 401) { showLogin(); return null; }
    if (!res.ok) { console.warn('adminDelete failed:', path, res.status); return null; }
    return await res.json();
  } catch (e) { console.error('adminDelete error:', path, e); return null; }
}

function showReleaseResult(message, color) {
  const el = $('#release-result');
  if (!el) return;
  el.textContent = message;
  el.style.color = `var(--${color})`;
}

// ── Queue tab ──

async function refreshQueue() {
  const queue = await v3Get('/queue');
  if (!queue) return;

  // Queue stat cards
  const counts = { needed: 0, delegated: 0, received: 0, blocked: 0, failed: 0 };
  for (const p of queue) counts[p.status] = (counts[p.status] || 0) + 1;
  updateStatCard('q-needed', counts.needed, 'cyan');
  updateStatCard('q-delegated', counts.delegated, 'purple');
  updateStatCard('q-received', counts.received, 'green');
  updateStatCard('q-blocked', counts.blocked + counts.failed, counts.blocked + counts.failed > 0 ? 'red' : 'green');
  updateStatCard('q-total', queue.length, '');

  const tbody = $('#queue-tbody');
  if (!tbody) return;

  const filter = ($('#queue-filter') || {}).value || '';
  const filtered = filter ? queue.filter(p => p.status === filter) : queue;

  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty-state">${queue.length ? 'No matches for filter' : 'Queue empty'}</td></tr>`;
    return;
  }

  // Sort: blocked/failed first, then delegated, then needed, then received
  const order = { blocked: 0, failed: 1, delegated: 2, needed: 3, received: 4 };
  const sorted = filtered.sort((a, b) => (order[a.status] ?? 5) - (order[b.status] ?? 5));

  tbody.innerHTML = sorted.map(p => {
    const statusClass = p.status === 'received' ? 'online' :
                        p.status === 'blocked' || p.status === 'failed' ? 'offline' :
                        p.status === 'delegated' ? 'v3' : '';
    // Per-package actions based on status
    let actions = '';
    if (p.status === 'blocked' || p.status === 'failed') {
      actions = `<button class="btn" onclick="pkgAction('unblock','${p.package}')" title="Unblock">Retry</button>`;
    } else if (p.status === 'delegated') {
      actions = `<button class="btn" onclick="pkgAction('reclaim','${p.package}')" title="Reclaim back to needed">Reclaim</button>`;
    } else if (p.status === 'needed') {
      actions = `<button class="btn danger" onclick="pkgAction('block','${p.package}')" title="Block this package">Block</button>`;
    }
    return `<tr>
      <td class="mono" style="font-size:0.78rem">${p.package}</td>
      <td><span class="badge ${statusClass}">${p.status}</span></td>
      <td>${p.assigned_to || '<span style="color:var(--text-muted)">-</span>'}</td>
      <td>${p.failures || 0}</td>
      <td>${actions}</td>
    </tr>`;
  }).join('');
}

async function pkgAction(action, pkg) {
  const result = await v3Post('/control', { action, package: pkg });
  if (result) {
    setTimeout(refresh, 300);
  }
}

// ── History tab ──

async function refreshHistory() {
  const data = await v3Get('/history?limit=500');
  if (!data) return;

  const s = data.stats || {};
  updateStatCard('hist-total', s.total_builds ?? 0, '');
  updateStatCard('hist-success', s.successful ?? 0, 'green');
  updateStatCard('hist-failed', s.failed ?? 0, (s.failed || 0) > 0 ? 'red' : 'green');
  updateStatCard('hist-rate', s.total_builds ? `${Math.round(s.success_rate || 0)}%` : '-',
    (s.success_rate || 0) >= 90 ? 'green' : 'amber');
  updateStatCard('hist-avg', fmtDuration(s.avg_duration_s), '');

  const tbody = $('#history-tbody');
  if (!tbody) return;

  const history = data.history || [];
  if (history.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-state">No build history</td></tr>';
    return;
  }

  // Populate drone filter dropdown (once)
  const droneFilter = $('#history-drone-filter');
  if (droneFilter && droneFilter.options.length <= 1) {
    const drones = [...new Set(history.map(h => h.drone_name || h.drone_id).filter(Boolean))].sort();
    for (const d of drones) {
      const opt = document.createElement('option');
      opt.value = d;
      opt.textContent = d;
      droneFilter.appendChild(opt);
    }
  }

  // Apply filters
  const statusFilter = ($('#history-status-filter') || {}).value || '';
  const droneFilterVal = (droneFilter || {}).value || '';
  let filtered = history;
  if (statusFilter) filtered = filtered.filter(h => h.status === statusFilter);
  if (droneFilterVal) filtered = filtered.filter(h => (h.drone_name || h.drone_id) === droneFilterVal);

  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-state">${history.length ? 'No matches for filter' : 'No build history'}</td></tr>`;
    return;
  }

  tbody.innerHTML = filtered.map(h => {
    const statusClass = h.status === 'success' ? 'online' : 'offline';
    return `<tr>
      <td class="mono" style="font-size:0.78rem">${h.package}</td>
      <td>${h.drone_name || h.drone_id || '-'}</td>
      <td><span class="badge ${statusClass}">${h.status}</span></td>
      <td>${fmtDuration(h.duration_s || h.duration_seconds)}</td>
      <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;font-size:0.72rem;color:var(--red)">${h.error_message || ''}</td>
      <td style="white-space:nowrap">${fmtTime(h.built_at)}</td>
    </tr>`;
  }).join('');
}

// ── Wire (Protocol Inspector) tab ──

async function refreshWire() {
  const filter = ($('#wire-filter') || {}).value || '';
  const params = filter ? `?type=${filter}&limit=200` : '?limit=200';
  const data = await v3Get(`/protocol${params}`);
  if (!data || !data.entries) return;

  const tbody = $('#wire-tbody');
  if (!tbody) return;

  if (data.entries.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty-state">No protocol entries</td></tr>';
    return;
  }

  tbody.innerHTML = data.entries.map(e => {
    const methodClass = e.method === 'POST' ? 'color:var(--amber)' : 'color:var(--cyan)';
    const statusColor = (e.status_code || 0) >= 400 ? 'color:var(--red)' :
                        (e.status_code || 0) >= 300 ? 'color:var(--amber)' : 'color:var(--green)';
    const latencyColor = (e.latency_ms || 0) > 100 ? 'color:var(--red)' :
                         (e.latency_ms || 0) > 20 ? 'color:var(--amber)' : '';
    return `<tr>
      <td style="white-space:nowrap">${fmtTime(e.timestamp)}</td>
      <td class="mono" style="font-size:0.72rem">${e.source_ip || '-'}</td>
      <td style="${methodClass};font-weight:600">${e.method}</td>
      <td class="mono" style="font-size:0.72rem;max-width:200px;overflow:hidden;text-overflow:ellipsis">${e.path}</td>
      <td><span class="badge v3">${e.msg_type || '-'}</span></td>
      <td style="${statusColor};font-weight:600">${e.status_code || '-'}</td>
      <td class="mono" style="${latencyColor}">${e.latency_ms != null ? e.latency_ms.toFixed(1) + 'ms' : '-'}</td>
      <td>${e.drone_id || ''}</td>
      <td class="mono" style="font-size:0.72rem">${e.package || ''}</td>
    </tr>`;
  }).join('');
}

// ── Data (SQL Explorer) tab ──

async function loadSQLTables() {
  const data = await v3Get('/sql/tables');
  if (!data || !data.tables) return;

  const el = $('#sql-tables');
  if (!el) return;

  el.innerHTML = 'Tables: ' + Object.entries(data.tables)
    .map(([name, count]) => `<button class="btn" style="padding:0.15rem 0.5rem;font-size:0.72rem;margin:0.15rem" onclick="document.getElementById('sql-query').value='SELECT * FROM ${name} LIMIT 50';runSQL()">${name} (${count})</button>`)
    .join('');
}

async function runSQL() {
  const input = $('#sql-query');
  const query = (input ? input.value : '').trim();
  if (!query) return;

  // Safety: only SELECT
  if (!query.toUpperCase().startsWith('SELECT')) {
    const tbody = $('#sql-tbody');
    if (tbody) tbody.innerHTML = '<tr><td class="empty-state" style="color:var(--red)">Only SELECT queries are allowed</td></tr>';
    return;
  }

  const data = await v3Get(`/sql/query?q=${encodeURIComponent(query)}`);
  const thead = $('#sql-thead');
  const tbody = $('#sql-tbody');
  if (!thead || !tbody) return;

  if (data && data.error) {
    tbody.innerHTML = `<tr><td class="empty-state" style="color:var(--red)">${data.error}</td></tr>`;
    thead.innerHTML = '<tr></tr>';
    return;
  }

  if (!data || !data.rows || data.rows.length === 0) {
    tbody.innerHTML = '<tr><td class="empty-state">No results</td></tr>';
    thead.innerHTML = '<tr></tr>';
    return;
  }

  const cols = data.columns || Object.keys(data.rows[0]);
  thead.innerHTML = '<tr>' + cols.map(c => `<th>${c}</th>`).join('') + '</tr>';
  tbody.innerHTML = data.rows.map(row => {
    const cells = cols.map(c => {
      const val = row[c];
      if (val === null) return '<td style="color:var(--text-muted)">NULL</td>';
      return `<td class="mono" style="font-size:0.72rem">${String(val).substring(0, 200)}</td>`;
    }).join('');
    return `<tr>${cells}</tr>`;
  }).join('');
}

// ── Topology tab ──

async function refreshTopology() {
  const [status, v2Data] = await Promise.all([
    v3Get('/status'),
    adminGet('/v2/nodes'),
  ]);

  const container = $('#topology-svg');
  if (!container) return;

  const drones = status?.drones ? Object.entries(status.drones) : [];
  const v2Nodes = [...(v2Data?.drones || []), ...(v2Data?.orchestrators || [])];

  // Build SVG
  const W = 700, H = 400;
  let svg = `<svg viewBox="0 0 ${W} ${H}" width="${W}" style="max-width:100%">`;
  svg += `<style>
    text { fill: #94a3b8; font-family: 'JetBrains Mono', monospace; font-size: 11px; }
    .node-box { rx: 6; ry: 6; }
    .label { font-size: 9px; fill: #64748b; }
  </style>`;

  // Control plane (center top)
  svg += `<rect x="${W/2-60}" y="20" width="120" height="40" class="node-box" fill="#0f172a" stroke="#06b6d4" stroke-width="1.5"/>`;
  svg += `<text x="${W/2}" y="44" text-anchor="middle" fill="#06b6d4" font-weight="600">Control Plane</text>`;
  svg += `<text x="${W/2}" y="14" text-anchor="middle" class="label">:8100 + :8093</text>`;

  // Binhosts (center)
  svg += `<rect x="${W/2-130}" y="100" width="110" height="35" class="node-box" fill="#0f172a" stroke="#22c55e" stroke-width="1"/>`;
  svg += `<text x="${W/2-75}" y="122" text-anchor="middle" fill="#22c55e">Primary BH</text>`;
  svg += `<rect x="${W/2+20}" y="100" width="110" height="35" class="node-box" fill="#0f172a" stroke="#8b5cf6" stroke-width="1"/>`;
  svg += `<text x="${W/2+75}" y="122" text-anchor="middle" fill="#8b5cf6">Secondary BH</text>`;

  // Lines: CP to binhosts
  svg += `<line x1="${W/2}" y1="60" x2="${W/2-75}" y2="100" stroke="#22c55e" stroke-width="0.5" opacity="0.4"/>`;
  svg += `<line x1="${W/2}" y1="60" x2="${W/2+75}" y2="100" stroke="#8b5cf6" stroke-width="0.5" opacity="0.4"/>`;
  // Line between binhosts (rsync)
  svg += `<line x1="${W/2-20}" y1="117" x2="${W/2+20}" y2="117" stroke="#f59e0b" stroke-width="0.5" stroke-dasharray="4,3" opacity="0.5"/>`;
  svg += `<text x="${W/2}" y="112" text-anchor="middle" class="label" fill="#f59e0b">rsync</text>`;

  // V3 drones
  const droneY = 200;
  const droneCount = drones.length || 1;
  const spacing = Math.min(140, (W - 100) / Math.max(droneCount, 1));
  const startX = (W - (droneCount - 1) * spacing) / 2;

  drones.forEach(([id, d], i) => {
    const x = startX + i * spacing;
    const online = d.status === 'online';
    const color = online ? '#06b6d4' : '#ef4444';
    svg += `<line x1="${W/2}" y1="60" x2="${x}" y2="${droneY}" stroke="${color}" stroke-width="0.5" opacity="0.3"/>`;
    svg += `<rect x="${x-50}" y="${droneY}" width="100" height="35" class="node-box" fill="#0f172a" stroke="${color}" stroke-width="1"/>`;
    svg += `<text x="${x}" y="${droneY+15}" text-anchor="middle" fill="${color}" font-weight="500">${d.name || id}</text>`;
    svg += `<text x="${x}" y="${droneY+27}" text-anchor="middle" class="label">${d.ip || ''}</text>`;
  });

  if (drones.length === 0) {
    svg += `<text x="${W/2}" y="${droneY+15}" text-anchor="middle" class="label">No v3 drones registered</text>`;
  }

  // V2 nodes
  const v2Y = 310;
  const v2Count = v2Nodes.length || 1;
  const v2Spacing = Math.min(140, (W - 100) / Math.max(v2Count, 1));
  const v2StartX = (W - (v2Count - 1) * v2Spacing) / 2;

  if (v2Nodes.length > 0) {
    svg += `<text x="${W/2}" y="${v2Y - 10}" text-anchor="middle" class="label" fill="#f59e0b">V2 Legacy</text>`;
    v2Nodes.forEach((n, i) => {
      const x = v2StartX + i * v2Spacing;
      const online = n.online || n.status === 'online';
      const color = online ? '#f59e0b' : '#ef4444';
      svg += `<rect x="${x-50}" y="${v2Y}" width="100" height="35" class="node-box" fill="#0f172a" stroke="${color}" stroke-width="1" stroke-dasharray="4,2"/>`;
      svg += `<text x="${x}" y="${v2Y+15}" text-anchor="middle" fill="${color}">${n.name || n.id}</text>`;
      svg += `<text x="${x}" y="${v2Y+27}" text-anchor="middle" class="label">${n.type || 'drone'}</text>`;
    });
  }

  svg += '</svg>';
  container.innerHTML = svg;
}

// ── System info ──

async function refreshSystemInfo() {
  const info = await adminGet('/system/info');
  if (!info) return;
  const el = $('#system-info-content');
  if (!el) return;
  el.innerHTML = `
    <table>
      <tr><td class="mono" style="color:var(--text-muted)">Version</td><td>${info.version}</td></tr>
      <tr><td class="mono" style="color:var(--text-muted)">Uptime</td><td>${info.uptime_human}</td></tr>
      <tr><td class="mono" style="color:var(--text-muted)">Database</td><td class="mono">${info.db_path} (${info.db_size_mb} MB)</td></tr>
      <tr><td class="mono" style="color:var(--text-muted)">Control Plane</td><td>:${info.control_plane_port}</td></tr>
      <tr><td class="mono" style="color:var(--text-muted)">Admin Dashboard</td><td>:${info.admin_port}</td></tr>
      <tr><td class="mono" style="color:var(--text-muted)">V2 Gateway</td><td class="mono">${info.v2_gateway_url}</td></tr>
      <tr><td class="mono" style="color:var(--text-muted)">Binhost Primary</td><td class="mono">${info.binhost_primary_ip}</td></tr>
      <tr><td class="mono" style="color:var(--text-muted)">Binhost Secondary</td><td class="mono">${info.binhost_secondary_ip}</td></tr>
    </table>
  `;
}

// ── Startup ──

let refreshCount = 0;
function startRefresh() {
  console.log('startRefresh: V3_API =', V3_API, 'ADMIN_API =', ADMIN_API);
  refresh();
  refreshSystemInfo();
  updateBuildRateChart();
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(() => {
    refreshCount++;
    refresh();
    // Update build rate chart every 6th cycle (30s)
    if (refreshCount % 6 === 0) updateBuildRateChart();
    // Update system info every 12th cycle (60s)
    if (refreshCount % 12 === 0) refreshSystemInfo();
  }, REFRESH_MS);
}

// Debounced resize for charts
let resizeTimeout;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimeout);
  resizeTimeout = setTimeout(() => {
    if (queueChart) {
      const c = $('#chart-queue');
      if (c) queueChart.setSize({ width: c.clientWidth - 8, height: 230 });
    }
    if (buildRateChart) {
      const c = $('#chart-buildrate');
      if (c) buildRateChart.setSize({ width: c.clientWidth - 8, height: 230 });
    }
  }, 200);
});

document.addEventListener('DOMContentLoaded', () => {
  initTabs();

  // Login form
  $('#login-btn').addEventListener('click', tryLogin);
  $('#login-key').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') tryLogin();
  });

  // Keyboard shortcuts: 1-9,0 for tabs, R for refresh
  document.addEventListener('keydown', (e) => {
    // Skip if typing in an input
    if (e.target.matches('input, textarea, select')) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;

    const tabKeys = ['1','2','3','4','5','6','7','8','9','0'];
    const idx = tabKeys.indexOf(e.key);
    if (idx >= 0) {
      const tabs = $$('.tab');
      if (idx < tabs.length) {
        tabs[idx].click();
        e.preventDefault();
      }
    }
    if (e.key === 'r' || e.key === 'R') {
      refresh();
      e.preventDefault();
    }
  });

  // Control buttons
  $$('[data-action]').forEach(btn => {
    btn.addEventListener('click', () => controlAction(btn.dataset.action, btn));
  });

  // Drone config form
  const dcForm = $('#drone-config-form');
  if (dcForm) dcForm.addEventListener('submit', saveDroneConfig);
  const dcDelete = $('#dc-delete-btn');
  if (dcDelete) dcDelete.addEventListener('click', deleteDroneConfig);

  // Filter change handlers — trigger immediate refresh
  ['queue-filter', 'wire-filter', 'events-filter', 'history-status-filter', 'history-drone-filter'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', () => refresh());
  });

  // SQL Enter key
  const sqlInput = $('#sql-query');
  if (sqlInput) {
    sqlInput.addEventListener('keydown', e => { if (e.key === 'Enter') runSQL(); });
    loadSQLTables();
  }

  // Check if we have a stored key
  if (adminKey) {
    adminGet('/auth/check').then(result => {
      if (result && result.authenticated) {
        hideLogin();
        startRefresh();
      } else {
        adminKey = '';
        sessionStorage.removeItem('admin_key');
        showLogin();
      }
    });
  } else {
    showLogin();
  }
});
