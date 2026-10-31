#!/usr/bin/env python3
"""
Dockpit - Modern Docker Stack Cockpit & System Monitor
A fast, beautiful, real-time dashboard for Docker containers grouped by stack,
providing host system KPIs (CPU, RAM, Disks, Uptime), normalized 0-100% CPU,
deep safe container inspection, hardware inventory details, and hardened security.

Author: Carl-Emil Berg
License: MIT
"""

import sys
import os
import json
import time
import re
import socket
import http.client
import shutil
import subprocess
import threading
import selectors
from pathlib import Path
from collections import deque
from contextlib import closing
from . import __version__
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Configuration via environment variables
PORT = int(os.environ.get("DOCKPIT_PORT", os.environ.get("DOCKER_MONITOR_PORT", 8989)))
HOST = os.environ.get("DOCKPIT_HOST", "0.0.0.0" if os.path.exists("/.dockerenv") else "127.0.0.1")
DOCKER_SOCKET = os.environ.get("DOCKPIT_DOCKER_SOCKET", "/var/run/docker.sock")
POLL_INTERVAL = float(os.environ.get("DOCKPIT_POLL_INTERVAL", 2.0))
if not 0.5 <= POLL_INTERVAL <= 3600:
    raise ValueError("DOCKPIT_POLL_INTERVAL must be between 0.5 and 3600 seconds")
MAX_LOG_BYTES = 1024 * 1024
STALE_AFTER = max(30, POLL_INTERVAL * 3)
history = deque(maxlen=7200)
api_slots = threading.BoundedSemaphore(4)
poll_status = {"last_attempt": None, "last_success": None, "last_error": None, "backend": None}
stop_event = threading.Event()

PROC_DIR = os.environ.get(
    "DOCKPIT_PROC_DIR",
    "/host/proc" if os.path.exists("/host/proc/stat") else "/proc"
)
ROOT_DIR = os.environ.get(
    "DOCKPIT_ROOT_DIR",
    "/host/root" if os.path.exists("/host/root/etc") else "/"
)

SECONDARY_STORAGE_PATH = os.environ.get("DOCKPIT_SECONDARY_STORAGE_PATH")
if not SECONDARY_STORAGE_PATH:
    # Check default secondary mount points if available
    for candidate in ("/srv/data", "/mnt/data", "/data"):
        check_path = os.path.join(ROOT_DIR, candidate.lstrip("/"))
        if os.path.exists(check_path):
            SECONDARY_STORAGE_PATH = check_path
            break
SECONDARY_STORAGE_LABEL = os.environ.get("DOCKPIT_SECONDARY_STORAGE_LABEL", "Data Storage")


# Helper for Docker Unix Socket communication
class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path=DOCKER_SOCKET, timeout=5):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


cached_data = {
    "timestamp": 0,
    "system": {},
    "docker_total_cpu": 0.0,
    "docker_total_mem_bytes": 0,
    "docker_total_mem_str": "0 B",
    "docker_total_mem_perc": 0.0,
    "total_containers": 0,
    "total_stacks": 0,
    "total_pids": 0,
    "stacks": {}
}
cache_lock = threading.Lock()

# Persistent CPU cache to measure container deltas accurately across polls
_container_cpu_cache = {}
_cpu_cache_lock = threading.Lock()

# CPU measurement state for host
prev_cpu_idle = 0
prev_cpu_total = 0
_previous_cpu = None

# Hardware cache to avoid heavy repetitive disk/proc scanning on rapid polls
_hw_cache = {
    "cpu": None,
    "disks": None,
    "docker": None,
    "last_disk_scan": 0,
    "last_docker_scan": 0,
}


def get_host_resources():
    """Compatibility view of the same measured host metrics used by both backends."""
    system = get_system_metrics()
    return {
        "cpu_count": system["cpus"], "cpu_perc": system["host_cpu_pct"],
        "mem_total_bytes": system["mem_total_bytes"],
        "mem_available_bytes": system["details"]["memory"]["available_bytes"],
        "disk_total_bytes": system["root_disk"]["total_bytes"],
        "disk_free_bytes": system["root_disk"]["free_bytes"],
        "disk_used_perc": system["root_pct"],
    }


def get_host_memory_bytes():
    return get_mem_hardware_details()["total_bytes"]


def container_health(state, status):
    if state != 'running':
        return state or 'unknown'
    if 'unhealthy' in status:
        return 'unhealthy'
    if '(healthy)' in status:
        return 'healthy'
    if 'starting' in status:
        return 'starting'
    return 'running'


def parse_bytes(s):
    if not s:
        return 0.0
    s = s.strip()
    match = re.match(r'^([\d\.]+)\s*([a-zA-Z]+)$', s)
    if not match:
        return 0.0
    val, unit = float(match.group(1)), match.group(2).lower()
    units = {
        'b': 1, 'kb': 1e3, 'kib': 1024,
        'mb': 1e6, 'mib': 1024**2,
        'gb': 1e9, 'gib': 1024**3,
        'tb': 1e12, 'tib': 1024**4
    }
    return val * units.get(unit, 1)


def parse_labels(label_str):
    res = {}
    if not label_str:
        return res
    for part in label_str.split(','):
        if '=' in part:
            k, v = part.split('=', 1)
            res[k.strip()] = v.strip()
    return res


def format_bytes(num_bytes):
    if num_bytes is None:
        return "Unavailable"
    if num_bytes <= 0:
        return "0 B"
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    i = 0
    val = float(num_bytes)
    while val >= 1024.0 and i < len(units) - 1:
        val /= 1024.0
        i += 1
    return f"{val:.1f} {units[i]}"


def get_cpu_hardware_details():
    if _hw_cache["cpu"]:
        return _hw_cache["cpu"]
    details = {
        "model_name": "Unknown Processor",
        "vendor_id": "Unknown",
        "cache_size": "",
        "cpu_mhz": "",
        "cores_per_socket": 0,
        "threads_total": os.cpu_count() or 1,
        "flags": []
    }
    cpuinfo_path = os.path.join(PROC_DIR, "cpuinfo")
    if os.path.exists(cpuinfo_path):
        try:
            with open(cpuinfo_path) as f:
                for line in f:
                    if ":" in line:
                        k, v = [x.strip() for x in line.split(":", 1)]
                        if k == "model name" and details["model_name"] == "Unknown Processor":
                            details["model_name"] = v
                        elif k == "vendor_id" and details["vendor_id"] == "Unknown":
                            details["vendor_id"] = v
                        elif k == "cpu MHz" and not details["cpu_mhz"]:
                            details["cpu_mhz"] = v
                        elif k == "cache size" and not details["cache_size"]:
                            details["cache_size"] = v
                        elif k == "cpu cores" and not details["cores_per_socket"]:
                            try:
                                details["cores_per_socket"] = int(v)
                            except ValueError:
                                pass
                        elif k == "flags" and not details["flags"]:
                            details["flags"] = v.split()[:24]
        except Exception:
            pass
    _hw_cache["cpu"] = details
    return details


def get_mem_hardware_details():
    mem = {}
    mem_file = os.path.join(PROC_DIR, "meminfo")
    if os.path.exists(mem_file):
        try:
            with open(mem_file) as f:
                for line in f:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        parts = v.strip().split()
                        val = int(parts[0]) * 1024 if len(parts) > 1 and parts[1].lower() == "kb" else int(parts[0])
                        mem[k.strip()] = val
        except Exception:
            pass
    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", 0)
    free = mem.get("MemFree", 0)
    buffers = mem.get("Buffers", 0)
    cached = mem.get("Cached", 0)
    active = mem.get("Active", 0)
    inactive = mem.get("Inactive", 0)
    shmem = mem.get("Shmem", 0)
    sreclaim = mem.get("SReclaimable", 0)
    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free)
    swap_pct = round((swap_used / max(1, swap_total)) * 100.0, 1) if swap_total > 0 else 0.0

    return {
        "available": bool(total and "MemAvailable" in mem),
        "total_bytes": total,
        "total_formatted": format_bytes(total),
        "available_bytes": avail,
        "available_formatted": format_bytes(avail),
        "free_bytes": free,
        "free_formatted": format_bytes(free),
        "used_bytes": max(0, total - avail),
        "used_formatted": format_bytes(max(0, total - avail)),
        "buffers_bytes": buffers,
        "buffers_formatted": format_bytes(buffers),
        "cached_bytes": cached,
        "cached_formatted": format_bytes(cached),
        "active_bytes": active,
        "active_formatted": format_bytes(active),
        "inactive_bytes": inactive,
        "inactive_formatted": format_bytes(inactive),
        "shmem_bytes": shmem,
        "shmem_formatted": format_bytes(shmem),
        "sreclaimable_bytes": sreclaim,
        "sreclaimable_formatted": format_bytes(sreclaim),
        "swap_total_bytes": swap_total,
        "swap_total_formatted": format_bytes(swap_total),
        "swap_used_bytes": swap_used,
        "swap_used_formatted": format_bytes(swap_used),
        "swap_free_bytes": swap_free,
        "swap_free_formatted": format_bytes(swap_free),
        "swap_pct": swap_pct
    }


def get_block_devices():
    now = time.time()
    if _hw_cache["disks"] and (now - _hw_cache["last_disk_scan"]) < 10.0:
        return _hw_cache["disks"]

    devices = []
    by_id = {}
    dev_by_id_path = os.path.join(ROOT_DIR, "dev/disk/by-id") if ROOT_DIR != "/" else "/dev/disk/by-id"
    if os.path.exists(dev_by_id_path):
        try:
            for entry in os.listdir(dev_by_id_path):
                full = os.path.realpath(os.path.join(dev_by_id_path, entry))
                if full not in by_id:
                    by_id[full] = entry
        except Exception:
            pass

    mounts = {}
    mounts_file = os.path.join(PROC_DIR, "mounts")
    if os.path.exists(mounts_file):
        try:
            with open(mounts_file) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3 and parts[0].startswith("/dev/"):
                        dev = parts[0]
                        mp = parts[1]
                        fstype = parts[2]
                        if dev not in mounts or len(mp) < len(mounts[dev]["mount"]):
                            mounts[dev] = {"mount": mp, "fstype": fstype}
        except Exception:
            pass

    sys_block_dir = os.path.join(ROOT_DIR, "sys/block") if ROOT_DIR != "/" else "/sys/block"
    if not os.path.exists(sys_block_dir) and os.path.exists("/sys/block"):
        sys_block_dir = "/sys/block"

    if os.path.exists(sys_block_dir):
        try:
            for b in sorted(os.listdir(sys_block_dir)):
                if b.startswith(("loop", "ram", "dm-")):
                    continue
                b_path = os.path.join(sys_block_dir, b)
                dev_path = os.path.join(b_path, "device")

                model = ""
                for mf in (os.path.join(dev_path, "model"), os.path.join(b_path, "model")):
                    if os.path.exists(mf):
                        try:
                            with open(mf) as f:
                                model = f.read().strip()
                            break
                        except Exception:
                            pass

                serial = ""
                for sf in (os.path.join(dev_path, "serial"), os.path.join(b_path, "serial")):
                    if os.path.exists(sf):
                        try:
                            with open(sf) as f:
                                serial = f.read().strip()
                            break
                        except Exception:
                            pass

                rot = "NVMe SSD" if "nvme" in b else "SSD"
                rot_file = os.path.join(b_path, "queue/rotational")
                if os.path.exists(rot_file):
                    try:
                        with open(rot_file) as f:
                            if f.read().strip() == "1":
                                rot = "HDD"
                    except Exception:
                        pass

                size_bytes = 0
                size_file = os.path.join(b_path, "size")
                if os.path.exists(size_file):
                    try:
                        with open(size_file) as f:
                            size_bytes = int(f.read().strip()) * 512
                    except Exception:
                        pass

                disk_id = by_id.get(f"/dev/{b}", serial or b)

                parts = []
                for p in sorted(os.listdir(b_path)):
                    if p.startswith(b) and p != b:
                        p_dev = f"/dev/{p}"
                        p_size = 0
                        p_size_file = os.path.join(b_path, p, "size")
                        if os.path.exists(p_size_file):
                            try:
                                with open(p_size_file) as f:
                                    p_size = int(f.read().strip()) * 512
                            except Exception:
                                pass

                        m_info = mounts.get(p_dev, {})
                        mount_pt = m_info.get("mount", "")
                        fstype = m_info.get("fstype", "")

                        usage = None
                        if mount_pt:
                            try:
                                u = shutil.disk_usage(os.path.join(ROOT_DIR, mount_pt.lstrip("/")))
                                usage = {
                                    "total_bytes": u.total,
                                    "total_formatted": format_bytes(u.total),
                                    "used_bytes": u.used,
                                    "used_formatted": format_bytes(u.used),
                                    "free_bytes": u.free,
                                    "free_formatted": format_bytes(u.free),
                                    "pct": round((u.used / max(1, u.total)) * 100.0, 1)
                                }
                            except Exception:
                                pass

                        parts.append({
                            "name": p,
                            "device": p_dev,
                            "size_bytes": p_size,
                            "size_formatted": format_bytes(p_size),
                            "mount": mount_pt,
                            "fstype": fstype,
                            "usage": usage,
                            "id": by_id.get(p_dev, p)
                        })

                devices.append({
                    "device": f"/dev/{b}",
                    "name": b,
                    "model": model or "Generic Block Device",
                    "serial": serial or "N/A",
                    "id": disk_id,
                    "type": rot,
                    "size_bytes": size_bytes,
                    "size_formatted": format_bytes(size_bytes),
                    "partitions": parts
                })
        except Exception:
            pass

    _hw_cache["disks"] = devices
    _hw_cache["last_disk_scan"] = now
    return devices


def get_docker_engine_details():
    now = time.time()
    if _hw_cache["docker"] and (now - _hw_cache["last_docker_scan"]) < 30.0:
        return _hw_cache["docker"]

    info = {
        "docker_version": "Unknown",
        "api_version": "Unknown",
        "kernel_version": "Unknown",
        "os": "Linux",
        "arch": "Unknown",
        "driver": "Unknown",
        "cgroup_version": "Unknown",
        "containerd_version": "",
        "runc_version": ""
    }
    try:
        if os.path.exists(DOCKER_SOCKET):
            version = docker_json("/version", 1024 * 1024)
            daemon = docker_json("/info", 4 * 1024 * 1024)
            info.update(docker_version=version.get("Version", "Unknown"), api_version=version.get("ApiVersion", "Unknown"), kernel_version=version.get("KernelVersion", "Unknown"), os=daemon.get("OperatingSystem", version.get("Os", "Unknown")), arch=version.get("Arch", "Unknown"), driver=daemon.get("Driver", "Unknown"), cgroup_version=str(daemon.get("CgroupVersion", "Unknown")))
            for component in version.get("Components", []):
                if component.get("Name") in ("containerd", "runc"):
                    info[component["Name"] + "_version"] = component.get("Version", "Unknown")
    except (OSError, ValueError, RuntimeError, http.client.HTTPException):
        pass

    _hw_cache["docker"] = info
    _hw_cache["last_docker_scan"] = now
    return info


def get_system_metrics():
    global prev_cpu_idle, prev_cpu_total
    cpus = os.cpu_count() or 1

    # 1. System Host CPU %
    host_cpu_pct = None
    try:
        with open(os.path.join(PROC_DIR, "stat"), "r") as f:
            fields = [float(x) for x in f.readline().split()[1:9]]
            idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)
            total = sum(fields)
            if prev_cpu_total > 0:
                idle_delta = idle - prev_cpu_idle
                total_delta = total - prev_cpu_total
                if total_delta > 0:
                    host_cpu_pct = max(0.0, min(100.0, 100.0 * (1.0 - (idle_delta / total_delta))))
            prev_cpu_idle = idle
            prev_cpu_total = total
    except Exception:
        pass

    # Load avg
    try:
        l1, l5, l15 = os.getloadavg()
    except Exception:
        l1, l5, l15 = 0.0, 0.0, 0.0

    # Uptime
    uptime_sec = 0
    try:
        with open(os.path.join(PROC_DIR, "uptime"), "r") as f:
            uptime_sec = float(f.readline().split()[0])
    except Exception:
        pass
    d = int(uptime_sec // 86400)
    h = int((uptime_sec % 86400) // 3600)
    m = int((uptime_sec % 3600) // 60)
    uptime_str = f"{d}d {h}h {m}m" if d > 0 else f"{h}h {m}m"

    # 2. System Host RAM
    mem_total, mem_avail = 0, 0
    mem_avail_read = False
    try:
        with open(os.path.join(PROC_DIR, "meminfo"), "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    mem_avail = int(line.split()[1]) * 1024
                    mem_avail_read = True
    except Exception:
        pass
    memory_available = mem_total > 0 and mem_avail_read

    mem_used = mem_total - mem_avail
    mem_pct = round((mem_used / mem_total) * 100.0, 1) if memory_available else None

    # 3. System Storage / Disks (Root system disk and optional secondary data disk)
    try:
        du_root = shutil.disk_usage(ROOT_DIR)
        root_total = du_root.total
        root_used = du_root.used
        root_free = du_root.free
        root_pct = round((root_used / max(1, root_total)) * 100.0, 1)
    except Exception:
        root_total, root_used, root_free, root_pct = None, None, None, None

    root_disk = {
        "name": "System Disk",
        "mount": "/",
        "type": "Root Storage",
        "total_bytes": root_total,
        "used_bytes": root_used,
        "free_bytes": root_free,
        "total_formatted": format_bytes(root_total),
        "used_formatted": format_bytes(root_used),
        "free_formatted": format_bytes(root_free),
        "pct": root_pct
    }

    data_disk = None
    if SECONDARY_STORAGE_PATH and os.path.exists(SECONDARY_STORAGE_PATH):
        try:
            du_data = shutil.disk_usage(SECONDARY_STORAGE_PATH)
            data_disk = {
                "name": SECONDARY_STORAGE_LABEL,
                "mount": SECONDARY_STORAGE_PATH,
                "type": "Secondary Storage",
                "total_bytes": du_data.total,
                "used_bytes": du_data.used,
                "free_bytes": du_data.free,
                "total_formatted": format_bytes(du_data.total),
                "used_formatted": format_bytes(du_data.used),
                "free_formatted": format_bytes(du_data.free),
                "pct": round((du_data.used / max(1, du_data.total)) * 100.0, 1)
            }
        except Exception:
            pass

    # Detailed hardware inventories for modal popups
    details = {
        "cpu": get_cpu_hardware_details(),
        "memory": get_mem_hardware_details(),
        "storage": {
            "devices": get_block_devices(),
            "data_root": os.environ.get("DOCKPIT_DATA_ROOT", "/var/lib/docker")
        },
        "docker": get_docker_engine_details()
    }

    return {
        "cpus": cpus,
        "host_cpu_pct": round(host_cpu_pct, 1) if host_cpu_pct is not None else None,
        "availability": {"cpu": host_cpu_pct is not None, "memory": memory_available, "storage": root_total is not None},
        "load": [round(l1, 2), round(l5, 2), round(l15, 2)],
        "load_str": f"{l1:.2f}, {l5:.2f}, {l15:.2f}",
        "uptime_str": uptime_str,
        "mem_used_bytes": mem_used,
        "mem_total_bytes": mem_total,
        "mem_used_formatted": format_bytes(mem_used if memory_available else None),
        "mem_total_formatted": format_bytes(mem_total if memory_available else None),
        "mem_pct": mem_pct,
        "root_disk": root_disk,
        "root_total_formatted": format_bytes(root_total),
        "root_used_formatted": format_bytes(root_used),
        "root_free_formatted": format_bytes(root_free),
        "root_pct": root_pct,
        "data_disk": data_disk,
        "details": details
    }


def docker_json(path, limit=16 * 1024 * 1024):
    with closing(UnixHTTPConnection(timeout=5)) as conn:
        conn.request("GET", path)
        response = conn.getresponse()
        if response.status != 200:
            raise RuntimeError(f"Docker API returned HTTP {response.status}")
        body = response.read(limit + 1)
        if len(body) > limit:
            raise RuntimeError("Docker API response exceeded size limit")
        return json.loads(body)


def container_metadata(c):
    labels = c.get("Labels") or {}
    name = (c.get("Names") or [c["Id"][:12]])[0].lstrip("/")
    ports = []
    for port in c.get("Ports", []):
        private = f'{port.get("PrivatePort", "")}/{port.get("Type", "tcp")}'
        ports.append(f'{port.get("IP", "")}:{port["PublicPort"]}->{private}' if port.get("PublicPort") else private)
    sources = [m.get("Source", "") for m in c.get("Mounts", []) if isinstance(m, dict)]
    # Docker reports host paths, while statvfs uses the mounted host root.
    secondary = SECONDARY_STORAGE_PATH
    if secondary and ROOT_DIR != "/" and secondary.startswith(ROOT_DIR.rstrip("/") + "/"):
        secondary = secondary[len(ROOT_DIR.rstrip("/")):]
    is_secondary = lambda src: bool(secondary and (src == secondary or src.startswith(secondary.rstrip("/") + "/")))
    uses_secondary = any(is_secondary(src) for src in sources)
    uses_root = any(src and not is_secondary(src) for src in sources)
    url = labels.get("dockpit.url", "")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or len(url) > 2048 or any(ord(ch) < 32 for ch in url):
        url = ""
    icon = labels.get("dockpit.icon", "fa-cube")
    if not re.fullmatch(r"fa-[a-z0-9-]{1,60}", icon):
        icon = "fa-cube"
    return {
        "id": c["Id"][:12], "full_id": c["Id"], "name": name,
        "display_name": labels.get("dockpit.name", name)[:120], "url": url, "icon": icon,
        "stack": labels.get("com.docker.compose.project") or "Standalone",
        "image": c.get("Image", ""), "state": c.get("State", "unknown"),
        "status": c.get("Status", ""), "created": c.get("Status", ""),
        "health": container_health(c.get("State"), c.get("Status", "")),
        "ports": ", ".join(ports), "service": labels.get("com.docker.compose.service", name),
        "storage_tier": "Root + Secondary" if uses_secondary and uses_root else "Secondary Storage" if uses_secondary else "Root Storage",
    }


def fetch_single_container_stats(c, host_mem_bytes, cpus):
    row = container_metadata(c)
    st = {}
    if c.get("State") == "running":
        try:
            st = docker_json(f'/containers/{c["Id"]}/stats?stream=false&one-shot=true')
        except (OSError, ValueError, RuntimeError, http.client.HTTPException):
            pass
    available = bool(st.get("cpu_stats") or st.get("memory_stats"))
    cpu = st.get("cpu_stats", {})
    previous = st.get("precpu_stats", {})
    current_total = cpu.get("cpu_usage", {}).get("total_usage", 0)
    current_system = cpu.get("system_cpu_usage", 0)
    with _cpu_cache_lock:
        prior = _container_cpu_cache.get(c["Id"])
        previous_total, previous_system = prior[:2] if prior else (previous.get("cpu_usage", {}).get("total_usage", 0), previous.get("system_cpu_usage", 0))
        if available:
            _container_cpu_cache[c["Id"]] = (current_total, current_system, time.time())
    delta, system_delta = current_total - previous_total, current_system - previous_system
    online = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage") or []) or cpus
    cpu_percent = max(0, delta / system_delta * online * 100) if delta > 0 and system_delta > 0 else 0
    mem = st.get("memory_stats", {})
    cache = mem.get("stats", {}).get("inactive_file", mem.get("stats", {}).get("total_inactive_file", 0))
    used = max(0, mem.get("usage", 0) - cache)
    limit = mem.get("limit") or host_mem_bytes or 0
    net = st.get("networks", {})
    block = st.get("blkio_stats", {}).get("io_service_bytes_recursive") or []
    row.update({
        "cpu_perc": round(cpu_percent, 2), "stats_available": available,
        "mem_used_bytes": used, "mem_limit_bytes": limit,
        "mem_used_formatted": format_bytes(used), "mem_limit_formatted": format_bytes(limit),
        "mem_perc": round(used / limit * 100, 2) if limit else 0,
        "has_custom_limit": bool(limit and host_mem_bytes and limit < host_mem_bytes),
        "pids": st.get("pids_stats", {}).get("current", 0),
        "net_rx": format_bytes(sum(n.get("rx_bytes", 0) for n in net.values())),
        "net_tx": format_bytes(sum(n.get("tx_bytes", 0) for n in net.values())),
        "block_read": format_bytes(sum(b.get("value", 0) for b in block if b.get("op", "").lower() == "read")),
        "block_write": format_bytes(sum(b.get("value", 0) for b in block if b.get("op", "").lower() == "write")),
    })
    return row


def aggregate_snapshot(rows, system, backend):
    stacks = {}
    for row in rows:
        stack = stacks.setdefault(row["stack"], {"name": row["stack"], "containers": [], "total_cpu": 0, "total_mem_bytes": 0, "total_pids": 0, "healthy_count": 0, "total_containers": 0})
        stack["containers"].append(row)
        stack["total_cpu"] += row["cpu_perc"]
        stack["total_mem_bytes"] += row["mem_used_bytes"]
        stack["total_pids"] += row["pids"]
        stack["total_containers"] += 1
        stack["healthy_count"] += row["health"] in ("healthy", "running")
    cpu = round(sum(r["cpu_perc"] for r in rows), 2)
    memory = sum(r["mem_used_bytes"] for r in rows)
    host_memory = system["mem_total_bytes"]
    host_cpu = round(cpu / max(1, system["cpus"]), 1)
    mem_percent = round(memory / host_memory * 100, 1) if host_memory else None
    for stack in stacks.values():
        stack["total_cpu"] = round(stack["total_cpu"], 2)
        stack["total_mem_formatted"] = format_bytes(stack["total_mem_bytes"])
        stack["host_mem_perc"] = round(stack["total_mem_bytes"] / host_memory * 100, 2) if host_memory else None
        stack["docker_mem_share"] = round(stack["total_mem_bytes"] / memory * 100, 1) if memory else 0
        stack["containers"].sort(key=lambda r: (r["health"] not in ("unhealthy", "exited", "dead", "restarting"), -r["cpu_perc"]))
    return {
        "timestamp": time.time(), "system": system, "backend": backend,
        "docker_total_cpu": cpu, "docker_host_cpu_perc": host_cpu,
        "total_cpu": cpu, "total_cpu_host_perc": host_cpu,
        "docker_total_mem_bytes": memory, "docker_total_mem_str": format_bytes(memory),
        "docker_total_mem_perc": mem_percent, "total_mem_bytes": memory,
        "total_mem_str": format_bytes(memory), "total_mem_perc": mem_percent,
        "host_total_mem_bytes": host_memory, "host_total_mem_str": format_bytes(host_memory or None),
        "running_containers": sum(r["state"] == "running" for r in rows),
        "unhealthy_containers": sum(r["health"] == "unhealthy" for r in rows),
        "total_containers": len(rows), "total_stacks": len(stacks),
        "total_pids": sum(r["pids"] for r in rows), "stacks": stacks,
    }


def _fetch_docker_metrics_socket():
    system = get_system_metrics()
    containers = docker_json("/containers/json?all=1")
    containers = [c for c in containers if (c.get("Labels") or {}).get("dockpit.hide", "").lower() != "true"]
    running_ids = {c["Id"] for c in containers if c.get("State") == "running"}
    with _cpu_cache_lock:
        for cid in list(_container_cpu_cache):
            if cid not in running_ids:
                del _container_cpu_cache[cid]
    with ThreadPoolExecutor(max_workers=16) as pool:
        rows = list(pool.map(lambda c: fetch_single_container_stats(c, system["mem_total_bytes"], system["cpus"]), containers))
    return aggregate_snapshot(rows, system, "socket")


def _fetch_docker_metrics_subprocess():
    system = get_system_metrics()
    def read_cli(args):
        result = subprocess.run(["docker"] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True, timeout=20)
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    containers = read_cli(["ps", "--all", "--format", "{{json .}}"])
    stats = {s["Name"]: s for s in read_cli(["stats", "--no-stream", "--format", "{{json .}}"])}
    rows = []
    for container in containers:
        labels = parse_labels(container.get("Labels", ""))
        if labels.get("dockpit.hide", "").lower() == "true":
            continue
        name = container.get("Names", "")
        name = name[0].lstrip("/") if isinstance(name, list) else name.lstrip("/")
        # Construct the same metadata as the socket backend without requesting stats.
        c = {"Id": container["ID"], "Names": [name], "Labels": labels, "Image": container.get("Image", ""), "State": container.get("State"), "Status": container.get("Status", "")}
        row = container_metadata(c)
        stat = stats.get(name, {}) if c["State"] == "running" else {}
        used, _, limit = stat.get("MemUsage", "0B / 0B").partition("/")
        used_bytes, limit_bytes = parse_bytes(used), parse_bytes(limit)
        row.update({
            "cpu_perc": float(stat.get("CPUPerc", "0%").rstrip("%") or 0),
            "mem_used_bytes": used_bytes, "mem_limit_bytes": limit_bytes,
            "mem_used_formatted": format_bytes(used_bytes), "mem_limit_formatted": format_bytes(limit_bytes),
            "mem_perc": float(stat.get("MemPerc", "0%").rstrip("%") or 0),
            "has_custom_limit": bool(limit_bytes and system["mem_total_bytes"] and limit_bytes < system["mem_total_bytes"]),
            "pids": int(stat.get("PIDs", "0") or 0), "stats_available": bool(stat),
            "ports": container.get("Ports", ""), "net_rx": "Unavailable", "net_tx": "Unavailable",
            "block_read": "Unavailable", "block_write": "Unavailable",
        })
        rows.append(row)
    return aggregate_snapshot(rows, system, "cli")


def fetch_docker_metrics():
    if os.path.exists(DOCKER_SOCKET):
        try:
            return _fetch_docker_metrics_socket()
        except (OSError, ValueError, RuntimeError, http.client.HTTPException):
            if not shutil.which("docker"):
                raise
    return _fetch_docker_metrics_subprocess()


def poll_once():
    global cached_data
    attempted = time.time()
    try:
        data = fetch_docker_metrics()
        if not data:
            raise RuntimeError("No Docker snapshot returned")
        with cache_lock:
            cached_data = data
            poll_status.update(last_attempt=attempted, last_success=data["timestamp"], last_error=None, backend=data["backend"])
            history.append({"timestamp": data["timestamp"], "host_cpu": data["system"]["host_cpu_pct"], "host_memory": data["system"]["mem_pct"], "docker_cpu": data["docker_host_cpu_perc"], "docker_memory": data["docker_total_mem_bytes"]})
            while history and history[0]["timestamp"] < attempted - 3600:
                history.popleft()
        return True
    except Exception as error:
        # Keep the worker alive; do not log daemon payloads or command output.
        message = "Docker access failed. Check socket path, permissions and daemon availability."
        print(f"{message} ({type(error).__name__})", file=sys.stderr)
        with cache_lock:
            poll_status.update(last_attempt=attempted, last_error=message)
        return False


def background_poller():
    while not stop_event.wait(POLL_INTERVAL):
        poll_once()


STATIC_DIR = Path(__file__).with_name("static")
HTML_PAGE = (STATIC_DIR / "index.html").read_text(encoding="utf-8")



def docker_api_inspect(name):
    if os.path.exists(DOCKER_SOCKET):
        return docker_json(f"/containers/{name}/json", 4 * 1024 * 1024)
    result = subprocess.run(["docker", "inspect", name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True, timeout=8)
    items = json.loads(result.stdout)
    return items[0] if items else None


def cli_logs(name, tail, timestamps=False):
    args = ["docker", "logs", "--tail", str(tail)]
    if timestamps:
        args.append("--timestamps")
    args.append(name)
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
        chunks, size = [], 0
        deadline = time.monotonic() + 10
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while size < MAX_LOG_BYTES:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise TimeoutError("Log request timed out")
                    chunk = os.read(proc.stdout.fileno(), min(65536, MAX_LOG_BYTES - size))
                    if not chunk:
                        proc.wait(timeout=max(0.1, deadline - time.monotonic()))
                        if proc.returncode:
                            raise RuntimeError("Logs could not be retrieved")
                        break
                    chunks.append(chunk)
                    size += len(chunk)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        return b"".join(chunks).decode("utf-8", errors="replace")


def docker_api_logs(name, tail=100, timestamps=False):
    if not os.path.exists(DOCKER_SOCKET):
        return cli_logs(name, tail, timestamps)
    tty = bool((docker_api_inspect(name) or {}).get("Config", {}).get("Tty"))
    with closing(UnixHTTPConnection(timeout=8)) as conn:
        conn.request("GET", f"/containers/{name}/logs?stdout=1&stderr=1&tail={tail}&timestamps={int(timestamps)}")
        response = conn.getresponse()
        if response.status != 200:
            raise RuntimeError("Logs could not be retrieved")
        if tty:
            return response.read(MAX_LOG_BYTES).decode("utf-8", errors="replace")
        chunks, size, frames = [], 0, 0
        deadline = time.monotonic() + 10
        while size < MAX_LOG_BYTES and frames < 8192:
            if time.monotonic() >= deadline:
                raise TimeoutError("Log request timed out")
            header = response.read(8)
            if not header:
                break
            if len(header) != 8 or header[0] not in (0, 1, 2) or header[1:4] != b"\0\0\0":
                raise RuntimeError("Malformed Docker log stream")
            length = int.from_bytes(header[4:], "big")
            payload = response.read(min(length, MAX_LOG_BYTES - size))
            chunks.append(payload)
            size += len(payload)
            frames += 1
            if len(payload) < length:
                break
        return b"".join(chunks).decode("utf-8", errors="replace")


class DockerCockpitHandler(BaseHTTPRequestHandler):
    timeout = 10

    def log_message(self, format, *args):
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_body(self, body, content_type, code=200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, data, code=200):
        self.send_body(json.dumps(data).encode("utf-8"), "application/json; charset=utf-8", code)

    def do_HEAD(self):
        self.do_GET()

    def known_container(self, name):
        if not name or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*", name):
            return False
        with cache_lock:
            return any(name in (c.get("name"), c.get("id"), c.get("full_id")) for stack in cached_data.get("stacks", {}).values() for c in stack.get("containers", []))

    def do_GET(self):
        limited = urlparse(self.path).path in ("/api/inspect", "/api/logs")
        if limited and not api_slots.acquire(blocking=False):
            self.send_json({"error": "Too many concurrent Docker requests"}, 429)
            return
        try:
            self.handle_get()
        finally:
            if limited:
                api_slots.release()

    def handle_get(self):
        parsed = urlparse(self.path)
        path, params = parsed.path, parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            self.send_body(HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path.startswith("/static/"):
            target = (STATIC_DIR / path[len("/static/"):]).resolve()
            types = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".woff2": "font/woff2"}
            if STATIC_DIR.resolve() not in target.parents or not target.is_file() or target.suffix not in types:
                self.send_json({"error": "Not found"}, 404)
                return
            self.send_body(target.read_bytes(), types[target.suffix])
            return
        if path in ("/healthz", "/readyz", "/api/diagnostics", "/api/history", "/api/stats"):
            with cache_lock:
                data = dict(cached_data)
                status = dict(poll_status)
                points = [dict(point) for point in history if point["timestamp"] >= time.time() - 3600]
            timestamp = data.get("timestamp", 0)
            stale = not timestamp or time.time() - timestamp > STALE_AFTER
            if path == "/healthz":
                self.send_json({"status": "ok", "version": __version__})
            elif path == "/readyz":
                self.send_json({"ready": not stale, "stale": stale}, 503 if stale else 200)
            elif path == "/api/history":
                self.send_json({"retention_seconds": 3600, "points": points})
            elif path == "/api/diagnostics":
                self.send_json({"version": __version__, "poll_interval": POLL_INTERVAL, "stale": stale, "socket_exists": os.path.exists(DOCKER_SOCKET), "socket_accessible": os.access(DOCKER_SOCKET, os.R_OK | os.W_OK), "measurements": data.get("system", {}).get("availability", {}), **status})
            else:
                data["stale"] = stale
                self.send_json(data, 200 if timestamp else 503)
            return
        if path in ("/api/inspect", "/api/logs"):
            name = params.get("name", [None])[0]
            if not self.known_container(name):
                self.send_json({"error": "Unknown or missing container name"}, 400)
                return
            if path == "/api/logs":
                try:
                    tail = min(500, max(1, int(params.get("tail", ["100"])[0])))
                except ValueError:
                    self.send_json({"error": "Invalid tail line count"}, 400)
                    return
                try:
                    output = docker_api_logs(name, tail, params.get("timestamps", ["0"])[0] == "1")
                    self.send_body(output.encode("utf-8")[:MAX_LOG_BYTES], "text/plain; charset=utf-8")
                except Exception:
                    self.send_json({"error": "Container logs could not be retrieved"}, 502)
                return
            try:
                item = docker_api_inspect(name)
                if not item:
                    self.send_json({"error": "Container not found"}, 404)
                    return
                state = item.get("State", {})
                health = state.get("Health", {})
                self.send_json({
                    "Name": item.get("Name"),
                    "State": {key: state.get(key) for key in ("Status", "OOMKilled", "StartedAt", "FinishedAt", "ExitCode", "Pid")},
                    "Health": {"Status": health.get("Status"), "FailingStreak": health.get("FailingStreak", 0)},
                    "RestartCount": item.get("RestartCount", 0),
                    "Resources": {key: item.get("HostConfig", {}).get(key) for key in ("Memory", "NanoCpus", "CpuQuota", "CpuPeriod", "PidsLimit", "ShmSize", "RestartPolicy")},
                    "Mounts": item.get("Mounts", []),
                    "NetworkSettings": item.get("NetworkSettings", {}),
                })
            except Exception:
                self.send_json({"error": "Container inspection failed"}, 502)
            return
        self.send_json({"error": "Not found"}, 404)


def main():
    print(f"Starting Dockpit {__version__}")
    stop_event.clear()
    poll_once()
    poller = threading.Thread(target=background_poller, daemon=True)
    poller.start()
    server = ThreadingHTTPServer((HOST, PORT), DockerCockpitHandler)
    print(f"Dashboard running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()
        poller.join(timeout=1)


if __name__ == "__main__":
    main()
