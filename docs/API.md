# Dockpit REST API Reference

Dockpit exposes a lightweight, read-only REST API for querying host and container metrics, safe inspection, and logs.

All API responses are served with:
```http
Access-Control-Allow-Origin: *
Cache-Control: no-cache, no-store, must-revalidate
```

---

## Endpoints

### 1. `GET /api/stats`
Returns the current snapshot of all Docker stacks, active and stopped containers, host system metrics (CPU, RAM, Disks, Uptime), and aggregated utilization.

#### Response: `200 OK`
```json
{
  "timestamp": 1793451230.32,
  "stale": false,
  "system": {
    "cpus": 12,
    "host_cpu_pct": 11.5,
    "load": [1.28, 4.26, 5.72],
    "load_str": "1.28, 4.26, 5.72",
    "uptime_str": "7d 16h 28m",
    "mem_used_bytes": 33682071552,
    "mem_total_bytes": 48826085376,
    "mem_used_formatted": "31.4 GiB",
    "mem_total_formatted": "45.5 GiB",
    "mem_pct": 69.0,
    "root_disk": {
      "name": "System Disk",
      "mount": "/",
      "type": "Root Storage",
      "total_bytes": 251000000000,
      "used_bytes": 180000000000,
      "free_bytes": 71000000000,
      "total_formatted": "233.8 GiB",
      "used_formatted": "167.6 GiB",
      "free_formatted": "66.1 GiB",
      "pct": 71.7
    },
    "data_disk": null,
    "details": {
      "cpu": {
        "model_name": "12th Gen Intel(R) Core(TM) i5-12400",
        "vendor_id": "GenuineIntel",
        "cache_size": "18432 KB",
        "cpu_mhz": "3988.857",
        "cores_per_socket": 6,
        "threads_total": 12,
        "flags": ["fpu", "vme", "de", "pse", "tsc", "msr", "pae", "cx8", "avx2", "aes"]
      },
      "memory": {
        "total_bytes": 48826085376,
        "total_formatted": "45.5 GiB",
        "available_bytes": 14565371904,
        "available_formatted": "13.6 GiB",
        "used_bytes": 34260713472,
        "used_formatted": "31.9 GiB",
        "buffers_bytes": 110059520,
        "cached_bytes": 6423494656,
        "active_bytes": 11918086144,
        "inactive_bytes": 23350804480,
        "shmem_bytes": 2867412992,
        "sreclaimable_bytes": 5237862400,
        "swap_total_bytes": 10737414144,
        "swap_used_bytes": 10733236224,
        "swap_pct": 100.0
      },
      "storage": {
        "devices": [
          {
            "device": "/dev/nvme0n1",
            "name": "nvme0n1",
            "model": "SAMSUNG MZVLB256HAHQ-000L7",
            "serial": "S41GNX0KC37943",
            "id": "nvme-SAMSUNG_MZVLB256HAHQ-000L7_S41GNX0KC37943",
            "type": "NVMe SSD",
            "size_bytes": 256060514304,
            "size_formatted": "238.5 GiB",
            "partitions": [
              {
                "name": "nvme0n1p5",
                "device": "/dev/nvme0n1p5",
                "mount": "/",
                "fstype": "ext4",
                "usage": {
                  "total_bytes": 251233738752,
                  "used_bytes": 187199680512,
                  "free_bytes": 51224817664,
                  "pct": 74.5
                },
                "id": "nvme-eui.0025388c81bad0c5-part5"
              }
            ]
          }
        ],
        "data_root": "/var/lib/docker"
      },
      "docker": {
        "docker_version": "29.9.0",
        "api_version": "1.56",
        "kernel_version": "7.0.0-38-generic",
        "os": "Ubuntu 26.04.1 LTS",
        "arch": "amd64",
        "driver": "overlay2",
        "cgroup_version": "2",
        "containerd_version": "v2.4.1",
        "runc_version": "1.5.2"
      }
    }
  },
  "docker_total_cpu": 45.2,
  "docker_host_cpu_perc": 3.8,
  "docker_total_mem_bytes": 9823485760,
  "docker_total_mem_str": "9.1 GiB",
  "docker_total_mem_perc": 20.1,
  "total_containers": 38,
  "running_containers": 36,
  "unhealthy_containers": 0,
  "total_stacks": 8,
  "total_pids": 342,
  "stacks": {
    "web": {
      "name": "web",
      "total_cpu": 12.4,
      "total_mem_bytes": 1073741824,
      "total_mem_formatted": "1.0 GiB",
      "host_mem_perc": 2.2,
      "docker_mem_share": 10.9,
      "total_containers": 2,
      "healthy_count": 2,
      "containers": [
        {
          "id": "e47c1da46c84",
          "full_id": "e47c1da46c846245de944c735f47ee3f2a10ddfc",
          "name": "web_nginx",
          "stack": "web",
          "image": "nginx:alpine",
          "state": "running",
          "status": "Up 2 hours (healthy)",
          "health": "healthy",
          "ports": "0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp",
          "created": "Up 2 hours",
          "cpu_perc": 2.1,
          "mem_used_bytes": 26214400,
          "mem_limit_bytes": 536870912,
          "mem_used_formatted": "25.0 MiB",
          "mem_limit_formatted": "512.0 MiB",
          "mem_perc": 4.88,
          "has_custom_limit": true,
          "net_rx": "12.4 MiB",
          "net_tx": "48.2 MiB",
          "block_read": "4.0 KiB",
          "block_write": "18.2 MiB",
          "pids": 4,
          "storage_tier": "Root Storage",
          "service": "nginx"
        }
      ]
    }
  }
}
```

---

### 2. `GET /api/inspect?name=<container_name_or_id>`
Returns safe runtime configuration and resource limits for a specific container.

> [!IMPORTANT]
> To prevent credential leaks, environment variables (`Env`) containing database passwords, API tokens, and secret keys are **permanently removed server-side**.

#### Query Parameters:
- `name` *(required, string)*: Valid container name or hex ID matching regex `^[a-zA-Z0-9_\.\-]+$`.

#### Response: `200 OK`
```json
{
  "Name": "/web_nginx",
  "State": {
    "Status": "running",
    "Health": {
      "Status": "healthy",
      "FailingStreak": 0
    },
    "OOMKilled": false,
    "StartedAt": "2026-10-31T11:00:00.000Z",
    "FinishedAt": "0001-01-01T00:00:00Z",
    "ExitCode": 0,
    "Pid": 12345
  },
  "RestartCount": 0,
  "Resources": {
    "Memory": 536870912,
    "NanoCpus": 2000000000,
    "CpuQuota": 0,
    "CpuPeriod": 0,
    "PidsLimit": 100,
    "ShmSize": 67108864,
    "RestartPolicy": {
      "Name": "unless-stopped"
    }
  },
  "Mounts": [
    {
      "Type": "bind",
      "Source": "/etc/nginx/nginx.conf",
      "Destination": "/etc/nginx/nginx.conf",
      "Mode": "ro",
      "RW": false
    }
  ],
  "NetworkSettings": {
    "Networks": {
      "web_network": {
        "IPAddress": "172.20.0.4",
        "Gateway": "172.20.0.1"
      }
    }
  }
}
```

#### Errors:
- `400 Bad Request`: `{"error": "Unknown or missing container name"}`
- `404 Not Found`: `{"error": "Container not found"}`

---

### 3. `GET /api/logs?name=<container_name_or_id>&tail=<lines>`
Fetches recent stdout/stderr log output for a container.

#### Query Parameters:
- `name` *(required, string)*: Container name or ID.
- `tail` *(optional, integer)*: Number of lines to return. Defaults to `100`, bounded between `1` and `500`.

#### Response: `200 OK`
```http
Content-Type: text/plain; charset=utf-8
```
```text
2026-10-31 11:00:01 [info] 1#1: using the "epoll" event method
2026-10-31 11:00:01 [info] 1#1: start worker processes
2026-10-31 11:00:01 [info] 1#1: start worker process 28
```

#### Errors:
- `400 Bad Request`: Invalid line count or untrusted container name.
- `502 Bad Gateway`: Docker daemon returned non-zero exit code while streaming logs.

