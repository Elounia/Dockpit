# Read-only HTTP API

All endpoints require the same trusted deployment as the dashboard. Dockpit has no built-in authentication and does not enable cross-origin requests. Responses use `Cache-Control: no-store`.

| Endpoint | Response |
|---|---|
| `GET /api/stats` | Latest snapshot, `timestamp` (Unix seconds), `stale`, `backend`, `system`, totals and `stacks` |
| `GET /api/history` | `retention_seconds: 3600`, `points` with `timestamp`, `host_cpu`, `host_memory`, `docker_cpu`, `docker_memory` |
| `GET /api/diagnostics` | Version, poll interval, stale flag, socket existence/accessibility, backend, last attempt/success/error and measurement availability |
| `GET /api/inspect?name=<name-or-id>` | Allowlisted runtime inspection fields |
| `GET /api/logs?name=<name-or-id>&tail=100&timestamps=1` | UTF-8 text; at most 500 requested lines and 1 MiB |
| `GET /healthz` | HTTP liveness and version; HTTP 200 |
| `GET /readyz` | `ready`, `stale`; HTTP 503 if no snapshot or stale |

`/api/stats` returns HTTP 503 until its first successful snapshot. A previously collected snapshot still returns HTTP 200 with `stale: true` when it expires. Staleness uses the greater of 30 seconds or three configured poll intervals.

CPU values `total_cpu` / `docker_total_cpu` are the sum of per-core percentages and can exceed 100. `total_cpu_host_perc` / `docker_host_cpu_perc` normalize against host CPU count. `system.host_cpu_pct` independently measures the whole host from procfs. Container memory is Docker usage minus inactive file cache; it should not be interpreted as a precise process RSS measurement.

`system.gpu` contains `devices`, mean `utilization_pct`, summed `memory_used_bytes`
and `memory_total_bytes`. Each device includes `id`, `index`, `name`, `vendor`,
`utilization_pct`, `memory_used_bytes`, `memory_total_bytes`, `temperature_c` and
`power_w`. Unsupported measurements are `null`; no detected hardware yields an
empty device list and `null` totals. A total is `null` if any detected device is
missing that counter.

DRM devices also include `memory_kind` (`shared`, `dedicated`, or `unknown`).
Intel devices include nullable `frequency_mhz` and `max_frequency_mhz`, plus
`utilization_note` and, for identified integrated models, `memory_note`.
Clock readings are independent of GPU load; they never populate `utilization_pct`.
Shared system RAM has no fabricated dedicated VRAM capacity.

Containers include nullable `gpu_perc`, nullable `gpu_memory_bytes`, and
`gpu_stats_available` (at least one GPU counter is measured), independently of
Docker's `stats_available`. NVIDIA process SM percentages are summed per container
and can exceed 100 across multiple GPUs. GPU memory is framebuffer memory in bytes,
separate from Docker RAM. See [GPU monitoring](DOCKER.md#gpu-monitoring) for backend
support, process mapping and permissions.

Each stack contains `name`, `containers`, `total_cpu`, `total_mem_bytes`, `total_mem_formatted`, `total_pids`, `healthy_count`, `total_containers`, `host_mem_perc`, and `docker_mem_share`. Containers include name/IDs, `display_name`, `url`, `icon`, image, state/status/health, CPU/memory, `stats_available`, ports, service and storage tier. Stopped containers remain listed with no available running stats. CLI fallback supplies the same schema but has fewer hardware/container metadata fields; unavailable I/O is labeled accordingly.

Containers also include `stack_source` (`label`, `compose`, `swarm`, `image`, or `standalone`) and `compose_project` (original Compose project, or an empty string). Stack precedence is `dockpit.stack`, Compose project, Swarm namespace, then inferred image repository for unassigned containers sharing a repository. CubeCoders `cubecoders/ampbase` containers use the AMP group, even for a single instance. Inference never merges into an explicit stack with the same name. Browser stack assignments are local display preferences and do not alter API snapshots.

Unavailable host CPU percentages and storage capacities are `null`; unavailable host memory capacity is zero with `system.availability.memory: false` and nullable percentages. Formatted unavailable host values say `Unavailable`. A running container with `stats_available: false` must not be interpreted as an idle measured container.

Inspection allows Name; State status, OOMKilled, timestamps, exit code and PID; Health status/failure count; RestartCount; selected resource limits; mounts; and network settings. It excludes `Config.Env`, command arguments and healthcheck output. Logs and other metadata may still be sensitive.

Invalid or unknown container identifiers and invalid tail counts return HTTP 400. Runtime inspection/log failures return HTTP 502 with generic errors. At most four inspection/log requests run concurrently; excess requests return HTTP 429. Unknown routes return HTTP 404. HEAD returns the same headers and no body. Mutating HTTP methods are unsupported.

History is sampled after successful polls, held in memory, bounded to 7200 points and one hour, and lost on restart. No synthetic zero samples are added when polling fails.

### Host history and system status

Host CPU/RAM use a separate one-second procfs sampler; Docker polling keeps
`DOCKPIT_POLL_INTERVAL`. `/api/history?since=<last cursor>` returns only newer
points, plus `cursor` and `reset`. Fetch without `since` initially. On `reset`,
replace the local buffer. Retention is one hour: 300 recent one-second points
and up to 330 ten-second means, with a fixed capacity of 630. Missing values
remain null; coarse points include per-metric `counts` for weighted merging.
The in-memory history resets when Dockpit restarts.

`/api/diagnostics` includes `measurements` keyed by CPU, RAM, Storage and GPU;
each has `ok`, a short `reason`, and an actionable `hint` when unavailable.
`docker` reports backend connectivity and a fix hint. `poll_timing.last_ms`
and `average_ms` cover the last poll and the latest 60 attempts, including
failures. `process.cpu_pct` is Dockpit CPU as percent of one core between
attempts; `ram_bytes` is resident memory. `hidden_containers`, `total_containers`
(including hidden), `host_last_sample`, and `history.size/capacity` describe
inventory and sampler state. Raw diagnostics remain available in the UI for
copying into bug reports. GPU detection behavior is unchanged.

History responses also include `capacity.cpu_threads` (logical CPUs) and
`capacity.memory_bytes` (host RAM). Hover values express CPU utilization as
equivalent logical CPUs in use and RAM as used/total GiB, alongside percentages.
