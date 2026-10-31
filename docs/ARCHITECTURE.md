# Dockpit Architecture & Technical Design

Dockpit is designed from first principles to be an **ultra-lightweight, zero-dependency, secure real-time cockpit** for Docker hosts and container stacks.

```mermaid
flowchart TD
    subgraph Host["Linux Host"]
        DockerSock["/var/run/docker.sock"]
        ProcFs["/proc (stat, meminfo, uptime)"]
        RootFs["/ (System Disk) & /data (Secondary)"]
    end

    subgraph DockpitCore["Dockpit Core (Python 3 Stdlib)"]
        SocketPoller["Unix Socket Worker / Engine Client"]
        SysMetrics["Host Metrics Engine"]
        CacheState[("Thread-Safe Metrics Cache")]
        Sanitizer["Security Filter & Secret Stripper"]
        HttpServer["Threading HTTP Server (:8989)"]
    end

    subgraph BrowserClient["Web Browser UI"]
        Dashboard["Glassmorphism UI (Tailwind CSS)"]
        SearchEngine["Instant Fuzzy Search & Sorters"]
        InspectModal["Safe Inspection View"]
        LogViewer["Live Terminal Log Stream"]
    end

    DockerSock -->|Unix Domain Socket| SocketPoller
    ProcFs -->|sysfs / procfs| SysMetrics
    RootFs -->|shutil.disk_usage| SysMetrics

    SocketPoller --> CacheState
    SysMetrics --> CacheState

    CacheState --> HttpServer
    DockerSock --> Sanitizer --> HttpServer

    HttpServer -->|/api/stats (JSON)| Dashboard
    HttpServer -->|/api/inspect (Sanitized)| InspectModal
    HttpServer -->|/api/logs (Bounded)| LogViewer
```

---

## Key Architectural Principles

### 1. Zero External Dependencies
Dockpit uses **Python 3 standard library exclusively**:
- `http.server.ThreadingHTTPServer` for asynchronous request serving.
- `http.client` + `socket.AF_UNIX` for high-throughput Unix domain socket streaming directly to Docker Engine.
- `concurrent.futures.ThreadPoolExecutor` for parallelizing container stats retrieval.
- `json`, `re`, `shutil`, `threading`, `time`.

No heavy frameworks (no Flask, Django, FastAPI), no third-party package vulnerabilities, and no multi-megabyte `node_modules` or Python virtual environment overhead.

### 2. Normalized CPU Measurement
Standard `docker stats` reports CPU utilization as a percentage of a single core. On a 12-core system, a multi-threaded workload can display as `600%` or `1200%`, confusing operators and obscuring actual host headroom.

Dockpit calculates:
1. **Container CPU:** Measured via delta ticks between sample intervals divided by system delta and multiplied by online CPU count.
2. **Host Normalized CPU:** Aggregated and scaled from 0% to 100% of the entire host machine capacity.
3. **Host Hardware CPU:** Independently sampled directly from `/proc/stat` to give an accurate, unadulterated ground truth of overall system load.

### 3. True Resident Memory (RSS) vs Buffer Cache
Many monitoring tools report raw Docker `memory_stats.usage`, which mistakenly includes Linux page cache (inactive file buffers) held by the kernel for filesystem operations. This causes idle databases or file-heavy services to look like they are running out of RAM.

Dockpit extracts:
$$\text{Used Memory} = \max(0, \text{usage} - \text{inactive\_file})$$

This reflects the real resident memory footprint of each container while comparing it against either container cgroup limits or total host physical RAM.

### 4. Stack Grouping & Storage Tiering
- **Compose Stacks:** Containers are automatically grouped into visual stacks according to the Docker Compose label `com.docker.compose.project`. Non-compose containers fall into a clean `Standalone` group.
- **Storage Tier Detection:** Mount sources are analyzed to differentiate between fast NVMe/SSD system drives and high-capacity secondary storage pools (e.g. HDD arrays or data mounts).

---

## Security Model & Hardening

Dockpit is engineered to operate safely in internal networks or behind SSO reverse proxies (Authelia, Authentik, Cloudflare Access):

1. **Server-Side Secret Stripping:**
   When a user clicks **Details** to inspect a container, Dockpit intercepts the Docker Engine inspection object and strips all `Env` keys (passwords, tokens, API secrets, database credentials) server-side before transmitting the JSON payload over the wire.
2. **Command Injection Prevention:**
   Container identifiers and names are validated with strict regex (`^[a-zA-Z0-9_\.\-]+$`) and cross-checked against active container IDs in memory before invoking Docker logs or inspection. Flags like `--help` or shell operators are rejected with HTTP 400.
3. **Bounded Payloads & Rate Limiting:**
   Log queries are capped at a maximum of 500 lines and response bodies are bounded to 1 MiB to prevent memory exhaustion and DoS attacks.
4. **Least-Privilege Container:**
   The official container runs as an unprivileged user (`dockpit:dockpit`, UID 1000) with `read_only` rootfs, `no-new-privileges:true`, and all Linux capabilities dropped (`cap_drop: ALL`).

