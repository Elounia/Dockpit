/* Host history has its own one-second fetch cadence. No work runs in a hidden tab. */
class HostHistoryChart {
    constructor(canvas, tooltip, status) {
        this.canvas = canvas;
        this.tooltip = tooltip;
        this.status = status;
        this.points = [];
        this.cursor = 0;
        this.capacity = {};
        this.range = 300;
        this.pending = false;
        this.frame = 0;
        this.timer = null;
        this.abort = null;
        this.hover = null;
        tooltip.innerHTML = '<time class="history-time"></time><div class="history-tooltip-row"><span class="history-cpu-label">CPU</span><strong class="history-cpu-value"></strong><span class="history-cpu-percent"></span></div><div class="history-tooltip-row"><span>RAM</span><strong class="history-ram-value"></strong><span class="history-ram-percent"></span></div>';
        this.tooltipFields = Object.fromEntries(['time','cpu-value','cpu-percent','ram-value','ram-percent'].map(key => [key, tooltip.querySelector(`.history-${key}`)]));
        canvas.addEventListener('pointermove', event => {
            const bounds = canvas.getBoundingClientRect();
            this.hover = Math.max(8, Math.min(bounds.width - 8, event.clientX - bounds.left));
            this.draw();
        });
        canvas.addEventListener('pointerleave', () => { this.hover = null; tooltip.hidden = true; this.draw(); });
        this.resize = new ResizeObserver(() => { if (!document.hidden) this.draw(); });
        this.resize.observe(canvas);
    }
    setRange(seconds) {
        this.range = seconds;
        document.querySelectorAll('[data-range]').forEach(el => el.setAttribute('aria-pressed', Number(el.dataset.range) === seconds));
        this.updateStatus();
        this.draw();
    }
    updateStatus() {
        const rangeLabels = { 300: '5 minutes', 900: '15 minutes', 3600: '1 hour' };
        const label = rangeLabels[this.range] || `${Math.round(this.range / 60)}m`;
        if (!this.points.length) {
            this.status.textContent = `Collecting samples… (range: ${label})`;
            return;
        }
        const earliest = this.points[0].timestamp;
        const latest = Math.max(Date.now() / 1000, this.points[this.points.length - 1].timestamp);
        const dataSpan = latest - earliest;
        if (dataSpan < this.range) {
            const mins = Math.floor(dataSpan / 60);
            const secs = Math.round(dataSpan % 60);
            const spanText = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
            this.status.textContent = `Last ${spanText} · range ${label}`;
        } else {
            this.status.textContent = `Showing last ${label}`;
        }
    }
    start() {
        this.stop();
        this.fetch();
        this.timer = setInterval(() => this.fetch(), 1000);
    }
    stop() {
        clearInterval(this.timer);
        cancelAnimationFrame(this.frame);
        this.abort?.abort();
        this.tooltip.hidden = true;
        this.hover = null;
    }
    compact() {
        const now = Date.now() / 1000, cutoff = now - 300;
        const buckets = new Map();
        const recent = [];
        for (const point of this.points) {
            if (point.timestamp < now - 3600) continue;
            if (point.timestamp > cutoff) { recent.push(point); continue; }
            const timestamp = Math.floor(point.timestamp / 10) * 10;
            if (!buckets.has(timestamp)) buckets.set(timestamp, {timestamp, sums:[0,0], counts:[0,0]});
            const bucket = buckets.get(timestamp);
            ['host_cpu','host_memory'].forEach((key, i) => {
                if (Number.isFinite(point[key])) {
                    const weight = point.counts?.[i] ?? 1;
                    bucket.sums[i] += point[key] * weight;
                    bucket.counts[i] += weight;
                }
            });
        }
        const coarse = [...buckets.values()].slice(-330).map(b => ({timestamp:b.timestamp, counts:b.counts,
            host_cpu:b.counts[0] ? b.sums[0]/b.counts[0] : null,
            host_memory:b.counts[1] ? b.sums[1]/b.counts[1] : null}));
        this.points = coarse.concat(recent.slice(-300));
    }
    async fetch() {
        if (this.pending || document.hidden) return;
        this.pending = true;
        const abort = this.abort = new AbortController();
        try {
            const response = await fetch(`/api/history?since=${this.cursor}`, {signal:abort.signal});
            if (!response.ok) throw new Error('History unavailable');
            const data = await response.json();
            if (document.hidden || abort.signal.aborted) return;
            if (data.reset) this.points = [];
            this.capacity = data.capacity || {};
            const arrived = data.points.length > 0;
            this.points.push(...data.points);
            this.cursor = data.cursor;
            this.compact();
            this.updateStatus();
            if (arrived) this.animate();
        } catch (error) {
            if (error.name !== 'AbortError') this.status.textContent = 'History unavailable';
        } finally { this.pending = false; }
    }
    animate() {
        cancelAnimationFrame(this.frame);
        if (matchMedia('(prefers-reduced-motion: reduce)').matches) { this.draw(); return; }
        const start = performance.now();
        const tick = time => {
            if (document.hidden) return;
            const progress = Math.min(1, (time - start) / 300);
            this.draw(1 - Math.pow(1 - progress, 3));
            if (progress < 1) this.frame = requestAnimationFrame(tick);
            else this.frame = 0;
        };
        this.frame = requestAnimationFrame(tick);
    }
    // Fritsch–Carlson tangents keep every cubic within its neighboring sample values.
    curve(context, points) {
        if (!points.length) return;
        context.moveTo(points[0].x, points[0].y);
        if (points.length < 2) return;
        const slopes = points.slice(1).map((p,i) => (p.y-points[i].y)/(p.x-points[i].x));
        const tangents = points.map((_,i) => i === 0 ? slopes[0] : i === points.length-1 ? slopes[i-1] : slopes[i-1]*slopes[i] <= 0 ? 0 : 2/(1/slopes[i-1]+1/slopes[i]));
        slopes.forEach((slope,i) => {
            if (!slope) { tangents[i] = tangents[i+1] = 0; return; }
            const a = tangents[i]/slope, b = tangents[i+1]/slope;
            const length = Math.hypot(a,b);
            if (length > 3) { tangents[i] = 3*a/length*slope; tangents[i+1] = 3*b/length*slope; }
        });
        for (let i=0; i<points.length-1; i++) {
            const a=points[i], b=points[i+1], dx=(b.x-a.x)/3;
            context.bezierCurveTo(a.x+dx,a.y+dx*tangents[i],b.x-dx,b.y-dx*tangents[i+1],b.x,b.y);
        }
    }
    showTooltip(point) {
        const percent = value => Number.isFinite(value) ? `${value.toFixed(1)}%` : 'Unavailable';
        const threads = this.capacity.cpu_threads, memory = this.capacity.memory_bytes;
        const cpu = Number.isFinite(point.host_cpu) && threads > 0 ? `${(point.host_cpu / 100 * threads).toFixed(2)} / ${threads} CPUs` : 'Unavailable';
        let ram = 'Unavailable';
        if (Number.isFinite(point.host_memory) && memory > 0) {
            const unit = memory >= 1024 ** 3 ? 1024 ** 3 : 1024 ** 2;
            const label = unit === 1024 ** 3 ? 'GiB' : 'MiB';
            ram = `${(point.host_memory / 100 * memory / unit).toFixed(1)} / ${(memory / unit).toFixed(1)} ${label}`;
        }
        this.tooltipFields.time.textContent = new Date(point.timestamp * 1000).toLocaleTimeString();
        this.tooltipFields['cpu-value'].textContent = cpu;
        this.tooltipFields['cpu-value'].title = 'Equivalent logical CPUs in use; 100% uses all logical CPUs.';
        this.tooltipFields['cpu-percent'].textContent = percent(point.host_cpu);
        this.tooltipFields['ram-value'].textContent = ram;
        this.tooltipFields['ram-percent'].textContent = percent(point.host_memory);
    }
    draw(progress=1) {
        if (document.hidden) return;
        const canvas = this.canvas, width = canvas.clientWidth, height = canvas.clientHeight;
        const ratio = devicePixelRatio || 1;
        if (canvas.width !== Math.round(width*ratio) || canvas.height !== Math.round(height*ratio)) {
            canvas.width = Math.round(width*ratio); canvas.height = Math.round(height*ratio);
        }
        const context = canvas.getContext('2d');
        context.setTransform(ratio,0,0,ratio,0,0);
        context.clearRect(0,0,width,height);

        const left = 8, right = width - 8;
        const now = Date.now() / 1000;
        let start, end, windowDuration;

        if (!this.points.length) {
            windowDuration = Math.max(60, this.range);
            start = now - windowDuration;
            end = now;
        } else {
            const earliest = this.points[0].timestamp;
            const latest = Math.max(now, this.points[this.points.length - 1].timestamp);
            const dataSpan = latest - earliest;
            windowDuration = Math.min(this.range, Math.max(60, dataSpan));
            if (dataSpan >= this.range) {
                // Once history reaches the selected range, scroll smoothly
                start = latest - this.range;
                end = latest;
                windowDuration = this.range;
            } else {
                // Growing window: line always starts at left edge
                start = earliest;
                end = earliest + windowDuration;
            }
        }

        const visible = this.points.filter(p => p.timestamp >= start && p.timestamp <= end);

        // Display-only smoothing for CPU (3-sample moving average)
        const smoothedCpu = visible.map((p, i) => {
            if (!Number.isFinite(p.host_cpu)) return null;
            let sum = p.host_cpu;
            let count = 1;
            if (i > 0 && Number.isFinite(visible[i - 1].host_cpu) && Math.abs(p.timestamp - visible[i - 1].timestamp) <= 3) {
                sum += visible[i - 1].host_cpu;
                count++;
            }
            if (i < visible.length - 1 && Number.isFinite(visible[i + 1].host_cpu) && Math.abs(visible[i + 1].timestamp - p.timestamp) <= 3) {
                sum += visible[i + 1].host_cpu;
                count++;
            }
            return sum / count;
        });

        // CPU Auto-scaling ceiling: 10 / 25 / 50 / 100
        let maxCpu = 0;
        for (const val of smoothedCpu) {
            if (val != null && val > maxCpu) maxCpu = val;
        }
        let cpuCeiling = 10;
        if (maxCpu > 50) cpuCeiling = 100;
        else if (maxCpu > 25) cpuCeiling = 50;
        else if (maxCpu > 10) cpuCeiling = 25;
        else cpuCeiling = 10;

        // Proportions: CPU row is taller (~70%), RAM row is a slim strip
        const cpuTop = 10;
        const cpuHeight = Math.max(50, Math.round((height - 34) * 0.70));
        const cpuBottom = cpuTop + cpuHeight;

        const ramTop = cpuBottom + 10;
        const ramBottom = height - 10;
        const ramHeight = ramBottom - ramTop;

        // CPU grid lines & ceiling label
        context.strokeStyle = 'rgba(255,255,255,.05)';
        context.lineWidth = 1;
        const cpuSteps = cpuCeiling === 100 ? [25, 50, 75] : cpuCeiling === 50 ? [25] : cpuCeiling === 25 ? [10] : [5];
        for (const step of cpuSteps) {
            const y = cpuBottom - (step / cpuCeiling) * (cpuBottom - cpuTop);
            context.beginPath(); context.moveTo(left, y); context.lineTo(right, y); context.stroke();
        }
        context.beginPath(); context.moveTo(left, cpuTop); context.lineTo(right, cpuTop); context.stroke();
        context.beginPath(); context.moveTo(left, cpuBottom); context.lineTo(right, cpuBottom); context.stroke();

        context.fillStyle = 'rgba(255,255,255,.35)';
        context.font = '10px ui-monospace, SFMono-Regular, monospace';
        context.fillText(`${cpuCeiling}%`, left + 4, cpuTop + 10);
        context.fillText('CPU', left + 4, cpuBottom - 5);

        // Render CPU Curve
        let cpuSegment = [];
        const flushCpu = () => {
            if (!cpuSegment.length) return;
            context.beginPath();
            this.curve(context, cpuSegment);
            context.strokeStyle = '#818cf8';
            context.lineWidth = 1.75;
            context.lineJoin = 'round';
            context.lineCap = 'round';
            context.stroke();

            if (cpuSegment.length > 1) {
                context.lineTo(cpuSegment[cpuSegment.length - 1].x, cpuBottom);
                context.lineTo(cpuSegment[0].x, cpuBottom);
                context.closePath();
                const grad = context.createLinearGradient(0, cpuTop, 0, cpuBottom);
                grad.addColorStop(0, 'rgba(129,140,248,.20)');
                grad.addColorStop(1, 'rgba(129,140,248,0)');
                context.fillStyle = grad;
                context.fill();
            }
            cpuSegment = [];
        };

        let previousCpu = null;
        visible.forEach((p, i) => {
            const val = smoothedCpu[i];
            const gap = previousCpu && p.timestamp - previousCpu.timestamp > (p.timestamp <= end - 300 ? 25 : 3);
            if (gap || val == null) flushCpu();
            if (val != null) {
                let displayVal = val;
                if (i === visible.length - 1 && previousCpu && previousCpu.val != null && !gap) {
                    displayVal = previousCpu.val + (displayVal - previousCpu.val) * progress;
                }
                const x = left + ((p.timestamp - start) / windowDuration) * (right - left);
                const y = cpuBottom - (Math.max(0, Math.min(cpuCeiling, displayVal)) / cpuCeiling) * (cpuBottom - cpuTop);
                cpuSegment.push({ x, y });
            }
            previousCpu = { timestamp: p.timestamp, val };
        });
        flushCpu();

        // RAM row grid & labels
        context.strokeStyle = 'rgba(255,255,255,.05)';
        context.beginPath(); context.moveTo(left, ramTop); context.lineTo(right, ramTop); context.stroke();
        context.beginPath(); context.moveTo(left, ramBottom); context.lineTo(right, ramBottom); context.stroke();

        const latestRamPoint = [...visible].reverse().find(p => Number.isFinite(p.host_memory));
        const currentRam = latestRamPoint ? latestRamPoint.host_memory : null;
        context.fillStyle = 'rgba(255,255,255,.35)';
        context.font = '10px ui-monospace, SFMono-Regular, monospace';
        context.fillText(currentRam != null ? `RAM ${currentRam.toFixed(1)}%` : 'RAM 0–100%', left + 4, ramTop + 10);

        // Render RAM Curve
        let ramSegment = [];
        const flushRam = () => {
            if (!ramSegment.length) return;
            context.beginPath();
            this.curve(context, ramSegment);
            context.strokeStyle = '#858791';
            context.lineWidth = 1.25;
            context.lineJoin = 'round';
            context.lineCap = 'round';
            context.stroke();

            if (ramSegment.length > 1) {
                context.lineTo(ramSegment[ramSegment.length - 1].x, ramBottom);
                context.lineTo(ramSegment[0].x, ramBottom);
                context.closePath();
                const grad = context.createLinearGradient(0, ramTop, 0, ramBottom);
                grad.addColorStop(0, 'rgba(133,135,145,.18)');
                grad.addColorStop(1, 'rgba(133,135,145,0)');
                context.fillStyle = grad;
                context.fill();
            }
            ramSegment = [];
        };

        let previousRam = null;
        visible.forEach((p, i) => {
            const val = p.host_memory;
            const gap = previousRam && p.timestamp - previousRam.timestamp > (p.timestamp <= end - 300 ? 25 : 3);
            if (gap || !Number.isFinite(val)) flushRam();
            if (Number.isFinite(val)) {
                let displayVal = val;
                if (i === visible.length - 1 && previousRam && Number.isFinite(previousRam.val) && !gap) {
                    displayVal = previousRam.val + (displayVal - previousRam.val) * progress;
                }
                const x = left + ((p.timestamp - start) / windowDuration) * (right - left);
                const y = ramBottom - (Math.max(0, Math.min(100, displayVal)) / 100) * (ramBottom - ramTop);
                ramSegment.push({ x, y });
            }
            previousRam = { timestamp: p.timestamp, val };
        });
        flushRam();

        // Shared hover crosshair across both rows
        if (this.hover !== null && visible.length) {
            const timestamp = start + ((this.hover - left) / (right - left)) * windowDuration;
            const point = visible.reduce((best, p) => Math.abs(p.timestamp - timestamp) < Math.abs(best.timestamp - timestamp) ? p : best);
            const x = left + ((point.timestamp - start) / windowDuration) * (right - left);

            context.strokeStyle = 'rgba(255,255,255,.25)';
            context.lineWidth = 1;
            context.beginPath();
            context.moveTo(x, cpuTop);
            context.lineTo(x, ramBottom);
            context.stroke();

            if (Number.isFinite(point.host_cpu)) {
                const cpuY = cpuBottom - (Math.max(0, Math.min(cpuCeiling, point.host_cpu)) / cpuCeiling) * (cpuBottom - cpuTop);
                context.fillStyle = '#818cf8';
                context.beginPath();
                context.arc(x, cpuY, 3, 0, Math.PI * 2);
                context.fill();
            }
            if (Number.isFinite(point.host_memory)) {
                const ramY = ramBottom - (Math.max(0, Math.min(100, point.host_memory)) / 100) * (ramBottom - ramTop);
                context.fillStyle = '#858791';
                context.beginPath();
                context.arc(x, ramY, 2.5, 0, Math.PI * 2);
                context.fill();
            }

            this.showTooltip(point);
            this.tooltip.hidden = false;
            this.tooltip.style.left = `${Math.max(0, Math.min(width - this.tooltip.offsetWidth, x + 12))}px`;
        } else {
            this.tooltip.hidden = true;
        }
    }
}
