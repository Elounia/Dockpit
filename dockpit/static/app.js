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
    })); } catch { /* Storage can be disabled by the browser. */ }
}
const preferences = loadPreferences();
let latestData = null;
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
async function fetchStats(force = false) {
    if (statsPending) return;
    statsPending = true;
    try {
        const res = await fetch('/api/stats');
        if (!res.ok) throw new Error('API returned status ' + res.status);
        const data = await res.json();
        latestData = data;

        updateTopKPIs(data);
        renderDashboard();

        const sampled = new Date(data.timestamp * 1000);
        document.getElementById('statusText').textContent = (data.stale ? 'Stale · last sample ' : 'Sampled ') + sampled.toLocaleTimeString();
        const warning = document.getElementById('dataWarning');
        warning.textContent = data.stale ? 'Docker measurements are stale. Open Diagnostics to check access and polling.' : '';
        warning.classList.toggle('hidden', !data.stale);
        renderIssues(data);
        refreshHistory();
    } catch (err) {
        console.error('Error fetching stats:', err);
        document.getElementById('statusText').textContent = 'Connection error';
        const warning = document.getElementById('dataWarning');
        warning.textContent = 'Measurements could not be refreshed. Displayed values may be outdated.';
        warning.classList.remove('hidden');
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

function updateTopKPIs(data) {
    const sys = data.system || {}, gpu = sys.gpu || {}, devices = gpu.devices || [];
    const text = (id, value) => { document.getElementById(id).textContent = value; };
    const bar = (id, value) => {
        const el = document.getElementById(id);
        el.style.width = `${Math.min(100, Math.max(0, value || 0))}%`;
        el.className = value >= 90 ? 'critical' : value >= 75 ? 'warning' : '';
    };
    text('sysCpu', sys.host_cpu_pct == null ? 'Unavailable' : `${sys.host_cpu_pct.toFixed(1)}%`);
    text('sysCpuCores', `${sys.cpus || 1} cores · Docker ${(data.docker_host_cpu_perc || 0).toFixed(1)}%`);
    bar('sysCpuBar', sys.host_cpu_pct);
    text('sysRam', sys.mem_used_formatted || 'Unavailable');
    text('sysRamSub', `of ${sys.mem_total_formatted || '—'} · ${sys.mem_pct ?? '—'}%`);
    bar('sysRamBar', sys.mem_pct);
    const disk = sys.root_disk || {};
    text('sysNvmeStats', disk.total_bytes == null ? 'Unavailable' : formatBytesNoDec(disk.used_bytes));
    text('storageSub', `of ${formatBytesNoDec(disk.total_bytes)} · ${disk.pct ?? '—'}% used`);
    bar('sysNvmeBar', disk.pct);
    const total = data.total_containers || 0, running = data.running_containers || 0;
    text('sysHealth', `${running} / ${total}`);
    text('sysUptime', `${data.total_stacks || 0} stacks · Up ${sys.uptime_str || '—'}`);
    text('count-all', total);
    bar('sysHealthBar', total ? running / total * 100 : 0);
    document.getElementById('sysHealthBar').className = '';
    text('sysGpu', gpu.utilization_pct != null ? `${gpu.utilization_pct.toFixed(1)}%` : devices.length ? devices.map(d => d.name).join(', ') : 'No GPU');
    document.getElementById('sysGpu').classList.toggle('no-metrics', gpu.utilization_pct == null);
    text('sysGpuName', gpu.utilization_pct != null ? devices.map(d => d.name).join(', ') : devices.length ? 'No usage data' : 'No GPU detected');
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
        Object.keys(latestData.stacks).forEach(s => expandedStacks.add(s));
    } else {
        expandedStacks.clear();
    }
    renderDashboard();
}

function setQuickFilter(type) {
    activeQuickFilter = type;
    document.querySelectorAll('.qf-btn').forEach(btn => {
        btn.className = 'qf-btn text-xs px-3 py-1 rounded-lg bg-slate-800/80 hover:bg-slate-700 text-slate-300 border border-slate-700/60 transition cursor-pointer';
    });
    const activeBtn = document.getElementById('qf-' + type);
    if (activeBtn) {
        activeBtn.className = 'qf-btn text-xs px-3 py-1 rounded-lg bg-indigo-600 text-white font-medium transition cursor-pointer';
    }
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
    } else {
        btnText.textContent = 'Start';
        btnIcon.className = 'fa-solid fa-play';
        clearInterval(refreshInterval);
    }
}

function startInterval() {
    clearInterval(refreshInterval);
    refreshInterval = setInterval(() => {
        if (autoRefresh) fetchStats();
    }, 2500);
}

// ==========================================
// TOP 4 CARDS INTERACTIVE MODALS
// ==========================================
function openTopCardModal(type) {
    if (!latestData) return;
    const sys = escapedView(latestData.system || {});
    const details = sys.details || {};

    if (type === 'cpu') {
        renderCpuDetailsModal(sys, details.cpu || {});
    } else if (type === 'memory') {
        renderMemoryDetailsModal(sys, details.memory || {});
    } else if (type === 'storage') {
        renderStorageDetailsModal(sys, details.storage || {});
    } else if (type === 'system') {
        renderSystemDetailsModal(sys, details.docker || {});
    }
}

function renderCpuDetailsModal(sys, cpu) {
    const view = escapedView(latestData);
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
    const view = escapedView(latestData);
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
    const view = escapedView(latestData);
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
    const view = escapedView(latestData);
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

function renderDashboard() {
    const view = escapedView(latestData);
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
                   c.image.toLowerCase().includes(q) ||
                   c.service.toLowerCase().includes(q) ||
                   (c.storage_tier && c.storage_tier.toLowerCase().includes(q)) ||
                   (c.ports && c.ports.toLowerCase().includes(q));

            if (!matchesSearch) return false;

            if (activeQuickFilter === 'issues') return ['unhealthy', 'exited', 'dead', 'restarting'].includes(c.health);
            if (activeQuickFilter === 'cpu') return c.cpu_perc >= 0.5;
            if (activeQuickFilter === 'mem') return c.mem_used_bytes >= (250 * 1024 * 1024);
            if (activeQuickFilter === 'ports') return Boolean(c.ports && c.ports.trim().length > 0);
            if (activeQuickFilter === 'secondary') return Boolean(c.storage_tier && c.storage_tier.includes('Secondary'));
            if (activeQuickFilter === 'root') return Boolean(c.storage_tier && c.storage_tier.includes('Root') && !c.storage_tier.includes('Secondary'));
            return true;
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

    let html = '';
    for (const stack of filtered) {
        const isExpanded = (q && q.length > 0) || expandedStacks.has(stack.key);
        const sIcon = getStackIcon(stack.name);
        const totalMem = stack.total_mem_formatted || formatBytesNoDec(stack.total_mem_bytes);

        html += `
        <div class="glass-card rounded-2xl overflow-hidden border border-slate-800 transition">
            <!-- Stack Header / Accordion trigger -->
            <div role="button" tabindex="0" data-action="stack" data-value="${escapeHtml(stack.key)}"
                 class="p-3.5 bg-slate-900/80 hover:bg-slate-850/90 cursor-pointer flex flex-col md:flex-row md:items-center justify-between gap-3 select-none">
                <div class="flex items-center gap-3">
                    <div class="w-8 h-8 rounded-xl ${sIcon.bg} ${sIcon.border} border flex items-center justify-center ${sIcon.color}">
                        <i class="fa-solid ${sIcon.icon} text-sm"></i>
                    </div>
                    <div>
                        <div class="flex items-center gap-2">
                            <h2 class="text-base font-bold text-white tracking-tight">${stack.name}</h2>
                            <span class="text-xs text-slate-400 font-mono bg-slate-800 px-2 py-0.5 rounded-full border border-slate-700/60">
                                ${stack.displayContainers.length}${stack.displayContainers.length !== stack.total_containers ? ` of ${stack.total_containers}` : ''} containers
                            </span>
                        </div>
                    </div>
                </div>

                <div class="flex items-center gap-4 text-xs font-mono">
                    <div class="flex items-center gap-1.5 text-slate-300">
                        <i class="fa-solid fa-microchip text-amber-400 text-[11px]"></i>
                        <span>${stack.total_cpu.toFixed(1)}% CPU</span>
                    </div>
                    <div class="flex items-center gap-1.5 text-slate-300">
                        <i class="fa-solid fa-memory text-purple-400 text-[11px]"></i>
                        <span>${totalMem} RAM</span>
                        <span class="text-slate-500 text-[10px]">(${stack.docker_mem_share || 0}% Docker)</span>
                    </div>
                    <div class="text-slate-400 pl-2">
                        <i class="fa-solid fa-chevron-${isExpanded ? 'up' : 'down'} text-xs text-slate-500"></i>
                    </div>
                </div>
            </div>

            <!-- Containers List -->
            ${isExpanded ? `
            <div class="divide-y divide-slate-800/80 border-t border-slate-800/80">
                ${stack.displayContainers.map(c => renderContainerRow(c, stack)).join('')}
            </div>
            ` : ''}
        </div>
        `;
    }

    containerEl.innerHTML = html;
}

function renderContainerRow(c, stack) {
    const h = getHealthBadge(c.health);

    // Format ports nicely with links
    let portsHtml = '';
    if (c.ports) {
        const portItems = c.ports.split(',').map(p => p.trim()).filter(Boolean);
        portsHtml = portItems.map(p => {
            const match = p.match(/(?:^|:)(\d+)-(?:>|&gt;)/);
            if (match) {
                const portNum = match[1];
                return `<a href="${escapeHtml(portUrl(portNum))}" target="_blank" rel="noopener noreferrer" title="Open port in browser" class="text-cyan-400 hover:text-cyan-300 hover:underline bg-cyan-950/60 border border-cyan-800/40 px-1.5 py-0.5 rounded text-[11px] font-mono flex items-center gap-1"><i class="fa-solid fa-arrow-up-right-from-square text-[9px]"></i>${portNum}</a>`;
            }
            return `<span class="text-slate-400 bg-slate-800/70 border border-slate-700/50 px-1.5 py-0.5 rounded text-[11px] font-mono">${p}</span>`;
        }).join('');
    }

    // CPU Meter calculations
    const cpuBarWidth = Math.min(100, Math.max(2, (c.cpu_perc / 100) * 100));
    const cpuColor = c.cpu_perc > 50 ? 'bg-rose-500' : (c.cpu_perc > 10 ? 'bg-amber-400' : 'bg-amber-400/80');

    // RAM Meter calculations
    let memBarWidth = 2;
    let memBarLabel = c.mem_used_formatted || '0 B';
    let memSubLabel = '';
    let memColor = 'bg-purple-500';

    if (c.has_custom_limit) {
        memBarWidth = Math.min(100, Math.max(2, c.mem_perc || 0));
        memSubLabel = `${Math.round(c.mem_perc)}% of ${c.mem_limit_formatted}`;
        if (c.mem_perc > 85) memColor = 'bg-rose-500';
    } else {
        const shareOfStack = stack.total_mem_bytes > 0 ? Math.round((c.mem_used_bytes / stack.total_mem_bytes) * 100) : 0;
        memBarWidth = Math.min(100, Math.max(2, shareOfStack));
        memSubLabel = `${shareOfStack}% of stack`;
    }

    return `
    <div class="p-3.5 hover:bg-slate-900/50 transition flex flex-col xl:flex-row xl:items-center justify-between gap-3">
        <!-- Info Column -->
        <div class="flex-1 min-w-0 space-y-1">
            <div class="flex items-center gap-2 flex-wrap">
                <span class="w-2.5 h-2.5 rounded-full ${h.color === 'text-emerald-400' ? 'bg-emerald-400' : (h.color === 'text-blue-400' ? 'bg-blue-400' : (h.color === 'text-rose-400' ? 'bg-rose-400' : 'bg-slate-500'))} inline-block shrink-0 shadow-sm" title="${h.label}"></span>
                <h3 class="text-sm font-bold text-white truncate max-w-md hover:text-indigo-400 transition cursor-pointer" data-action="inspect" data-value="${c.name}">
                    <i class="fa-solid ${c.icon || 'fa-cube'} text-slate-400"></i> ${c.display_name || c.name}
                </h3>
                <span class="text-[10px] font-mono text-slate-400 bg-slate-800/80 border border-slate-700/60 px-1.5 py-0.5 rounded">
                    ${c.id}
                </span>
                <span class="text-[11px] font-medium px-2 py-0.5 rounded-full ${h.bg} ${h.color} ${h.border} border flex items-center gap-1">
                    <i class="fa-solid ${h.icon} text-[10px]"></i> ${h.label}
                </span>
                ${c.service && c.service !== c.name ? `
                <span class="text-[10px] text-slate-400 bg-slate-900 border border-slate-800 px-2 py-0.5 rounded font-mono">
                    svc: ${c.service}
                </span>
                ` : ''}
            </div>

            <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-400">
                <span class="truncate max-w-sm text-slate-400" title="${c.image}">
                    <i class="fa-solid fa-box text-slate-500 mr-1"></i> ${c.image}
                </span>
                <span><i class="fa-regular fa-clock text-slate-500 mr-1"></i> ${c.created}</span>
                ${portsHtml ? `<div class="flex flex-wrap items-center gap-1.5">${portsHtml}</div>` : ''}
            </div>
        </div>

        <!-- Metrics Grid -->
        <div class="grid grid-cols-2 lg:grid-cols-4 gap-2.5 w-full xl:w-auto xl:min-w-[580px] shrink-0">
            <!-- CPU Meter -->
            <div class="bg-slate-950/70 p-2.5 rounded-xl border border-slate-800/70 flex flex-col justify-between">
                <div class="flex items-center justify-between text-xs mb-1">
                    <span class="text-slate-400 text-[11px] flex items-center gap-1 font-medium shrink-0"><i class="fa-solid fa-microchip text-[10px] text-amber-400"></i> CPU</span>
                    <span class="text-slate-200 font-bold metric-badge font-mono truncate ml-1">${c.cpu_perc.toFixed(1)}%</span>
                </div>
                <div class="w-full bg-slate-800 rounded-full h-1.5 overflow-hidden">
                    <div class="${cpuColor} h-1.5 rounded-full progress-bar-smooth" style="width: ${cpuBarWidth}%"></div>
                </div>
                <div class="text-[10px] text-slate-400 mt-1 font-mono">PIDs: ${c.pids}</div>
            </div>

            <!-- RAM Meter -->
            <div class="bg-slate-950/70 p-2.5 rounded-xl border border-slate-800/70 flex flex-col justify-between">
                <div class="flex items-center justify-between text-xs mb-1">
                    <span class="text-slate-400 text-[11px] flex items-center gap-1 font-medium shrink-0"><i class="fa-solid fa-memory text-[10px] text-purple-400"></i> RAM</span>
                    <span class="text-slate-200 font-bold metric-badge font-mono truncate ml-1">${memBarLabel}</span>
                </div>
                <div class="w-full bg-slate-800 rounded-full h-1.5 overflow-hidden">
                    <div class="${memColor} h-1.5 rounded-full progress-bar-smooth" style="width: ${memBarWidth}%"></div>
                </div>
                <div class="text-[10px] text-slate-400 mt-1 font-mono truncate" title="${memSubLabel}">${memSubLabel}</div>
            </div>

            <!-- Disk Block I/O -->
            <div class="bg-slate-950/70 p-2.5 rounded-xl border border-slate-800/70 flex flex-col justify-between">
                <div class="text-[11px] text-slate-400 flex items-center justify-between font-medium mb-1">
                    <span class="flex items-center gap-1 shrink-0"><i class="fa-solid fa-hard-drive text-[10px] text-cyan-400"></i> I/O</span>
                    <span class="text-[10px] font-mono text-slate-400 truncate ml-1">${c.storage_tier || 'Root'}</span>
                </div>
                <div class="text-[10px] font-mono space-y-0.5">
                    <div class="flex justify-between text-slate-400">
                        <span>Read:</span> <span class="text-slate-200 font-semibold truncate ml-1">${c.block_read}</span>
                    </div>
                    <div class="flex justify-between text-slate-400">
                        <span>Write:</span> <span class="text-cyan-300 font-semibold truncate ml-1">${c.block_write}</span>
                    </div>
                </div>
            </div>

            <!-- Net I/O -->
            <div class="bg-slate-950/70 p-2.5 rounded-xl border border-slate-800/70 flex flex-col justify-between">
                <div class="text-[11px] text-slate-400 flex items-center gap-1 font-medium mb-1">
                    <i class="fa-solid fa-arrow-right-arrow-left text-[10px] text-emerald-400"></i> Net
                </div>
                <div class="text-[10px] font-mono space-y-0.5">
                    <div class="flex justify-between text-slate-400">
                        <span>Down:</span> <span class="text-emerald-400 font-semibold truncate ml-1">${c.net_rx}</span>
                    </div>
                    <div class="flex justify-between text-slate-400">
                        <span>Up:</span> <span class="text-indigo-300 font-semibold truncate ml-1">${c.net_tx}</span>
                    </div>
                </div>
            </div>
        </div>

        <!-- Action Buttons -->
        ${c.url ? `<a href="${c.url}" target="_blank" rel="noopener noreferrer" class="text-xs text-cyan-300 hover:underline">Open app</a>` : ''}
        ${c.state === 'running' && !c.stats_available ? '<span class="text-xs text-amber-300">Metrics unavailable</span>' : ''}
        <div class="flex items-center gap-1.5 xl:flex-col xl:items-end shrink-0">
            <button data-action="inspect" data-value="${c.name}" class="text-xs px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 transition flex items-center gap-1 cursor-pointer">
                <i class="fa-solid fa-circle-info text-indigo-400"></i> Details
            </button>
            <button data-action="logs" data-value="${c.name}" class="text-xs px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 transition flex items-center gap-1 cursor-pointer">
                <i class="fa-solid fa-terminal text-cyan-400"></i> Logs
            </button>
        </div>
    </div>
    `;
}

async function inspectContainer(name) {
    openModal(
        `Container Details: ${name}`,
        'Fetching secure inspection data from Docker...',
        '<div class="text-center py-8 text-slate-400"><i class="fa-solid fa-circle-notch fa-spin text-2xl text-indigo-500 mb-2"></i><p>Loading configuration...</p></div>',
        'fa-circle-info text-indigo-400',
        () => inspectContainer(name)
    );

    const generation = modalGeneration;
    try {
        const res = await fetch(`/api/inspect?name=${encodeURIComponent(name)}`);
        if (!res.ok) throw new Error('Could not fetch inspection data');
        const data = escapedView(await res.json());
        if (generation !== modalGeneration) return;

        const state = data.State || {};
        const mounts = data.Mounts || [];
        const net = (data.NetworkSettings && data.NetworkSettings.Networks) ? data.NetworkSettings.Networks : {};

        let mountsHtml = mounts.map(m => {
            const isSecondary = (m.Source || '').includes('/srv/data') || (m.Source || '').includes('/data') || (m.Source || '').includes('/mnt');
            const diskBadge = isSecondary
                ? '<span class="text-[10px] px-2 py-0.5 rounded bg-emerald-950/80 text-emerald-300 font-mono border border-emerald-800/40 font-semibold flex items-center gap-1"><i class="fa-solid fa-hard-drive text-[9px]"></i>Secondary Storage</span>'
                : '<span class="text-[10px] px-2 py-0.5 rounded bg-cyan-950/80 text-cyan-300 font-mono border border-cyan-800/40 font-semibold flex items-center gap-1"><i class="fa-solid fa-bolt text-[9px]"></i>System Storage</span>';
            return `
            <div class="bg-slate-950 p-2.5 rounded-xl border border-slate-800">
                <div class="flex items-center justify-between mb-1.5">
                    <div class="flex items-center gap-2">
                        <span class="text-indigo-400 font-bold uppercase text-[10px] tracking-wider bg-indigo-950/60 px-1.5 py-0.5 rounded border border-indigo-800/40">${m.Type || 'mount'}</span>
                        <span class="text-xs text-slate-500 font-mono">Mode: ${m.RW ? 'Read-Write (RW)' : 'Read-Only (RO)'}</span>
                    </div>
                    ${diskBadge}
                </div>
                <div class="text-slate-300 font-mono text-xs break-all">${m.Source}</div>
                <div class="text-cyan-400 font-mono text-xs mt-0.5 break-all">&rarr; ${m.Destination}</div>
            </div>
        `}).join('') || '<div class="text-slate-500">No mounts found</div>';

        let netHtml = Object.keys(net).map(k => `
            <div class="bg-slate-950 p-2.5 rounded-xl border border-slate-800 flex flex-col md:flex-row md:items-center justify-between gap-1">
                <span class="text-slate-200 font-bold">${escapeHtml(k)}</span>
                <div class="flex items-center gap-3 font-mono text-xs">
                    <span class="text-cyan-400">IP: ${net[k].IPAddress || 'No IP'}</span>
                    <span class="text-slate-500">Gateway: ${net[k].Gateway || 'None'}</span>
                </div>
            </div>
        `).join('') || '<div class="text-slate-500">No networks attached</div>';

        document.getElementById('modalContent').innerHTML = `
            <div class="space-y-4">
            <p class="text-xs text-slate-300">Restarts: ${data.RestartCount || 0} · Exit code: ${state.ExitCode ?? '—'} · OOM killed: ${state.OOMKilled ? 'Yes' : 'No'} · Health: ${data.Health?.Status || 'No healthcheck'}</p>
                <div class="grid grid-cols-2 sm:grid-cols-4 gap-2.5 text-xs">
                    <div class="bg-slate-950 p-2.5 rounded-xl border border-slate-800">
                        <div class="text-slate-400 mb-0.5">Status</div>
                        <div class="font-bold text-white capitalize">${state.Status || '-'}</div>
                    </div>
                    <div class="bg-slate-950 p-2.5 rounded-xl border border-slate-800">
                        <div class="text-slate-400 mb-0.5">Restarts</div>
                        <div class="font-bold text-white font-mono">${data.RestartCount || 0}</div>
                    </div>
                    <div class="bg-slate-950 p-2.5 rounded-xl border border-slate-800">
                        <div class="text-slate-400 mb-0.5">Memory Limit</div>
                        <div class="font-bold text-white font-mono">${data.Resources && data.Resources.Memory ? formatBytesNoDec(data.Resources.Memory) : 'Unlimited'}</div>
                    </div>
                    <div class="bg-slate-950 p-2.5 rounded-xl border border-slate-800">
                        <div class="text-slate-400 mb-0.5">CPU Quota</div>
                        <div class="font-bold text-white font-mono">${data.Resources && data.Resources.NanoCpus ? (data.Resources.NanoCpus / 1e9) + ' CPUs' : 'Unlimited'}</div>
                    </div>
                </div>

                <div>
                    <h4 class="text-slate-300 font-bold mb-2 flex items-center gap-1.5"><i class="fa-solid fa-folder-tree text-indigo-400"></i> Mounts &amp; Volumes</h4>
                    <div class="space-y-2">${mountsHtml}</div>
                </div>

                <div>
                    <h4 class="text-slate-300 font-bold mb-2 flex items-center gap-1.5"><i class="fa-solid fa-network-wired text-cyan-400"></i> Networks</h4>
                    <div class="space-y-2">${netHtml}</div>
                </div>

                <div class="p-3 bg-slate-950/80 rounded-xl border border-slate-800/80 text-[11px] text-slate-400 flex items-center gap-2">
                    <i class="fa-solid fa-shield-halved text-emerald-400"></i>
                    <span>Environment variables (secrets) are stripped server-side to prevent credential or token leakage.</span>
                </div>
            </div>
        `;
    } catch (err) {
        if (generation !== modalGeneration) return;
        document.getElementById('modalContent').innerHTML = `
            <div class="p-6 text-center text-rose-400">
                <i class="fa-solid fa-triangle-exclamation text-2xl mb-2"></i>
                <p>${escapeHtml(err.message)}</p>
            </div>
        `;
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
    if (!logState) return;
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
        if (logState === state && generation === logGeneration && !state.paused) logTimer = setTimeout(refreshLogs, 3000);
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
    const rows = Object.values(data.stacks || {}).flatMap(stack => stack.containers);
    const issues = rows.filter(c => ['unhealthy','exited','dead','restarting'].includes(c.health));
    document.getElementById('issuesSummary').textContent = issues.length ? `${issues.length} containers need attention · ${issues.slice(0, 5).map(c => c.name + ': ' + c.health).join(' · ') + (issues.length > 5 ? ' · …' : '')}` : 'All containers running or healthy';
    document.getElementById('qf-issues').textContent = `Needs attention (${issues.length})`;
}
let historyFetched = 0;
async function refreshHistory(force = false) {
    if (!force && Date.now() - historyFetched < 15000) return;
    historyFetched = Date.now();
    try {
        const res = await fetch('/api/history');
        if (!res.ok) throw new Error('History unavailable');
        const {points} = await res.json();
        drawSparkline('cpuHistory', points, 'host_cpu', '#fbbf24');
        drawSparkline('memoryHistory', points, 'host_memory', '#c084fc');
        document.getElementById('historyStatus').textContent = `${points.length} samples · up to 60 minutes · kept in memory`;
    } catch { document.getElementById('historyStatus').textContent = 'History unavailable'; }
}
function drawSparkline(id, points, metric, color) {
    const svg = document.getElementById(id);
    svg.replaceChildren();
    const valid = points.filter(p => Number.isFinite(p[metric]));
    if (!valid.length) return;
    const end = points[points.length - 1].timestamp;
    const start = Math.min(points[0].timestamp, end - 1);
    let segment = [];
    function flush() {
        if (!segment.length) return;
        const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
        poly.setAttribute('points', segment.join(' '));
        poly.setAttribute('fill','none'); poly.setAttribute('stroke', color); poly.setAttribute('stroke-width','2');
        svg.append(poly); segment = [];
    }
    let previous = null;
    for (const point of points) {
        if (!Number.isFinite(point[metric]) || (previous && point.timestamp - previous.timestamp > 30)) flush();
        if (Number.isFinite(point[metric])) segment.push(`${(point.timestamp-start)/(end-start)*400},${58-Math.max(0,Math.min(100,point[metric]))/100*54}`);
        previous = point;
    }
    flush();
}
async function showDiagnostics() {
    openModal('Diagnostics', 'Docker connectivity and last successful measurements', '<pre id="diagnosticsText" class="text-xs whitespace-pre-wrap">Loading…</pre>', 'fa-stethoscope text-indigo-400', showDiagnostics);
    const target = document.getElementById('diagnosticsText');
    try {
        const res = await fetch('/api/diagnostics');
        if (!res.ok) throw new Error();
        target.textContent = JSON.stringify(await res.json(), null, 2);
    } catch { target.textContent = 'Diagnostics unavailable'; }
}
document.addEventListener('click', event => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    const value = button.dataset.value;
    const actions = {
        close: closeModal, refresh: () => fetchStats(true), auto: toggleAutoRefresh,
        expand: () => toggleExpandAll(value === 'true'), stack: () => toggleStack(value),
        card: () => openTopCardModal(value), filter: () => setQuickFilter(value),
        inspect: () => inspectContainer(value), logs: () => viewLogs(value),
        copy: () => copyText(value, button), 'copy-logs': () => copyText(document.getElementById('logText')?.textContent, button),
        diagnostics: showDiagnostics, 'log-download': downloadLogs,
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
setQuickFilter(['all','cpu','mem','ports','secondary','root','issues'].includes(activeQuickFilter) ? activeQuickFilter : 'all');
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
