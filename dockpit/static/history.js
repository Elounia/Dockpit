/* Host history has its own one-second fetch cadence. No work runs in a hidden tab. */
class HostHistoryChart {
    constructor(canvas, tooltip, status) {
        this.canvas = canvas;
        this.tooltip = tooltip;
        this.status = status;
        this.points = [];
        this.cursor = 0;
        this.range = 300;
        this.pending = false;
        this.frame = 0;
        this.timer = null;
        this.abort = null;
        this.hover = null;
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
        this.draw();
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
            const arrived = data.points.length > 0;
            this.points.push(...data.points);
            this.cursor = data.cursor;
            this.compact();
            this.status.textContent = `${this.points.length} samples · 1s / 10s resolution`;
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
        const left=8, right=width-8, top=8, bottom=height-8;
        const end=Date.now()/1000, start=end-this.range;
        context.strokeStyle='rgba(255,255,255,.06)'; context.lineWidth=1;
        for (const pct of [25,50,75]) { const y=bottom-pct/100*(bottom-top); context.beginPath(); context.moveTo(left,y); context.lineTo(right,y); context.stroke(); }
        const visible = this.points.filter(p => p.timestamp >= start && p.timestamp <= end);
        ['host_cpu','host_memory'].forEach((key,index) => {
            let segment=[];
            const flush=() => {
                if (!segment.length) return;
                context.beginPath(); this.curve(context,segment);
                context.strokeStyle=index ? '#858791' : '#818cf8'; context.lineWidth=index ? 1.25 : 2;
                context.lineJoin='round'; context.lineCap='round'; context.stroke();
                if (!index && segment.length > 1) {
                    context.lineTo(segment[segment.length-1].x,bottom); context.lineTo(segment[0].x,bottom); context.closePath();
                    const gradient=context.createLinearGradient(0,top,0,bottom); gradient.addColorStop(0,'rgba(129,140,248,.22)'); gradient.addColorStop(1,'rgba(129,140,248,0)');
                    context.fillStyle=gradient; context.fill();
                }
                segment=[];
            };
            let previous=null;
            visible.forEach((p,i) => {
                const gap=previous && p.timestamp-previous.timestamp > (p.timestamp <= end-300 ? 25 : 3);
                if (gap || !Number.isFinite(p[key])) flush();
                if (Number.isFinite(p[key])) {
                    let value=p[key];
                    if (i===visible.length-1 && previous && Number.isFinite(previous[key]) && !gap) value=previous[key]+(value-previous[key])*progress;
                    segment.push({x:left+(p.timestamp-start)/this.range*(right-left),y:bottom-Math.max(0,Math.min(100,value))/100*(bottom-top)});
                }
                previous=p;
            });
            flush();
        });
        if (this.hover !== null && visible.length) {
            const timestamp=start+(this.hover-left)/(right-left)*this.range;
            const point=visible.reduce((best,p) => Math.abs(p.timestamp-timestamp)<Math.abs(best.timestamp-timestamp) ? p : best);
            const x=left+(point.timestamp-start)/this.range*(right-left);
            context.strokeStyle='rgba(255,255,255,.25)'; context.beginPath(); context.moveTo(x,top); context.lineTo(x,bottom); context.stroke();
            const percent=value => Number.isFinite(value) ? `${value.toFixed(1)}%` : 'Unavailable';
            this.tooltip.textContent=`${new Date(point.timestamp*1000).toLocaleTimeString()} · CPU ${percent(point.host_cpu)} · RAM ${percent(point.host_memory)}`;
            this.tooltip.hidden=false;
            this.tooltip.style.left=`${Math.max(0,Math.min(width-this.tooltip.offsetWidth,x+12))}px`;
        } else this.tooltip.hidden=true;
    }
}
