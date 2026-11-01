function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[ch]));
}
function escapedView(value) {
    if (typeof value === 'string') return escapeHtml(value);
    if (Array.isArray(value)) return value.map(escapedView);
    if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, escapedView(item)]));
    return value;
}
function loadPreferences() {
    try { return JSON.parse(localStorage.getItem('dockpit.preferences') || '{}') || {}; }
    catch { return {}; }
}
function savePreferences() {
    try { localStorage.setItem('dockpit.preferences', JSON.stringify({
        expanded: [...expandedStacks], filter: activeQuickFilter,
        sort: document.getElementById('sortBy').value,
        search: document.getElementById('searchInput').value,
        stackAssignments: Object.fromEntries(stackAssignments),
    })); return true; } catch { return false; /* Storage can be disabled by the browser. */ }
}
const preferences = loadPreferences();
let latestData = null;
const stackAssignments = new Map(Object.entries(preferences.stackAssignments || {}).filter(([name, stack]) => typeof stack === 'string' && stack.trim() && stack.length <= 120));
let modalGeneration = 0;
let modalPreviousFocus = null;
let autoRefresh = true;
let refreshInterval = null;
let expandedStacks = new Set(Array.isArray(preferences.expanded) ? preferences.expanded : []);
let activeQuickFilter = preferences.filter || 'all';

function copyText(text, btn) {
    if (!text) return;
    navigator.clipboard.writeText(text).then(() => {
        const orig = btn.innerHTML;
        btn.innerHTML = '<i class="fa-solid fa-check text-emerald-400"></i> Copied';
        btn.classList.add('border-emerald-500/50');
        setTimeout(() => {
            btn.innerHTML = orig;
            btn.classList.remove('border-emerald-500/50');
        }, 1800);
    }).catch(err => {
        console.error('Copy failed:', err);
    });
}

function getHealthBadge(health) {
    const h = (health || '').toLowerCase();
    if (h === 'healthy') {
        return { icon: 'fa-circle-check', color: 'text-emerald-400', bg: 'bg-emerald-500/15', border: 'border-emerald-500/30', label: 'Healthy' };
    }
    if (h === 'running') {
        return { icon: 'fa-circle-play', color: 'text-blue-400', bg: 'bg-blue-500/15', border: 'border-blue-500/30', label: 'Running' };
    }
    if (h === 'unhealthy') {
        return { icon: 'fa-triangle-exclamation', color: 'text-rose-400', bg: 'bg-rose-500/15', border: 'border-rose-500/30', label: 'Unhealthy' };
    }
    if (h === 'starting') {
        return { icon: 'fa-circle-notch fa-spin', color: 'text-amber-400', bg: 'bg-amber-500/15', border: 'border-amber-500/30', label: 'Starting' };
    }
    return { icon: 'fa-circle-stop', color: 'text-slate-400', bg: 'bg-slate-500/15', border: 'border-slate-500/30', label: health || 'Exited' };
}

function getStackIcon(name) {
    const n = name.toLowerCase();
    if (n.includes('database') || n.includes('mariadb') || n.includes('postgres') || n.includes('mysql') || n.includes('redis') || n.includes('mongo')) {
        return { icon: 'fa-database', color: 'text-cyan-400', bg: 'bg-cyan-500/15', border: 'border-cyan-500/30' };
    }
    if (n.includes('vpn') || n.includes('proxy') || n.includes('gateway') || n.includes('tunnel') || n.includes('security')) {
        return { icon: 'fa-shield-halved', color: 'text-rose-400', bg: 'bg-rose-500/15', border: 'border-rose-500/30' };
    }
    if (n.includes('ai') || n.includes('rag') || n.includes('llm') || n.includes('vector')) {
        return { icon: 'fa-brain', color: 'text-purple-400', bg: 'bg-purple-500/15', border: 'border-purple-500/30' };
    }
    if (n.includes('mail') || n.includes('stalwart')) {
        return { icon: 'fa-envelope', color: 'text-amber-400', bg: 'bg-amber-500/15', border: 'border-amber-500/30' };
    }
    if (n.includes('game') || n.includes('amp')) {
        return { icon: 'fa-gamepad', color: 'text-emerald-400', bg: 'bg-emerald-500/15', border: 'border-emerald-500/30' };
    }
    if (n.includes('portainer') || n.includes('admin') || n.includes('dockpit')) {
        return { icon: 'fa-sliders', color: 'text-indigo-400', bg: 'bg-indigo-500/15', border: 'border-indigo-500/30' };
    }
    return { icon: 'fa-cubes', color: 'text-indigo-400', bg: 'bg-indigo-500/15', border: 'border-indigo-500/30' };
}

let statsPending = false;
let statsAbort = null;
async function fetchStats(force = false) {
    if (statsPending || document.hidden || (!autoRefresh && !force)) return;
    statsPending = true;
    statsAbort = new AbortController();
    try {
        const res = await fetch('/api/stats', {signal: statsAbort.signal});
        if (!res.ok) throw new Error('API returned status ' + res.status);
        const data = await res.json();
        latestData = data;

        updateTopKPIs(dashboardData());
        renderDashboard();

        updateSystemIndicator(data);
        renderIssues(data);

    } catch (err) {
        if (err.name === 'AbortError') return;
        console.error('Error fetching stats:', err);
        updateSystemIndicator(latestData, true);
    } finally { statsPending = false; }
}

function formatBytesNoDec(numBytes) {
    if (!numBytes || numBytes <= 0) return '0 B';
    const num = Number(numBytes);
    const tb = 1024 * 1024 * 1024 * 1024;
    const gb = 1024 * 1024 * 1024;
    const mb = 1024 * 1024;
    if (num >= tb) {
        return Math.round(num / tb) + ' TB';
    }
    if (num >= gb) {
        return Math.round(num / gb) + ' GB';
    }
    if (num >= mb) {
        return Math.round(num / mb) + ' MB';
    }
    return Math.round(num) + ' B';
}

function cleanGpuName(rawName) {
    if (!rawName) return '';
    return rawName.replace(/\s*\[.*?\]/g, '').trim() || rawName;
}

function formatImageName(img) {
    if (!img) return '—';
    if (img.startsWith('sha256:')) return img.slice(7, 19);
    if (/^[0-9a-f]{64}$/i.test(img)) return img.slice(0, 12);
    if (img.includes('@sha256:')) {
        const [repo, digest] = img.split('@sha256:');
        return `${repo}:${digest.slice(0, 12)}`;
    }
    return img;
}

function renderPortChips(portsStr) {
    if (!portsStr || !portsStr.trim()) {
        return '<span class="muted text-xs">No published ports</span>';
    }
    const parts = portsStr.split(',').map(s => s.trim()).filter(Boolean);
    const seen = new Set();
    const chips = [];
    for (const part of parts) {
        const match = part.match(/(?:(?:[0-9.]+|\[[0-9a-f:]+\]):)?(\d+)->(\d+(?:\/\w+)?)/i);
        if (match) {
            const hostPort = match[1];
            const containerPort = match[2];
            const key = `${hostPort}->${containerPort}`;
            if (seen.has(key)) continue;
            seen.add(key);
            const url = portUrl(hostPort);
            chips.push(`<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" class="port-chip" title="Open port ${hostPort}">${escapeHtml(hostPort)} &rarr; ${escapeHtml(containerPort)} <i class="fa-solid fa-arrow-up-right-from-square" style="font-size:9px"></i></a>`);
        } else {
            if (seen.has(part)) continue;
            seen.add(part);
            chips.push(`<span class="port-chip port-internal">${escapeHtml(part)}</span>`);
        }
    }
    return chips.length ? chips.join(' ') : '<span class="muted text-xs">No published ports</span>';
}

function findContainer(name) {
    const data = dashboardData();
    if (!data || !data.stacks) return null;
    for (const stack of Object.values(data.stacks)) {
        for (const c of stack.containers || []) {
            if (c.name === name || c.display_name === name) return c;
        }
    }
    return null;
}

function updateTopKPIs(data) {
    const sys = data.system || {}, gpu = sys.gpu || {}, devices = gpu.devices || [];
    const text = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    };
    const bar = (id, value) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.style.width = `${Math.min(100, Math.max(0, value || 0))}%`;
        el.className = value >= 90 ? 'critical' : value >= 75 ? 'warning' : '';
    };
    text('sysCpu', sys.host_cpu_pct == null ? 'Unavailable' : `${sys.host_cpu_pct.toFixed(1)}%`);
    text('sysCpuCores', `${sys.cpus || 1} cores · Docker ${(data.docker_host_cpu_perc || 0).toFixed(1)}%`);
    bar('sysCpuBar', sys.host_cpu_pct);
    text('sysRam', sys.mem_used_formatted || 'Unavailable');
    text('sysRamSub', `of ${sys.mem_total_formatted || '—'} · ${sys.mem_pct == null ? '—' : sys.mem_pct.toFixed(1)}%`);
    bar('sysRamBar', sys.mem_pct);

    const disk = sys.root_disk || {};
    const dataDisk = sys.data_disk;
    text('sysNvmeStats', disk.total_bytes == null ? 'Unavailable' : formatBytesNoDec(disk.used_bytes));
    bar('sysNvmeBar', disk.pct);
    const dataWrap = document.getElementById('sysDataWrap');
    if (dataWrap) {
        if (dataDisk && dataDisk.total_bytes) {
            dataWrap.hidden = false;
            bar('sysDataBar', dataDisk.pct);
            const nvmeWrap = document.getElementById('sysNvmeWrap');
            if (nvmeWrap) nvmeWrap.title = `Root: ${formatBytesNoDec(disk.used_bytes)} / ${formatBytesNoDec(disk.total_bytes)} (${disk.pct ?? 0}%)`;
            dataWrap.title = `Data: ${formatBytesNoDec(dataDisk.used_bytes)} / ${formatBytesNoDec(dataDisk.total_bytes)} (${dataDisk.pct ?? 0}%)`;
            text('storageSub', `Root ${disk.pct ?? '—'}% · Data ${dataDisk.pct ?? '—'}% of ${formatBytesNoDec(dataDisk.total_bytes)}`);
        } else {
            dataWrap.hidden = true;
            text('storageSub', `of ${formatBytesNoDec(disk.total_bytes)} · ${disk.pct ?? '—'}% used`);
        }
    }

    const total = data.total_containers || 0, running = data.running_containers || 0;
    text('sysHealth', `${running} / ${total}`);
    text('sysUptime', `${data.total_stacks || 0} stacks · Up ${sys.uptime_str || '—'}`);
    text('count-all', total);
    bar('sysHealthBar', total ? running / total * 100 : 0);
    const healthBar = document.getElementById('sysHealthBar');
    if (healthBar) healthBar.className = '';

    const gpuNames = devices.map(d => cleanGpuName(d.name)).filter(Boolean).join(', ');
    const gpuMain = gpu.utilization_pct != null ? `${gpu.utilization_pct.toFixed(1)}%` : gpuNames || 'No GPU';
    text('sysGpu', gpuMain);
    const sysGpuEl = document.getElementById('sysGpu');
    if (sysGpuEl) {
        sysGpuEl.title = gpuNames || (gpu.utilization_pct != null ? `${gpu.utilization_pct.toFixed(1)}%` : 'No GPU');
        sysGpuEl.classList.toggle('no-metrics', gpu.utilization_pct == null);
    }
    const subText = gpu.utilization_pct != null ? gpuNames : devices.length ? 'No usage data' : 'No GPU detected';
    text('sysGpuName', subText);
    const sysGpuNameEl = document.getElementById('sysGpuName');
    if (sysGpuNameEl) sysGpuNameEl.title = subText;
    bar('sysGpuBar', gpu.utilization_pct);
}

function toggleStack(stackName) {
    if (expandedStacks.has(stackName)) {
        expandedStacks.delete(stackName);
    } else {
        expandedStacks.add(stackName);
    }
    renderDashboard();
}

function toggleExpandAll(expand) {
    if (!latestData || !latestData.stacks) return;
    if (expand) {
        Object.keys(dashboardData().stacks).forEach(s => expandedStacks.add(s));
    } else {
        expandedStacks.clear();
    }
    renderDashboard();
}

function setQuickFilter(type) {
    activeQuickFilter = ['all', 'running', 'stopped', 'issues', 'paused', 'starting', 'ports'].includes(type) ? type : 'all';
    document.querySelectorAll('.qf-btn').forEach(btn => btn.setAttribute('aria-pressed', btn.dataset.value === activeQuickFilter));
    const extra = document.querySelector(`.filter-popover [data-value="${activeQuickFilter}"]`);
    document.querySelector('.filters summary').textContent = extra ? extra.textContent : 'More filters';
    renderDashboard();
}

function toggleAutoRefresh() {
    autoRefresh = !autoRefresh;
    const btnText = document.getElementById('toggleText');
    const btnIcon = document.getElementById('toggleIcon');

    if (autoRefresh) {
        btnText.textContent = 'Pause';
        btnIcon.className = 'fa-solid fa-pause';
        startInterval();
        historyChart.start();
        fetchStats();
    } else {
        btnText.textContent = 'Start';
        btnIcon.className = 'fa-solid fa-play';
        clearInterval(refreshInterval);
        historyChart.stop();
    }
}

function startInterval() {
    clearInterval(refreshInterval);
    refreshInterval = setInterval(() => {
        if (autoRefresh) fetchStats();
    }, (latestData?.poll_interval || 2) * 1000);
}

// ==========================================
// TOP 4 CARDS INTERACTIVE MODALS
// ==========================================
function openGpuStatus(gpu) {
    const devices = gpu.devices || [];
    const number = (value, suffix) => value == null ? 'Not measured' : `${value.toFixed(1)}${suffix}`;
    const bytes = value => value == null ? 'Unavailable' : formatBytesNoDec(value);
    const content = devices.map(device => `<section class="gpu-device">
        <h4>${device.name}</h4><p class="muted">${device.vendor || ''}</p>
        <div class="diagnostic-facts">
            <div>GPU load: <span>${number(device.utilization_pct, '%')}</span></div>
            <div>${device.memory_kind === 'shared' ? 'Memory: <span>Shared system RAM</span>' : `VRAM: <span>${bytes(device.memory_used_bytes)} / ${bytes(device.memory_total_bytes)}</span>`}</div>
            ${device.frequency_mhz == null ? '' : `<div>Current clock: <span>${device.frequency_mhz.toFixed(0)} MHz</span></div>`}
            ${device.max_frequency_mhz == null ? '' : `<div>Configured max clock: <span>${device.max_frequency_mhz.toFixed(0)} MHz</span></div>`}
            ${device.temperature_c == null ? '' : `<div>Temperature: <span>${number(device.temperature_c, ' °C')}</span></div>`}
            ${device.power_w == null ? '' : `<div>Power: <span>${number(device.power_w, ' W')}</span></div>`}
        </div>
        ${device.utilization_note ? `<p class="muted">${device.utilization_note}</p>` : ''}
        ${device.memory_note ? `<p class="muted">${device.memory_note}</p>` : ''}
    </section>`).join('') || '<p class="muted">No GPU detected.</p>';
    openModal('GPU', 'Device measurements', content, '', () => fetchStats(true).then(() => openTopCardModal('gpu')));
}

function openTopCardModal(type) {
    if (!latestData) return;
    const sys = escapedView(latestData.system || {});
    const details = sys.details || {};

    if (type === 'cpu') {
        renderCpuDetailsModal(sys, details.cpu || {});
    } else if (type === 'gpu') {
        openGpuStatus(sys.gpu || {});
    } else if (type === 'memory') {
        renderMemoryDetailsModal(sys, details.memory || {});
    } else if (type === 'storage') {
        renderStorageDetailsModal(sys, details.storage || {});
    } else if (type === 'system') {
        renderSystemDetailsModal(sys, details.docker || {});
    }
}

function gpuPercent(value) {
    return value == null ? 'Unavailable' : `${value.toFixed(1)}%`;
}
function gpuBytes(value) {
    return value == null ? 'Unavailable' : formatBytesNoDec(value);
}
function renderGpuDetailsModal(gpu) {
    const devices = gpu.devices || [];
    const content = devices.map(d => `
        <div class="bg-slate-950 p-3.5 rounded-xl border border-slate-800 space-y-2">
            <h4 class="text-white font-bold">${d.name}</h4>
            <div class="text-xs text-slate-400 font-mono">${d.vendor} · ${d.id}</div>
            <div class="grid grid-cols-2 gap-3 text-sm text-slate-300">
                <div>GPU load: <strong>${d.utilization_pct == null ? 'Not measured' : gpuPercent(d.utilization_pct)}</strong></div>
                <div>${d.memory_kind === 'shared' ? 'Memory: <strong>Shared system RAM</strong>' : `VRAM: <strong>${gpuBytes(d.memory_used_bytes)} / ${gpuBytes(d.memory_total_bytes)}</strong>`}</div>
                ${d.frequency_mhz == null ? '' : `<div>Current clock: <strong>${d.frequency_mhz.toFixed(0)} MHz</strong></div>`}
                ${d.max_frequency_mhz == null ? '' : `<div>Configured max clock: <strong>${d.max_frequency_mhz.toFixed(0)} MHz</strong></div>`}
                ${d.temperature_c == null ? '' : `<div>Temperature: <strong>${d.temperature_c.toFixed(1)} °C</strong></div>`}
                ${d.power_w == null ? '' : `<div>Power: <strong>${d.power_w.toFixed(1)} W</strong></div>`}
            </div>
            ${d.utilization_pct == null && d.utilization_note ? `<p class="text-xs text-amber-300">${d.utilization_note}</p>` : ''}
            ${d.memory_note ? `<p class="text-xs text-slate-400">${d.memory_note}</p>` : ''}
        </div>`).join('') || '<p class="text-slate-400">No GPU detected. GPU measurements are unavailable.</p>';
    const measured = devices.length && devices.every(d => d.utilization_pct != null);
    openModal('Host GPU & memory', `${devices.length} graphics device(s) · ${measured ? 'utilization averaged across GPUs' : 'load measurements incomplete'}`,
        content + '<p class="text-xs text-slate-400">Temperature and power appear when exposed by the device. Container GPU shows measured process activity and dedicated VRAM where supported.</p>',
        'fa-microchip text-emerald-400', () => fetchStats(true).then(() => openTopCardModal('gpu')));
}

function renderCpuDetailsModal(sys, cpu) {
    const view = escapedView(dashboardData());
    const hostCpu = (sys.host_cpu_pct == null ? 'Unavailable' : sys.host_cpu_pct.toFixed(1));
    const dockerTotal = (view.docker_total_cpu || 0).toFixed(1);
    const hostShare = ((dockerTotal / (sys.cpus || 1)) || 0).toFixed(1);

    // Collect and sort containers by CPU
    let allContainers = [];
    if (view.stacks) {
        Object.values(view.stacks).forEach(s => {
            (s.containers || []).forEach(c => allContainers.push(c));
        });
    }
    allContainers.sort((a, b) => b.cpu_perc - a.cpu_perc);
    const topContainers = allContainers.slice(0, 5);

    const flagsHtml = (cpu.flags || []).map(f => `
        <span class="text-[10px] font-mono bg-slate-950 px-2 py-0.5 rounded border border-slate-800 text-slate-300">${f}</span>
    `).join('') || '<span class="text-xs text-slate-500">No flags reported</span>';

    const topTableHtml = topContainers.map(c => `
        <tr class="border-b border-slate-800/60 hover:bg-slate-800/30 font-mono text-xs">
            <td class="py-2.5 px-3 font-medium text-white flex items-center gap-2">
                <span class="w-2 h-2 rounded-full ${c.health === 'unhealthy' ? 'bg-rose-400' : 'bg-emerald-400'}"></span>
                <span class="truncate max-w-[200px] cursor-pointer hover:text-indigo-400" data-action="inspect" data-value="${c.name}">${c.name}</span>
            </td>
            <td class="py-2.5 px-3 text-slate-400">${c.stack}</td>
            <td class="py-2.5 px-3 text-amber-300 font-bold">${c.cpu_perc.toFixed(1)}%</td>
            <td class="py-2.5 px-3 text-slate-400">${c.pids}</td>
            <td class="py-2.5 px-3 text-right">
                <button data-action="inspect" data-value="${c.name}" class="text-[11px] px-2 py-1 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded border border-slate-700">Details</button>
            </td>
        </tr>
    `).join('');

    const content = `
        <div class="space-y-4">
            <!-- Top Summary Grid -->
            <div class="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
                <div class="bg-slate-950 p-3 rounded-xl border border-slate-800">
                    <div class="text-slate-400 mb-1 font-semibold flex items-center gap-1.5"><i class="fa-solid fa-microchip text-amber-400"></i> Model</div>
                    <div class="font-bold text-white text-sm truncate" title="${cpu.model_name || 'Generic CPU'}">${cpu.model_name || 'Generic CPU'}</div>
                    <div class="text-[11px] text-slate-500 font-mono mt-0.5">${cpu.vendor_id || 'GenuineIntel'}</div>
                </div>
                <div class="bg-slate-950 p-3 rounded-xl border border-slate-800">
                    <div class="text-slate-400 mb-1 font-semibold flex items-center gap-1.5"><i class="fa-solid fa-layer-group text-cyan-400"></i> Topology</div>
                    <div class="font-bold text-white text-sm">${sys.cpus || 1} Threads</div>
                    <div class="text-[11px] text-slate-500 font-mono mt-0.5">${cpu.cores_per_socket || Math.round((sys.cpus || 1) / 2)} Physical Cores</div>
                </div>
                <div class="bg-slate-950 p-3 rounded-xl border border-slate-800">
                    <div class="text-slate-400 mb-1 font-semibold flex items-center gap-1.5"><i class="fa-solid fa-gauge-high text-emerald-400"></i> Clock &amp; Cache</div>
                    <div class="font-bold text-white text-sm font-mono">${cpu.cpu_mhz ? Math.round(cpu.cpu_mhz) + ' MHz' : 'Variable'}</div>
                    <div class="text-[11px] text-slate-500 font-mono mt-0.5">${cpu.cache_size || 'L3 Cache'}</div>
                </div>
                <div class="bg-slate-950 p-3 rounded-xl border border-slate-800">
                    <div class="text-slate-400 mb-1 font-semibold flex items-center gap-1.5"><i class="fa-solid fa-chart-line text-purple-400"></i> Load Averages</div>
                    <div class="font-bold text-white text-sm font-mono">${sys.load_str || '-'}</div>
                    <div class="text-[11px] text-slate-500 font-mono mt-0.5">1m, 5m, 15m</div>
                </div>
            </div>

            <!-- Utilization Meters -->
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
                <h4 class="text-xs font-bold uppercase tracking-wider text-slate-400 flex items-center gap-1.5"><i class="fa-solid fa-bolt text-amber-400"></i> Real-Time Utilization</h4>
                <div>
                    <div class="flex justify-between text-xs font-mono mb-1">
                        <span class="text-slate-300">Host Total CPU Load</span>
                        <span class="text-amber-400 font-bold">${hostCpu}%</span>
                    </div>
                    <div class="w-full bg-slate-800 rounded-full h-2 overflow-hidden">
                        <div class="bg-amber-400 h-2 rounded-full progress-bar-smooth" style="width: ${Math.min(100, Math.max(2, parseFloat(hostCpu)))}%"></div>
                    </div>
                </div>
                <div>
                    <div class="flex justify-between text-xs font-mono mb-1">
                        <span class="text-slate-300">Docker Aggregate Consumption (${dockerTotal}% cumulative)</span>
                        <span class="text-cyan-400 font-bold">${hostShare}% of entire machine</span>
                    </div>
                    <div class="w-full bg-slate-800 rounded-full h-2 overflow-hidden">
                        <div class="bg-cyan-400 h-2 rounded-full progress-bar-smooth" style="width: ${Math.min(100, Math.max(2, parseFloat(hostShare)))}%"></div>
                    </div>
                </div>
            </div>

            <!-- Top Consuming Containers -->
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                <h4 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-2 flex items-center gap-1.5"><i class="fa-solid fa-fire text-rose-400"></i> Top CPU Consuming Containers</h4>
                <div class="overflow-x-auto">
                    <table class="w-full text-left">
                        <thead>
                            <tr class="text-[11px] text-slate-500 uppercase tracking-wider border-b border-slate-800">
                                <th class="pb-2 px-3">Container</th>
                                <th class="pb-2 px-3">Stack</th>
                                <th class="pb-2 px-3">CPU %</th>
                                <th class="pb-2 px-3">PIDs</th>
                                <th class="pb-2 px-3 text-right">Action</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${topTableHtml || '<tr><td colspan="5" class="py-4 text-center text-slate-500">No active containers found</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- Instruction Flags -->
            <div class="bg-slate-950 p-3.5 rounded-xl border border-slate-800">
                <h4 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-2 flex items-center gap-1.5"><i class="fa-solid fa-microchip text-slate-500"></i> Hardware Flags</h4>
                <div class="flex flex-wrap gap-1.5">${flagsHtml}</div>
            </div>
        </div>
    `;

    openModal(
        'Host CPU & Processor Architecture',
        `${cpu.model_name || 'Central Processing Unit'} • ${sys.cpus || 1} Threads`,
        content,
        'fa-microchip text-amber-400',
        () => { fetchStats(true).then(() => openTopCardModal('cpu')); }
    );
}

function renderMemoryDetailsModal(sys, mem) {
    const view = escapedView(dashboardData());
    let allContainers = [];
    if (view.stacks) {
        Object.values(view.stacks).forEach(s => {
            (s.containers || []).forEach(c => allContainers.push(c));
        });
    }
    allContainers.sort((a, b) => b.mem_used_bytes - a.mem_used_bytes);
    const topContainers = allContainers.slice(0, 5);

    const topTableHtml = topContainers.map(c => `
        <tr class="border-b border-slate-800/60 hover:bg-slate-800/30 font-mono text-xs">
            <td class="py-2.5 px-3 font-medium text-white flex items-center gap-2">
                <span class="w-2 h-2 rounded-full ${c.health === 'unhealthy' ? 'bg-rose-400' : 'bg-purple-400'}"></span>
                <span class="truncate max-w-[200px] cursor-pointer hover:text-indigo-400" data-action="inspect" data-value="${c.name}">${c.name}</span>
            </td>
            <td class="py-2.5 px-3 text-slate-400">${c.stack}</td>
            <td class="py-2.5 px-3 text-purple-300 font-bold">${c.mem_used_formatted}</td>
            <td class="py-2.5 px-3 text-slate-400">${c.has_custom_limit ? c.mem_limit_formatted : 'Host RAM'}</td>
            <td class="py-2.5 px-3 text-right">
                <button data-action="inspect" data-value="${c.name}" class="text-[11px] px-2 py-1 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded border border-slate-700">Details</button>
            </td>
        </tr>
    `).join('');

    const content = `
        <div class="space-y-4">
            <!-- Top Summary Grid -->
            <div class="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
                <div class="bg-slate-950 p-3 rounded-xl border border-slate-800">
                    <div class="text-slate-400 mb-1 font-semibold flex items-center gap-1.5"><i class="fa-solid fa-memory text-purple-400"></i> Total Physical RAM</div>
                    <div class="font-bold text-white text-base font-mono">${sys.mem_total_formatted || mem.total_formatted || 'Unavailable'}</div>
                    <div class="text-[11px] text-slate-500 font-mono mt-0.5">Physical Hardware</div>
                </div>
                <div class="bg-slate-950 p-3 rounded-xl border border-slate-800">
                    <div class="text-slate-400 mb-1 font-semibold flex items-center gap-1.5"><i class="fa-solid fa-chart-pie text-cyan-400"></i> Real Used (RSS)</div>
                    <div class="font-bold text-purple-300 text-base font-mono">${sys.mem_used_formatted || mem.used_formatted}</div>
                    <div class="text-[11px] text-slate-500 font-mono mt-0.5">${sys.mem_pct || 0}% of Host RAM</div>
                </div>
                <div class="bg-slate-950 p-3 rounded-xl border border-slate-800">
                    <div class="text-slate-400 mb-1 font-semibold flex items-center gap-1.5"><i class="fa-solid fa-leaf text-emerald-400"></i> Available RAM</div>
                    <div class="font-bold text-emerald-300 text-base font-mono">${mem.available_formatted || 'N/A'}</div>
                    <div class="text-[11px] text-slate-500 font-mono mt-0.5">Free + Purgeable Cache</div>
                </div>
                <div class="bg-slate-950 p-3 rounded-xl border border-slate-800">
                    <div class="text-slate-400 mb-1 font-semibold flex items-center gap-1.5"><i class="fa-solid fa-arrow-right-arrow-left text-amber-400"></i> Swap Space</div>
                    <div class="font-bold text-white text-base font-mono">${mem.swap_used_formatted || '0 B'}</div>
                    <div class="text-[11px] text-slate-500 font-mono mt-0.5">of ${mem.swap_total_formatted || '0 B'} (${mem.swap_pct || 0}%)</div>
                </div>
            </div>

            <!-- Memory Tiers Breakdown -->
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
                <h4 class="text-xs font-bold uppercase tracking-wider text-slate-400 flex items-center gap-1.5"><i class="fa-solid fa-sliders text-purple-400"></i> Kernel Memory Distribution</h4>
                <div class="grid grid-cols-2 sm:grid-cols-3 gap-2.5 text-xs font-mono">
                    <div class="p-2.5 bg-slate-900 rounded-lg border border-slate-800">
                        <div class="text-slate-400 text-[11px]">Active Memory</div>
                        <div class="text-slate-200 font-bold mt-0.5">${mem.active_formatted || '0 B'}</div>
                        <div class="text-[10px] text-slate-500">In-use by processes</div>
                    </div>
                    <div class="p-2.5 bg-slate-900 rounded-lg border border-slate-800">
                        <div class="text-slate-400 text-[11px]">Inactive Memory</div>
                        <div class="text-slate-200 font-bold mt-0.5">${mem.inactive_formatted || '0 B'}</div>
                        <div class="text-[10px] text-slate-500">Purgeable by kernel</div>
                    </div>
                    <div class="p-2.5 bg-slate-900 rounded-lg border border-slate-800">
                        <div class="text-slate-400 text-[11px]">Page Cache</div>
                        <div class="text-slate-200 font-bold mt-0.5">${mem.cached_formatted || '0 B'}</div>
                        <div class="text-[10px] text-slate-500">Filesystem buffers in RAM</div>
                    </div>
                    <div class="p-2.5 bg-slate-900 rounded-lg border border-slate-800">
                        <div class="text-slate-400 text-[11px]">Buffers (Raw IO)</div>
                        <div class="text-slate-200 font-bold mt-0.5">${mem.buffers_formatted || '0 B'}</div>
                        <div class="text-[10px] text-slate-500">Block cache</div>
                    </div>
                    <div class="p-2.5 bg-slate-900 rounded-lg border border-slate-800">
                        <div class="text-slate-400 text-[11px]">Shared Memory (Shmem)</div>
                        <div class="text-slate-200 font-bold mt-0.5">${mem.shmem_formatted || '0 B'}</div>
                        <div class="text-[10px] text-slate-500">tmpfs and IPC</div>
                    </div>
                    <div class="p-2.5 bg-slate-900 rounded-lg border border-slate-800">
                        <div class="text-slate-400 text-[11px]">SReclaimable (Slab)</div>
                        <div class="text-slate-200 font-bold mt-0.5">${mem.sreclaimable_formatted || '0 B'}</div>
                        <div class="text-[10px] text-slate-500">Kernel directory cache</div>
                    </div>
                </div>

                <!-- Progress Bar -->
                <div class="pt-2">
                    <div class="flex justify-between text-xs font-mono mb-1">
                        <span class="text-slate-300">Docker Resident Footprint</span>
                        <span class="text-purple-300 font-bold">${view.docker_total_mem_str || '0 B'} (${view.docker_total_mem_perc || 0}% host)</span>
                    </div>
                    <div class="w-full bg-slate-800 rounded-full h-2 overflow-hidden">
                        <div class="bg-purple-500 h-2 rounded-full progress-bar-smooth" style="width: ${Math.min(100, Math.max(2, view.docker_total_mem_perc || 0))}%"></div>
                    </div>
                </div>
            </div>

            <!-- Top Memory Containers -->
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                <h4 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-2 flex items-center gap-1.5"><i class="fa-solid fa-memory text-purple-400"></i> Top Memory Consuming Containers</h4>
                <div class="overflow-x-auto">
                    <table class="w-full text-left">
                        <thead>
                            <tr class="text-[11px] text-slate-500 uppercase tracking-wider border-b border-slate-800">
                                <th class="pb-2 px-3">Container</th>
                                <th class="pb-2 px-3">Stack</th>
                                <th class="pb-2 px-3">Memory (RSS)</th>
                                <th class="pb-2 px-3">Limit</th>
                                <th class="pb-2 px-3 text-right">Action</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${topTableHtml || '<tr><td colspan="5" class="py-4 text-center text-slate-500">No active containers found</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    `;

    openModal(
        'Host Memory & Swap Breakdown',
        `${sys.mem_total_formatted || 'Unavailable'} Physical RAM • ${sys.mem_pct || 0}% Allocated`,
        content,
        'fa-memory text-purple-400',
        () => { fetchStats(true).then(() => openTopCardModal('memory')); }
    );
}

function renderStorageDetailsModal(sys, storage) {
    const view = escapedView(dashboardData());
    const devices = storage.devices || [];

    let devicesHtml = '';
    if (devices.length > 0) {
        devicesHtml = devices.map(d => {
            const isSsd = d.type.includes('SSD') || d.type.includes('NVMe');
            const badgeClass = isSsd
                ? 'bg-cyan-950 text-cyan-300 border-cyan-800/50'
                : 'bg-emerald-950 text-emerald-300 border-emerald-800/50';
            const icon = isSsd ? 'fa-bolt text-cyan-400' : 'fa-database text-emerald-400';

            // Partitions rows
            const partsHtml = (d.partitions || []).map(p => {
                const usage = p.usage || {};
                const pct = usage.pct !== undefined ? usage.pct : 0;
                const barColor = pct > 85 ? 'bg-rose-500' : (pct > 70 ? 'bg-amber-400' : 'bg-cyan-400');
                return `
                <div class="p-3 bg-slate-900 rounded-xl border border-slate-800/80 space-y-2">
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-1 text-xs">
                        <div class="flex items-center gap-2">
                            <span class="font-bold text-white font-mono">${p.device}</span>
                            <span class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-slate-800 text-slate-300 uppercase">${p.fstype || 'raw'}</span>
                            ${p.mount ? `<span class="text-xs font-semibold text-cyan-300 bg-cyan-950/70 border border-cyan-800/40 px-2 py-0.5 rounded font-mono">&rarr; ${p.mount}</span>` : '<span class="text-slate-500 text-[11px]">(Unmounted)</span>'}
                        </div>
                        <div class="font-mono text-slate-300 text-xs">
                            ${usage.used_formatted ? `${usage.used_formatted} / ${usage.total_formatted} (${pct}%)` : (p.size_formatted || 'N/A')}
                        </div>
                    </div>

                    ${usage.total_bytes ? `
                    <div class="w-full bg-slate-800 rounded-full h-1.5 overflow-hidden">
                        <div class="${barColor} h-1.5 rounded-full progress-bar-smooth" style="width: ${Math.min(100, Math.max(3, pct))}%"></div>
                    </div>
                    <div class="flex justify-between text-[11px] text-slate-400 font-mono">
                        <span>Free: <strong class="text-emerald-400">${usage.free_formatted || '-'}</strong></span>
                        <span class="truncate max-w-sm text-slate-500" title="${p.id || ''}">ID: ${p.id || p.name}</span>
                    </div>
                    ` : `
                    <div class="text-[11px] text-slate-500 font-mono">Size: ${p.size_formatted}</div>
                    `}
                </div>
                `;
            }).join('') || '<div class="text-xs text-slate-500 p-2">No partitions detected on this device</div>';

            return `
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
                <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-slate-800 pb-3">
                    <div class="flex items-center gap-2.5">
                        <div class="w-8 h-8 rounded-lg bg-slate-800 border border-slate-700 flex items-center justify-center">
                            <i class="fa-solid ${icon} text-sm"></i>
                        </div>
                        <div>
                            <div class="flex items-center gap-2">
                                <h4 class="font-bold text-white text-sm font-mono">${d.device}</h4>
                                <span class="text-[10px] font-semibold px-2 py-0.5 rounded-full border ${badgeClass}">${d.type}</span>
                            </div>
                            <div class="text-xs text-slate-400">${d.model}</div>
                        </div>
                    </div>
                    <div class="text-right">
                        <div class="text-base font-bold text-white font-mono">${d.size_formatted}</div>
                        <div class="text-[11px] text-slate-500 font-mono">Total Capacity</div>
                    </div>
                </div>

                <!-- Hardware Identifier Box -->
                <div class="grid grid-cols-1 md:grid-cols-2 gap-2 text-xs font-mono bg-slate-900/80 p-2.5 rounded-lg border border-slate-800">
                    <div class="flex items-center justify-between">
                        <span class="text-slate-400">Serial:</span>
                        <span class="text-slate-200 select-all font-semibold">${d.serial || 'N/A'}</span>
                    </div>
                    <div class="flex items-center justify-between gap-2 min-w-0">
                        <span class="text-slate-400 shrink-0">Disk ID:</span>
                        <span class="text-indigo-300 truncate select-all" title="${d.id}">${d.id}</span>
                        <button data-action="copy" data-value="${d.id}" class="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 transition">
                            <i class="fa-regular fa-copy"></i>
                        </button>
                    </div>
                </div>

                <!-- Partitions List -->
                <div class="space-y-2 pt-1">
                    <div class="text-xs font-bold uppercase tracking-wider text-slate-400">Partitions &amp; Mounts</div>
                    ${partsHtml}
                </div>
            </div>
            `;
        }).join('');
    } else {
        devicesHtml = `
            <div class="p-6 text-center text-slate-400 bg-slate-950 rounded-xl border border-slate-800">
                <i class="fa-solid fa-hard-drive text-3xl mb-2 text-slate-500"></i>
                <p class="font-semibold text-slate-300">Direct sysfs block inspection unavailable</p>
                <p class="text-xs text-slate-500 mt-1">Mounted volumes are reported via standard filesystem queries.</p>
            </div>
        `;
    }

    const content = `
        <div class="space-y-4">
            <!-- Top Summary -->
            <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs">
                <div class="bg-slate-950 p-3 rounded-xl border border-slate-800">
                    <div class="text-slate-400 mb-1 font-semibold flex items-center gap-1.5"><i class="fa-solid fa-bolt text-cyan-400"></i> Root System Drive</div>
                    <div class="font-bold text-white text-base font-mono">${sys.root_disk ? sys.root_disk.used_formatted : '-'} / ${sys.root_disk ? sys.root_disk.total_formatted : '-'}</div>
                    <div class="text-[11px] text-cyan-300 font-mono mt-0.5">${sys.root_disk ? sys.root_disk.pct : 0}% used (${sys.root_disk ? sys.root_disk.free_formatted : '-'} free)</div>
                </div>
                <div class="bg-slate-950 p-3 rounded-xl border border-slate-800">
                    <div class="text-slate-400 mb-1 font-semibold flex items-center gap-1.5"><i class="fa-solid fa-database text-emerald-400"></i> Secondary Data Drive</div>
                    <div class="font-bold text-white text-base font-mono">${sys.data_disk ? sys.data_disk.used_formatted + ' / ' + sys.data_disk.total_formatted : 'None detected'}</div>
                    <div class="text-[11px] text-emerald-300 font-mono mt-0.5">${sys.data_disk ? sys.data_disk.pct + '% used (' + sys.data_disk.free_formatted + ' free)' : 'Single tier configuration'}</div>
                </div>
                <div class="bg-slate-950 p-3 rounded-xl border border-slate-800">
                    <div class="text-slate-400 mb-1 font-semibold flex items-center gap-1.5"><i class="fa-brands fa-docker text-indigo-400"></i> Docker Root Directory</div>
                    <div class="font-bold text-white text-sm font-mono truncate" title="${storage.data_root || '/var/lib/docker'}">${storage.data_root || '/var/lib/docker'}</div>
                    <div class="text-[11px] text-slate-500 font-mono mt-0.5">Containers, images &amp; volumes</div>
                </div>
            </div>

            <!-- Physical Drives Inventory -->
            <div class="space-y-3">
                <h4 class="text-xs font-bold uppercase tracking-wider text-slate-400 flex items-center gap-1.5"><i class="fa-solid fa-server text-indigo-400"></i> Physical Block Devices &amp; Disk IDs</h4>
                ${devicesHtml}
            </div>
        </div>
    `;

    openModal(
        'Storage Drives & Filesystem Inventory',
        `Physical Disks, Drive Numbers, Hardware Serials, IDs & Mounts`,
        content,
        'fa-hard-drive text-cyan-400',
        () => { fetchStats(true).then(() => openTopCardModal('storage')); }
    );
}

function renderSystemDetailsModal(sys, docker) {
    const view = escapedView(dashboardData());
    const runningCount = view.running_containers !== undefined ? view.running_containers : (view.total_containers || 0);
    const totalCount = view.total_containers || runningCount;
    const unhealthyCount = view.unhealthy_containers || 0;

    const content = `
        <div class="space-y-4">
            <!-- Top Host Info Grid -->
            <div class="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
                <div class="bg-slate-950 p-3 rounded-xl border border-slate-800">
                    <div class="text-slate-400 mb-1 font-semibold flex items-center gap-1.5"><i class="fa-brands fa-linux text-amber-400"></i> Operating System</div>
                    <div class="font-bold text-white text-sm truncate" title="${docker.os || 'Linux'}">${docker.os || 'Linux'}</div>
                    <div class="text-[11px] text-slate-500 font-mono mt-0.5">${docker.arch || 'Unknown'}</div>
                </div>
                <div class="bg-slate-950 p-3 rounded-xl border border-slate-800">
                    <div class="text-slate-400 mb-1 font-semibold flex items-center gap-1.5"><i class="fa-solid fa-terminal text-cyan-400"></i> Linux Kernel</div>
                    <div class="font-bold text-white text-sm font-mono truncate" title="${docker.kernel_version || 'Linux'}">${docker.kernel_version || 'Linux'}</div>
                    <div class="text-[11px] text-slate-500 font-mono mt-0.5">Cgroups v${docker.cgroup_version || '2'}</div>
                </div>
                <div class="bg-slate-950 p-3 rounded-xl border border-slate-800">
                    <div class="text-slate-400 mb-1 font-semibold flex items-center gap-1.5"><i class="fa-solid fa-clock text-emerald-400"></i> System Uptime</div>
                    <div class="font-bold text-emerald-300 text-sm font-mono">${sys.uptime_str || 'N/A'}</div>
                    <div class="text-[11px] text-slate-500 font-mono mt-0.5">Continuous Operation</div>
                </div>
                <div class="bg-slate-950 p-3 rounded-xl border border-slate-800">
                    <div class="text-slate-400 mb-1 font-semibold flex items-center gap-1.5"><i class="fa-solid fa-microchip text-purple-400"></i> Active Processes</div>
                    <div class="font-bold text-white text-sm font-mono">${view.total_pids || 0} PIDs</div>
                    <div class="text-[11px] text-slate-500 font-mono mt-0.5">Across all containers</div>
                </div>
            </div>

            <!-- Docker Engine & Runtime -->
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
                <h4 class="text-xs font-bold uppercase tracking-wider text-slate-400 flex items-center gap-1.5"><i class="fa-brands fa-docker text-indigo-400"></i> Docker Daemon &amp; Containerd Runtime</h4>
                <div class="grid grid-cols-2 sm:grid-cols-3 gap-2.5 text-xs font-mono">
                    <div class="p-2.5 bg-slate-900 rounded-lg border border-slate-800">
                        <div class="text-slate-400 text-[11px]">Docker Engine</div>
                        <div class="text-white font-bold mt-0.5">${docker.docker_version || 'Unknown'}</div>
                        <div class="text-[10px] text-slate-500">API: ${docker.api_version || 'Unknown'}</div>
                    </div>
                    <div class="p-2.5 bg-slate-900 rounded-lg border border-slate-800">
                        <div class="text-slate-400 text-[11px]">Storage Driver</div>
                        <div class="text-cyan-300 font-bold mt-0.5">${docker.driver || 'overlay2'}</div>
                        <div class="text-[10px] text-slate-500">Backing root storage</div>
                    </div>
                    <div class="p-2.5 bg-slate-900 rounded-lg border border-slate-800">
                        <div class="text-slate-400 text-[11px]">Cgroup Driver</div>
                        <div class="text-emerald-300 font-bold mt-0.5">v${docker.cgroup_version || '2'}</div>
                        <div class="text-[10px] text-slate-500">systemd unified</div>
                    </div>
                    <div class="p-2.5 bg-slate-900 rounded-lg border border-slate-800">
                        <div class="text-slate-400 text-[11px]">containerd</div>
                        <div class="text-white font-bold mt-0.5">${docker.containerd_version || 'Unknown'}</div>
                        <div class="text-[10px] text-slate-500">OCI executor</div>
                    </div>
                    <div class="p-2.5 bg-slate-900 rounded-lg border border-slate-800">
                        <div class="text-slate-400 text-[11px]">runc</div>
                        <div class="text-white font-bold mt-0.5">${docker.runc_version || 'Unknown'}</div>
                        <div class="text-[10px] text-slate-500">Low-level container engine</div>
                    </div>
                    <div class="p-2.5 bg-slate-900 rounded-lg border border-slate-800">
                        <div class="text-slate-400 text-[11px]">Compose Stacks</div>
                        <div class="text-indigo-300 font-bold mt-0.5">${view.total_stacks || 0} Projects</div>
                        <div class="text-[10px] text-slate-500">Active project groups</div>
                    </div>
                </div>
            </div>

            <!-- Fleet Health Status -->
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
                <h4 class="text-xs font-bold uppercase tracking-wider text-slate-400 flex items-center gap-1.5"><i class="fa-solid fa-heart-pulse text-emerald-400"></i> Container Fleet Health</h4>
                <div class="grid grid-cols-2 sm:grid-cols-4 gap-2.5 text-xs">
                    <div class="p-2.5 bg-slate-900 rounded-lg border border-slate-800">
                        <div class="text-slate-400 text-[11px]">Running</div>
                        <div class="text-emerald-400 font-bold text-base font-mono">${runningCount}</div>
                    </div>
                    <div class="p-2.5 bg-slate-900 rounded-lg border border-slate-800">
                        <div class="text-slate-400 text-[11px]">Unhealthy</div>
                        <div class="${unhealthyCount > 0 ? 'text-rose-400' : 'text-slate-400'} font-bold text-base font-mono">${unhealthyCount}</div>
                    </div>
                    <div class="p-2.5 bg-slate-900 rounded-lg border border-slate-800">
                        <div class="text-slate-400 text-[11px]">Stopped / Exited</div>
                        <div class="text-slate-300 font-bold text-base font-mono">${Math.max(0, totalCount - runningCount)}</div>
                    </div>
                    <div class="p-2.5 bg-slate-900 rounded-lg border border-slate-800">
                        <div class="text-slate-400 text-[11px]">Total Containers</div>
                        <div class="text-indigo-300 font-bold text-base font-mono">${totalCount}</div>
                    </div>
                </div>
            </div>
        </div>
    `;

    openModal(
        'System Overview & Docker Daemon Runtime',
        `${docker.os || 'Linux'} • Kernel ${docker.kernel_version || 'Linux'} • Docker v${docker.docker_version || 'Unknown'}`,
        content,
        'fa-sliders text-emerald-400',
        () => { fetchStats(true).then(() => openTopCardModal('system')); }
    );
}

function dashboardData() {
    if (!latestData) return null;
    const stacks = new Map();
    for (const source of Object.values(latestData.stacks || {})) {
        for (const original of source.containers || []) {
            const name = stackAssignments.get(original.name) || original.stack || source.name;
            const c = {...original, stack: name, stack_source: stackAssignments.has(original.name) ? 'manual' : original.stack_source};
            if (!stacks.has(name)) stacks.set(name, {name, containers: [], total_cpu: 0, total_mem_bytes: 0, total_pids: 0, healthy_count: 0, total_containers: 0});
            const stack = stacks.get(name);
            stack.containers.push(c);
            stack.total_cpu += c.cpu_perc || 0;
            stack.total_mem_bytes += c.mem_used_bytes || 0;
            stack.total_pids += c.pids || 0;
            stack.healthy_count += ['healthy', 'running'].includes(c.health) ? 1 : 0;
            stack.total_containers++;
        }
    }
    for (const stack of stacks.values()) {
        stack.total_mem_formatted = formatBytesNoDec(stack.total_mem_bytes);
        stack.host_mem_perc = latestData.host_total_mem_bytes ? stack.total_mem_bytes / latestData.host_total_mem_bytes * 100 : 0;
        stack.docker_mem_share = latestData.docker_total_mem_bytes ? Math.round(stack.total_mem_bytes / latestData.docker_total_mem_bytes * 100) : 0;
    }
    return {...latestData, stacks: Object.fromEntries(stacks), total_stacks: stacks.size};
}

function organizeStacks(preselect = '') {
    if (!latestData) return;
    const data = dashboardData();
    const containers = Object.values(data.stacks).flatMap(s => s.containers).sort((a, b) => a.name.localeCompare(b.name));
    const reasons = {label: 'Stack label', compose: 'Compose project', swarm: 'Swarm stack', image: 'Same image', standalone: 'No stack metadata', manual: 'Your choice'};
    openModal('Organize stacks', 'Select containers, then choose an existing stack or create one.', `
        <div class="space-y-3">
            <p class="text-xs text-slate-400">Your choices are saved in this browser and survive container recreation with the same name. Use a dockpit.stack label to share a stack with everyone.</p>
            <label class="block text-sm text-slate-300">Find containers
                <input id="stackSearch" type="search" class="w-full mt-1 p-2 rounded-lg bg-slate-950 border border-slate-700" placeholder="Name, stack or image">
            </label>
            <div class="flex items-center gap-3 text-xs text-slate-300">
                <button data-action="stack-select" class="px-3 py-2 rounded-lg bg-slate-800">Select visible</button>
                <button data-action="stack-clear" class="px-3 py-2 rounded-lg bg-slate-800">Clear selection</button>
                <span id="stackSelection" role="status"></span>
            </div>
            <div id="stackChoices" class="stack-choices border border-slate-800 rounded-xl divide-y divide-slate-800">
                ${containers.map(c => `<label class="stack-choice flex items-center gap-3 p-3 cursor-pointer hover:bg-slate-800" data-search="${escapeHtml([c.name, c.stack, c.image].join(' ').toLowerCase())}">
                    <input type="checkbox" value="${escapeHtml(c.name)}" ${c.name === preselect ? 'checked' : ''}>
                    <span class="min-w-0"><strong class="block text-sm text-white break-all">${escapeHtml(c.name)}</strong>
                    <span class="block text-xs text-slate-400 break-all">${escapeHtml(c.stack)} · ${reasons[c.stack_source] || 'Automatic'} · ${escapeHtml(c.image)}</span></span>
                </label>`).join('')}
            </div>
            <label class="block text-sm text-slate-300">Destination stack
                <input id="stackDestination" maxlength="120" list="stackNames" class="w-full mt-1 p-2 rounded-lg bg-slate-950 border border-slate-700" placeholder="Choose a stack or type a new name">
                <datalist id="stackNames">${Object.keys(data.stacks).sort().map(name => `<option value="${escapeHtml(name)}"></option>`).join('')}</datalist>
            </label>
            <div class="flex flex-wrap gap-2">
                <button data-action="stack-assign" class="px-3 py-2 text-sm rounded-lg bg-indigo-600 text-white">Move selected</button>
                <button data-action="stack-reset" class="px-3 py-2 text-sm rounded-lg bg-slate-800 text-slate-300">Use automatic grouping</button>
            </div>
            <p id="stackFeedback" role="status" class="text-xs text-amber-300"></p>
        </div>`, 'fa-layer-group text-indigo-400', () => organizeStacks());
    const search = document.getElementById('stackSearch');
    search.addEventListener('input', () => {
        const q = search.value.trim().toLowerCase();
        document.querySelectorAll('.stack-choice').forEach(row => { row.hidden = !row.dataset.search.includes(q); });
    });
    document.getElementById('stackChoices').addEventListener('change', updateStackSelection);
    updateStackSelection();
}

function updateStackSelection() {
    const count = document.querySelectorAll('#stackChoices input:checked').length;
    document.getElementById('stackSelection').textContent = `${count} selected`;
}

function assignStack(reset = false) {
    const selected = [...document.querySelectorAll('#stackChoices input:checked')];
    const destination = document.getElementById('stackDestination').value.trim();
    if (!selected.length || (!reset && !destination)) {
        document.getElementById('stackFeedback').textContent = !selected.length ? 'Select at least one container.' : 'Enter a stack name.';
        return;
    }
    const previous = new Map(stackAssignments);
    selected.forEach(input => { if (reset) stackAssignments.delete(input.value); else stackAssignments.set(input.value, destination); });
    // A saved organization should never silently disappear after a reload.
    if (!savePreferences()) {
        stackAssignments.clear();
        previous.forEach((value, key) => stackAssignments.set(key, value));
        document.getElementById('stackFeedback').textContent = 'Could not save. Allow browser storage and try again.';
        return;
    }
    if (!reset) expandedStacks.add(destination);
    renderDashboard();
    updateTopKPIs(dashboardData());
    closeModal();
}

function renderDashboard() {
    const view = dashboardData();
    if (!latestData || !view.stacks) return;

    savePreferences();
    const q = (document.getElementById('searchInput').value || '').toLowerCase().trim();
    const sortBy = document.getElementById('sortBy').value;
    const containerEl = document.getElementById('stacksContainer');

    let stackKeys = Object.keys(view.stacks);
    let filtered = [];

    for (const key of stackKeys) {
        const s = view.stacks[key];
        const matchesStack = key.toLowerCase().includes(q);

        const matchingContainers = s.containers.filter(c => {
            const matchesSearch = matchesStack ||
                   c.name.toLowerCase().includes(q) ||
                   (c.display_name || '').toLowerCase().includes(q) ||
                   c.image.toLowerCase().includes(q) ||
                   c.service.toLowerCase().includes(q) ||
                   (c.storage_tier && c.storage_tier.toLowerCase().includes(q)) ||
                   (c.ports && c.ports.toLowerCase().includes(q));

            if (!matchesSearch) return false;

            return matchesQuickFilter(c, activeQuickFilter);
        });

        if (matchingContainers.length > 0) {
            filtered.push({
                ...s,
                key,
                displayContainers: matchingContainers
            });
        }
    }

    if (sortBy === 'cpu') {
        filtered.sort((a, b) => b.total_cpu - a.total_cpu);
    } else if (sortBy === 'mem') {
        filtered.sort((a, b) => b.total_mem_bytes - a.total_mem_bytes);
    } else if (sortBy === 'containers') {
        filtered.sort((a, b) => b.total_containers - a.total_containers);
    } else if (sortBy === 'name') {
        filtered.sort((a, b) => a.name.localeCompare(b.name));
    }

    if (filtered.length === 0) {
        containerEl.innerHTML = `
            <div class="glass-card rounded-2xl p-12 text-center text-slate-400">
                <i class="fa-solid fa-filter-circle-xmark text-3xl mb-3 text-slate-500"></i>
                <p class="font-medium text-slate-300">No containers matching the criteria</p>
                <p class="text-xs text-slate-500 mt-1">Try resetting search filters or selecting another quick filter</p>
            </div>
        `;
        return;
    }

    // Determine the heaviest visible row for bar scaling
    let maxCpu = 0;
    let maxMemBytes = 0;
    const cpus = latestData.system?.cpus || 1;
    const hostMemTotal = latestData.system?.mem_total_bytes || 1;

    for (const stack of filtered) {
        if (stack.total_cpu > maxCpu) maxCpu = stack.total_cpu;
        if (stack.total_mem_bytes > maxMemBytes) maxMemBytes = stack.total_mem_bytes;
        const isExp = (q && q.length > 0) || expandedStacks.has(stack.key);
        if (isExp) {
            for (const c of stack.displayContainers) {
                if (c.cpu_perc > maxCpu) maxCpu = c.cpu_perc;
                if (c.mem_used_bytes > maxMemBytes) maxMemBytes = c.mem_used_bytes;
            }
        }
    }
    if (maxCpu <= 0) maxCpu = 1;
    if (maxMemBytes <= 0) maxMemBytes = 1;

    const calcBarWidth = (val, max) => {
        if (!val || val <= 0) return 0;
        return Math.max(3, Math.min(100, (val / max) * 100));
    };

    let html = '';
    for (const stack of filtered) {
        const isExpanded = (q && q.length > 0) || expandedStacks.has(stack.key);
        const state = stack.containers.some(needsAttention) ? 'warning' : stack.containers.some(isRunning) ? '' : 'inactive';
        const cpuBarWidth = calcBarWidth(stack.total_cpu, maxCpu);
        const memBarWidth = calcBarWidth(stack.total_mem_bytes, maxMemBytes);
        const hostCpuPct = stack.total_cpu / cpus;
        const hostMemPct = stack.host_mem_perc || (stack.total_mem_bytes / hostMemTotal * 100);

        html += `<section class="stack-group">
            <div role="button" tabindex="0" aria-expanded="${Boolean(isExpanded)}" data-action="stack" data-value="${escapeHtml(stack.key)}" class="stack-row">
                <div class="stack-name">
                    <span class="state-dot ${state}"></span>
                    <h2 class="stack-title">${escapeHtml(stack.name)}</h2>
                    <span class="stack-count">${stack.displayContainers.length} containers</span>
                </div>
                ${rowMetric(`${stack.total_cpu.toFixed(1)}%`, cpuBarWidth, hostCpuPct, hostCpuPct > 25)}
                ${rowMetric(formatBytesNoDec(stack.total_mem_bytes), memBarWidth, hostMemPct, hostMemPct > 25)}
                <div></div>
                <span class="stack-chevron" aria-hidden="true"><i class="fa-solid fa-chevron-right"></i></span>
            </div>
            ${isExpanded ? `<div>${stack.displayContainers.map(c => renderContainerRow(c, maxCpu, maxMemBytes)).join('')}</div>` : ''}
        </section>`;
    }
    const allExpanded = Object.keys(view.stacks).every(key => expandedStacks.has(key));
    const expand = document.getElementById('expandAll');
    expand.setAttribute('aria-label', allExpanded ? 'Collapse all stacks' : 'Expand all stacks');
    expand.title = expand.getAttribute('aria-label');
    expand.textContent = allExpanded ? '−' : '↕';

    containerEl.innerHTML = html;
}

// Stopped containers may be retired, manually stopped, or completed jobs.
// A stop alone does not establish a failure or an expectation to keep running.
function needsAttention(c) { return ['unhealthy','dead','restarting'].includes(c.health); }
function isRunning(c) { return c.state === 'running' || (!c.state && ['healthy','running','unhealthy','starting'].includes(c.health)); }
function isStopped(c) { return (c.state || c.health) === 'exited'; }
function matchesQuickFilter(c, filter) {
    if (filter === 'issues') return needsAttention(c);
    if (filter === 'running') return isRunning(c);
    if (filter === 'stopped') return isStopped(c);
    if (filter === 'paused') return (c.state || c.health) === 'paused';
    if (filter === 'starting') return c.health === 'starting' || (c.state || c.health) === 'created';
    if (filter === 'ports') return /->/.test(c.ports || '');
    return true;
}
function rowMetric(label, barWidthPct, hostPct, isHigh) {
    const width = Math.max(0, Math.min(100, barWidthPct || 0));
    const hostText = Number.isFinite(hostPct) ? `${hostPct.toFixed(1)}% of host` : '';
    const tooltip = hostText ? `${label} (${hostText})` : label;
    const barClass = isHigh ? 'warning' : '';
    return `<div class="row-metric" title="${escapeHtml(tooltip)}"><span>${escapeHtml(label)}</span><span class="thin-bar"><span class="${barClass}" style="width:${width}%"></span></span></div>`;
}
function renderContainerRow(container, maxCpu, maxMemBytes) {
    const c = escapedView(container);
    const cpus = latestData.system?.cpus || 1;
    const hostMemTotal = latestData.system?.mem_total_bytes || 1;
    const cCpuBarWidth = container.cpu_perc > 0 ? Math.max(3, Math.min(100, (container.cpu_perc / maxCpu) * 100)) : 0;
    const cMemBarWidth = container.mem_used_bytes > 0 ? Math.max(3, Math.min(100, (container.mem_used_bytes / maxMemBytes) * 100)) : 0;
    const cHostCpuPct = container.cpu_perc / cpus;
    const cHostMemPct = (container.mem_used_bytes / hostMemTotal) * 100;

    return `<div class="container-row" role="button" tabindex="0" data-action="inspect" data-value="${escapeHtml(c.name)}">
        <div class="container-col-name">
            <span class="state-dot ${needsAttention(container) ? 'warning' : isRunning(container) ? '' : 'inactive'}"></span>
            <strong class="container-title">${escapeHtml(c.display_name || c.name)}</strong>
            <span class="container-state-label">${escapeHtml(isStopped(container) ? 'stopped' : (c.health || c.state || ''))}</span>
        </div>
        ${rowMetric(`${c.cpu_perc.toFixed(1)}%`, cCpuBarWidth, cHostCpuPct, cHostCpuPct > 25)}
        ${rowMetric(c.mem_used_formatted || '0 B', cMemBarWidth, cHostMemPct, cHostMemPct > 25)}
        <div></div>
        <span class="container-drawer-trigger" aria-hidden="true">&rsaquo;</span>
    </div>`;
}

async function inspectContainer(name) {
    const c = findContainer(name);
    const displayName = c ? (c.display_name || c.name) : name;
    const title = displayName;
    const subtitle = c ? `${c.stack} · ${isStopped(c) ? 'Stopped' : (c.health || c.state || 'Running')}` : 'Container details';

    const formattedImage = c ? formatImageName(c.image) : '—';
    const portsHtml = c ? renderPortChips(c.ports) : '<span class="muted text-xs">No ports published</span>';
    const netRx = c?.net_rx || '—';
    const netTx = c?.net_tx || '—';
    const cpuStr = c ? `${c.cpu_perc.toFixed(1)}%` : '—';
    const ramStr = c ? (c.mem_used_formatted || '0 B') : '—';

    const initialContent = `
        <div class="space-y-3">
            <div class="drawer-section">
                <div class="flex items-center justify-between mb-3">
                    <span class="drawer-section-title"><i class="fa-solid fa-gauge-high text-indigo-400"></i> Status &amp; Performance</span>
                    <button data-action="logs" data-value="${escapeHtml(name)}" class="drawer-btn-logs"><i class="fa-solid fa-terminal text-cyan-400"></i> Logs</button>
                </div>
                <div class="drawer-grid-4">
                    <div class="drawer-stat-box">
                        <div class="drawer-stat-label">Exit Code</div>
                        <div id="drawerExitCode" class="drawer-stat-value">${c && isStopped(c) ? 'Checking…' : '0'}</div>
                    </div>
                    <div class="drawer-stat-box">
                        <div class="drawer-stat-label">Restarts</div>
                        <div id="drawerRestarts" class="drawer-stat-value">Checking…</div>
                    </div>
                    <div class="drawer-stat-box">
                        <div class="drawer-stat-label">CPU</div>
                        <div class="drawer-stat-value">${escapeHtml(cpuStr)}</div>
                    </div>
                    <div class="drawer-stat-box">
                        <div class="drawer-stat-label">RAM</div>
                        <div class="drawer-stat-value">${escapeHtml(ramStr)}</div>
                    </div>
                </div>
            </div>

            <div class="drawer-section">
                <div class="drawer-section-title"><i class="fa-solid fa-box text-purple-400"></i> Image</div>
                <div class="flex items-center justify-between gap-2 p-2 bg-slate-950 rounded-lg border border-slate-800">
                    <code class="text-xs font-mono text-purple-300 break-all select-all">${escapeHtml(formattedImage)}</code>
                    <button data-action="copy" data-value="${escapeHtml(formattedImage)}" class="text-xs px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700/60 shrink-0">Copy</button>
                </div>
            </div>

            <div class="drawer-section">
                <div class="drawer-section-title"><i class="fa-solid fa-network-wired text-cyan-400"></i> Published Ports</div>
                <div class="flex flex-wrap gap-1.5">${portsHtml}</div>
            </div>

            <div class="drawer-section">
                <div class="drawer-section-title"><i class="fa-solid fa-arrow-right-arrow-left text-emerald-400"></i> Network I/O</div>
                <div class="flex items-center gap-4 text-xs font-mono text-slate-300 p-2 bg-slate-950 rounded-lg border border-slate-800">
                    <span><i class="fa-solid fa-arrow-down text-emerald-400"></i> Received: <strong>${escapeHtml(netRx)}</strong></span>
                    <span><i class="fa-solid fa-arrow-up text-cyan-400"></i> Transmitted: <strong>${escapeHtml(netTx)}</strong></span>
                </div>
                <div id="drawerNetworks" class="mt-2 space-y-1 text-xs font-mono text-slate-400">
                    <p class="text-[11px] text-slate-500">Inspecting networks…</p>
                </div>
            </div>

            <div class="drawer-section">
                <div class="drawer-section-title"><i class="fa-solid fa-folder-tree text-amber-400"></i> Mounts &amp; Volumes</div>
                <div id="drawerMounts" class="space-y-1.5">
                    <p class="text-[11px] text-slate-500">Inspecting mounts…</p>
                </div>
            </div>
        </div>
    `;

    openModal(title, subtitle, initialContent, 'fa-cube text-indigo-400', () => inspectContainer(name));

    const generation = modalGeneration;
    try {
        const res = await fetch(`/api/inspect?name=${encodeURIComponent(name)}`);
        if (!res.ok) throw new Error('Could not fetch inspection data');
        const data = escapedView(await res.json());
        if (generation !== modalGeneration) return;

        const state = data.State || {};
        const mounts = data.Mounts || [];
        const net = (data.NetworkSettings && data.NetworkSettings.Networks) ? data.NetworkSettings.Networks : {};

        const exitEl = document.getElementById('drawerExitCode');
        if (exitEl) exitEl.textContent = state.ExitCode ?? '—';

        const restartEl = document.getElementById('drawerRestarts');
        if (restartEl) restartEl.textContent = data.RestartCount ?? 0;

        const netEl = document.getElementById('drawerNetworks');
        if (netEl) {
            const netKeys = Object.keys(net);
            netEl.innerHTML = netKeys.length ? netKeys.map(k => `
                <div class="flex items-center justify-between p-1.5 bg-slate-900 rounded border border-slate-800 text-[11px]">
                    <span class="text-slate-300 font-semibold">${escapeHtml(k)}</span>
                    <span class="text-cyan-400">IP: ${net[k].IPAddress || 'None'}</span>
                </div>
            `).join('') : '<p class="text-[11px] text-slate-500">No networks attached</p>';
        }

        const mountsEl = document.getElementById('drawerMounts');
        if (mountsEl) {
            mountsEl.innerHTML = mounts.length ? mounts.map(m => `
                <div class="p-2 bg-slate-950 rounded border border-slate-800 text-xs font-mono">
                    <div class="flex items-center justify-between text-[10px] text-slate-400 mb-0.5">
                        <span class="uppercase font-bold text-indigo-400">${escapeHtml(m.Type || 'mount')}</span>
                        <span>${m.RW ? 'Read-Write' : 'Read-Only'}</span>
                    </div>
                    <div class="text-slate-300 truncate" title="${escapeHtml(m.Source)}">${escapeHtml(m.Source)}</div>
                    <div class="text-cyan-400 truncate" title="${escapeHtml(m.Destination)}">&rarr; ${escapeHtml(m.Destination)}</div>
                </div>
            `).join('') : '<p class="text-[11px] text-slate-500">No mounts found</p>';
        }
    } catch (err) {
        if (generation !== modalGeneration) return;
        const exitEl = document.getElementById('drawerExitCode');
        if (exitEl) exitEl.textContent = '—';
        const restartEl = document.getElementById('drawerRestarts');
        if (restartEl) restartEl.textContent = '—';
    }
}

let logTimer = null;
let logAbort = null;
let logGeneration = 0;
let logState = null;
async function viewLogs(name) {
    openModal(`Container Logs: ${name}`, 'Latest 100 lines · bounded to 1 MiB · may contain application secrets',
        `<div class="flex flex-wrap gap-2 mb-3">
            <input id="logSearch" type="search" placeholder="Search log lines" aria-label="Search log lines" class="bg-slate-950 border border-slate-700 rounded px-3 py-1 text-sm">
            <button data-action="log-pause" id="logPause" class="text-xs px-3 py-1 rounded bg-slate-800">Pause</button>
            <label class="text-xs flex items-center gap-1"><input id="logTimestamps" type="checkbox" checked> Timestamps</label>
            <button data-action="copy-logs" class="text-xs px-3 py-1 rounded bg-slate-800">Copy</button>
            <button data-action="log-download" class="text-xs px-3 py-1 rounded bg-slate-800">Download</button>
            <span id="logStatus" class="text-xs text-slate-400" role="status">Loading…</span>
        </div><pre id="logText" class="bg-slate-950 p-4 rounded-xl border border-slate-800 text-slate-300 font-mono text-xs whitespace-pre-wrap break-all max-h-[55vh] overflow-y-auto select-text"></pre>`,
        'fa-terminal text-cyan-400', () => refreshLogs());
    logState = {name, paused: false, text: ''};
    document.getElementById('logSearch').addEventListener('input', renderLogs);
    document.getElementById('logTimestamps').addEventListener('change', () => refreshLogs());
    await refreshLogs();
}
function renderLogs() {
    if (!logState || !document.getElementById('logText')) return;
    const query = document.getElementById('logSearch').value.toLowerCase();
    const pre = document.getElementById('logText');
    const follow = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
    pre.textContent = logState.text.split('\n').filter(line => !query || line.toLowerCase().includes(query)).join('\n') || 'No matching log output';
    if (follow) pre.scrollTop = pre.scrollHeight;
}
async function refreshLogs() {
    if (!logState || document.hidden) return;
    clearTimeout(logTimer);
    if (logAbort) logAbort.abort();
    logAbort = new AbortController();
    const generation = ++logGeneration;
    const state = logState;
    try {
        const timestamps = document.getElementById('logTimestamps').checked ? 1 : 0;
        const res = await fetch(`/api/logs?name=${encodeURIComponent(state.name)}&tail=100&timestamps=${timestamps}`, {signal: logAbort.signal});
        if (!res.ok) throw new Error('Logs unavailable');
        const text = await res.text();
        if (generation !== logGeneration || logState !== state) return;
        state.text = text;
        renderLogs();
        document.getElementById('logStatus').textContent = 'Sampled ' + new Date().toLocaleTimeString();
    } catch (error) {
        if (error.name !== 'AbortError' && logState === state && generation === logGeneration) document.getElementById('logStatus').textContent = 'Logs unavailable';
    } finally {
        if (logState === state && generation === logGeneration && !state.paused && !document.hidden) logTimer = setTimeout(refreshLogs, 3000);
    }
}
function stopLogs() {
    clearTimeout(logTimer);
    if (logAbort) logAbort.abort();
    logGeneration++;
    logState = null;
}
function downloadLogs() {
    if (!logState) return;
    const url = URL.createObjectURL(new Blob([logState.text], {type:'text/plain;charset=utf-8'}));
    const link = document.createElement('a');
    link.href = url;
    link.download = logState.name + '.log';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function openModal(title, subtitle, content, iconClass, onRefresh) {
    stopLogs();
    modalGeneration++;
    if (document.getElementById('modalOverlay').classList.contains('hidden')) modalPreviousFocus = document.activeElement;
    document.getElementById('modalTitle').textContent = title;
    document.getElementById('modalSubtitle').textContent = subtitle;
    document.getElementById('modalContent').innerHTML = content;
    document.getElementById('modalIcon').className = `w-10 h-10 rounded-xl bg-indigo-500/20 flex items-center justify-center shrink-0 ${iconClass}`;
    document.getElementById('modalActionBtn').onclick = onRefresh;
    document.getElementById('modalOverlay').classList.remove('hidden');
    document.querySelector('[data-action=close]').focus();
}

function closeModal() {
    stopLogs();
    modalGeneration++;
    modalPreviousFocus?.focus();
    document.getElementById('modalOverlay').classList.add('hidden');
}

window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeModal();
});

function portUrl(port) {
    const url = new URL(location.href);
    url.port = port;
    url.pathname = '/'; url.search = ''; url.hash = '';
    return url.href;
}
function renderIssues(data) {
    const rows = Object.values(data.stacks || {}).flatMap(stack => stack.containers || []);
    const issuesCount = rows.filter(needsAttention).length;
    const runningCount = rows.filter(isRunning).length;
    const stoppedCount = rows.filter(isStopped).length;
    const allCount = rows.length;

    const summary = document.getElementById('issuesSummary');
    summary.textContent = issuesCount ? `${issuesCount} ${issuesCount === 1 ? 'needs' : 'need'} attention` : 'No active issues';
    summary.classList.toggle('attention', issuesCount > 0);
    summary.dataset.value = issuesCount ? 'issues' : 'all';

    const countAllEl = document.getElementById('count-all');
    if (countAllEl) countAllEl.textContent = allCount;

    // Hide stopped segments whose count is 0
    const stoppedBtn = document.getElementById('qf-stopped');
    if (stoppedBtn) {
        if (stoppedCount === 0) {
            stoppedBtn.hidden = true;
            if (activeQuickFilter === 'stopped') setQuickFilter('all');
        } else {
            stoppedBtn.hidden = false;
            stoppedBtn.textContent = `Stopped (${stoppedCount})`;
        }
    }

    // Hide issues segments whose count is 0
    const issuesBtn = document.getElementById('qf-issues');
    if (issuesBtn) {
        if (issuesCount === 0) {
            issuesBtn.hidden = true;
            if (activeQuickFilter === 'issues') setQuickFilter('all');
        } else {
            issuesBtn.hidden = false;
            issuesBtn.textContent = `Needs attention (${issuesCount})`;
        }
    }

    // Show Running only when it differs from All
    const runningBtn = document.getElementById('qf-running');
    if (runningBtn) {
        if (runningCount === allCount || runningCount === 0) {
            runningBtn.hidden = true;
            if (activeQuickFilter === 'running') setQuickFilter('all');
        } else {
            runningBtn.hidden = false;
            runningBtn.textContent = `Running (${runningCount})`;
        }
    }
}
function relativeTime(timestamp) {
    return timestamp ? `${Math.max(0, Math.floor(Date.now() / 1000 - timestamp))}s ago` : 'No sample yet';
}
function updateSystemIndicator(data, failed = false) {
    const age = data?.timestamp ? Math.max(0, Math.floor(Date.now()/1000 - data.timestamp)) : null;
    const stale = !data || data.stale || age === null || age > (data.stale_after || 30);
    const error = failed || Boolean(data?.last_error);
    document.getElementById('statusText').textContent = error ? 'Poll failed' : stale ? `Stale · ${age ?? '—'}s ago` : `Live · ${age}s`;
    document.getElementById('liveDot').className = `state-dot ${error ? 'critical' : stale ? 'warning' : ''}`;
    const warning = document.getElementById('dataWarning');
    warning.textContent = error ? 'Polling failed · Check system status' : 'Data is stale · Check system status';
    warning.classList.toggle('hidden', !error && !stale);
}
const historyChart = new HostHistoryChart(document.getElementById('hostHistory'), document.getElementById('historyTooltip'), document.getElementById('historyStatus'));
document.querySelectorAll('[data-range]').forEach(button => button.addEventListener('click', () => historyChart.setRange(Number(button.dataset.range))));
async function showDiagnostics() {
    openModal('System status', 'Docker connectivity, measurement availability and polling cost', '<div id="diagnosticsPanel">Loading…</div>', 'fa-circle-info', showDiagnostics);
    const generation = modalGeneration;
    try {
        const res = await fetch('/api/diagnostics');
        if (!res.ok) throw new Error();
        const data = await res.json();
        if (generation !== modalGeneration) return;
        const row = (title, ok, detail) => `<div class="diagnostic-row"><span class="${ok ? 'check-ok' : 'check-fail'}">${ok ? '✓' : '✗'}</span><div><strong>${escapeHtml(title)}</strong><p class="muted">${escapeHtml(detail)}</p></div></div>`;
        let content = row('Docker connection', data.docker.ok, `${data.backend || 'No backend'} · Socket ${data.socket_exists ? data.socket_accessible ? 'accessible' : 'not accessible' : 'missing'}. ${data.docker.reason} ${data.docker.hint}`);
        content += row('Data freshness', data.freshness.ok, `Docker ${relativeTime(data.last_success)} · Host ${relativeTime(data.host_last_sample)} · Polling every ${data.poll_interval}s. ${data.freshness.reason} ${data.freshness.hint}`);
        for (const [key, measurement] of Object.entries(data.measurements)) content += row(key, measurement.ok, `${measurement.reason} ${measurement.hint}`);
        content += `<div class="diagnostic-facts"><div>Poll duration <span>${data.poll_timing.last_ms?.toFixed(1) ?? '—'} ms last / ${data.poll_timing.average_ms?.toFixed(1) ?? '—'} ms average</span></div><div>Dockpit cost <span>${data.process.cpu_pct?.toFixed(2) ?? '—'}% CPU · ${data.process.ram_bytes == null ? 'Unavailable' : formatBytesNoDec(data.process.ram_bytes)} RAM</span></div><div>Containers <span>${data.hidden_containers} hidden / ${data.total_containers} total</span></div><div>History buffer <span>${data.history.size} / ${data.history.capacity} samples</span></div></div>`;
        if (data.last_error) content += row('Last error', false, `${data.last_error} ${data.last_error_hint}`);
        content += '<details class="raw-details"><summary>Raw</summary><button data-action="copy-diagnostics">Copy</button><pre id="diagnosticsText"></pre></details>';
        document.getElementById('diagnosticsPanel').innerHTML = content;
        document.getElementById('diagnosticsText').textContent = JSON.stringify(data, null, 2);
    } catch {
        if (generation === modalGeneration) document.getElementById('diagnosticsPanel').textContent = 'System status unavailable. Check the Dockpit backend connection and reload.';
    }
}
document.addEventListener('click', event => {
    const button = event.target.closest('[data-action]');
    if (!button || event.target.closest('a')) return;
    const value = button.dataset.value;
    const actions = {
        close: closeModal, refresh: () => fetchStats(true), auto: toggleAutoRefresh,
        expand: () => toggleExpandAll(!Object.keys(dashboardData()?.stacks || {}).every(key => expandedStacks.has(key))), stack: () => toggleStack(value),
        card: () => openTopCardModal(value), filter: () => setQuickFilter(value),
        inspect: () => inspectContainer(value), logs: () => viewLogs(value),
        copy: () => copyText(value, button), 'copy-logs': () => copyText(document.getElementById('logText')?.textContent, button),
        organize: () => organizeStacks(value), 'stack-assign': () => assignStack(), 'stack-reset': () => assignStack(true),
        'stack-select': () => { document.querySelectorAll('.stack-choice:not([hidden]) input').forEach(input => { input.checked = true; }); updateStackSelection(); },
        'stack-clear': () => { document.querySelectorAll('#stackChoices input').forEach(input => { input.checked = false; }); updateStackSelection(); },
        diagnostics: showDiagnostics, 'copy-diagnostics': () => copyText(document.getElementById('diagnosticsText')?.textContent, button), 'log-download': downloadLogs,
        'log-pause': () => {
            if (!logState) return;
            logState.paused = !logState.paused;
            button.textContent = logState.paused ? 'Resume' : 'Pause';
            if (logState.paused) clearTimeout(logTimer); else refreshLogs();
        },
    };
    actions[button.dataset.action]?.();
});
document.getElementById('searchInput').value = typeof preferences.search === 'string' ? preferences.search : '';
if (['cpu','mem','containers','name'].includes(preferences.sort)) document.getElementById('sortBy').value = preferences.sort;
document.getElementById('searchInput').addEventListener('input', renderDashboard);
document.getElementById('sortBy').addEventListener('change', renderDashboard);
setQuickFilter(activeQuickFilter);
fetchStats();
startInterval();

document.addEventListener('keydown', event => { if ((event.key === 'Enter' || event.key === ' ') && event.target.matches('[role=button][data-action]')) { event.preventDefault(); event.target.click(); } });

document.addEventListener('keydown', event => {
    const overlay = document.getElementById('modalOverlay');
    if (event.key !== 'Tab' || overlay.classList.contains('hidden')) return;
    const focusable = [...overlay.querySelectorAll('button, input, select, a[href], [tabindex="0"]')].filter(el => !el.disabled && el.getClientRects().length);
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
});

document.addEventListener('click', event => { const filters = document.querySelector('.filters'); if (!filters.contains(event.target) || event.target.closest('.qf-btn')) filters.open = false; });

historyChart.start();
document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
        clearInterval(refreshInterval);
        statsAbort?.abort();
        historyChart.stop();
        clearTimeout(logTimer);
        logAbort?.abort();
    } else {
        if (autoRefresh) { fetchStats(); startInterval(); historyChart.start(); }
        if (logState && !logState.paused) refreshLogs();
    }
});
