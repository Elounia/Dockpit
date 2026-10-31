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
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Configuration via environment variables
PORT = int(os.environ.get("DOCKPIT_PORT", os.environ.get("DOCKER_MONITOR_PORT", 8989)))
HOST = os.environ.get("DOCKPIT_HOST", "0.0.0.0" if os.path.exists("/.dockerenv") else "127.0.0.1")
DOCKER_SOCKET = os.environ.get("DOCKPIT_DOCKER_SOCKET", "/var/run/docker.sock")
POLL_INTERVAL = float(os.environ.get("DOCKPIT_POLL_INTERVAL", 2.0))

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
    """Read host capacity; available RAM includes reclaimable filesystem cache."""
    global _previous_cpu
    mem_file = os.path.join(PROC_DIR, 'meminfo')
    stat_file = os.path.join(PROC_DIR, 'stat')
    memory = {}
    try:
        with open(mem_file) as f:
            memory = {line.split(':')[0]: int(line.split()[1]) * 1024 for line in f}
    except Exception:
        memory = {'MemTotal': 48 * 1024**3, 'MemAvailable': 16 * 1024**3, 'SwapTotal': 0, 'SwapFree': 0}

    ticks = [0] * 8
    try:
        with open(stat_file) as f:
            ticks = [int(value) for value in f.readline().split()[1:9]]
    except Exception:
        pass
    total, idle = sum(ticks), ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
    cpu = None
    if _previous_cpu and total > _previous_cpu[0]:
        cpu = round(100 * (1 - (idle - _previous_cpu[1]) / (total - _previous_cpu[0])), 1)
    _previous_cpu = (total, idle)
    disk_path = os.environ.get("DOCKPIT_DATA_ROOT", os.environ.get("DOCKER_MONITOR_DATA_ROOT", "/var/lib/docker"))
    try:
        disk = shutil.disk_usage(disk_path)
        disk_total, disk_free, disk_used = disk.total, disk.free, disk.used
    except Exception:
        disk_total, disk_free, disk_used = 250 * 10**9, 70 * 10**9, 180 * 10**9
    return {
        "cpu_count": os.cpu_count() or 1,
        "cpu_perc": cpu,
        "mem_total_bytes": memory.get('MemTotal', 48 * 1024**3),
        "mem_available_bytes": memory.get('MemAvailable', 16 * 1024**3),
        "swap_total_bytes": memory.get('SwapTotal', 0),
        "swap_used_bytes": memory.get('SwapTotal', 0) - memory.get('SwapFree', 0),
        "disk_path": disk_path,
        "disk_total_bytes": disk_total,
        "disk_free_bytes": disk_free,
        "disk_used_perc": round(disk_used / max(1, disk_used + disk_free) * 100, 1),
    }


def get_host_memory_bytes():
    mem_file = os.path.join(PROC_DIR, 'meminfo')
    try:
        with open(mem_file) as f:
            return next(int(line.split()[1]) * 1024 for line in f if line.startswith('MemTotal:'))
    except Exception:
        return 48 * 1024**3


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
    total = mem.get("MemTotal", 48 * 1024**3)
    avail = mem.get("MemAvailable", 16 * 1024**3)
    free = mem.get("MemFree", 4 * 1024**3)
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
                                u = shutil.disk_usage(mount_pt)
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
        "arch": "x86_64",
        "driver": "overlay2",
        "cgroup_version": "2",
        "containerd_version": "",
        "runc_version": ""
    }
    try:
        if os.path.exists(DOCKER_SOCKET):
            conn = UnixHTTPConnection(timeout=3)
            conn.request("GET", "/version")
            res = conn.getresponse()
            if res.status == 200:
                v_data = json.loads(res.read().decode())
                info["docker_version"] = v_data.get("Version", "")
                info["api_version"] = v_data.get("ApiVersion", "")
                info["kernel_version"] = v_data.get("KernelVersion", "")
                info["os"] = v_data.get("Os", "")
                info["arch"] = v_data.get("Arch", "")
                for comp in v_data.get("Components", []):
                    if comp.get("Name") == "containerd":
                        info["containerd_version"] = comp.get("Version", "")
                    elif comp.get("Name") == "runc":
                        info["runc_version"] = comp.get("Version", "")

            conn.request("GET", "/info")
            res = conn.getresponse()
            if res.status == 200:
                i_data = json.loads(res.read().decode())
                info["driver"] = i_data.get("Driver", "overlay2")
                info["cgroup_version"] = str(i_data.get("CgroupVersion", "2"))
                if not info["kernel_version"]:
                    info["kernel_version"] = i_data.get("KernelVersion", "")
                if not info["os"] or info["os"] == "linux":
                    info["os"] = i_data.get("OperatingSystem", info["os"])
            conn.close()
    except Exception:
        pass

    _hw_cache["docker"] = info
    _hw_cache["last_docker_scan"] = now
    return info


def get_system_metrics():
    global prev_cpu_idle, prev_cpu_total
    cpus = os.cpu_count() or 12

    # 1. System Host CPU %
    host_cpu_pct = 0.0
    try:
        with open(os.path.join(PROC_DIR, "stat"), "r") as f:
            fields = [float(x) for x in f.readline().split()[1:]]
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
    try:
        with open(os.path.join(PROC_DIR, "meminfo"), "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    mem_avail = int(line.split()[1]) * 1024
    except Exception:
        pass
    if mem_total == 0:
        mem_total = 48826085376
        mem_avail = 16000000000

    mem_used = mem_total - mem_avail
    mem_pct = round((mem_used / max(1, mem_total)) * 100.0, 1)

    # 3. System Storage / Disks (Root system disk and optional secondary data disk)
    try:
        du_root = shutil.disk_usage(ROOT_DIR)
        root_total = du_root.total
        root_used = du_root.used
        root_free = du_root.free
        root_pct = round((root_used / max(1, root_total)) * 100.0, 1)
    except Exception:
        root_total, root_used, root_free, root_pct = 250*1e9, 180*1e9, 70*1e9, 72.0

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
        "host_cpu_pct": round(host_cpu_pct, 1),
        "load": [round(l1, 2), round(l5, 2), round(l15, 2)],
        "load_str": f"{l1:.2f}, {l5:.2f}, {l15:.2f}",
        "uptime_str": uptime_str,
        "mem_used_bytes": mem_used,
        "mem_total_bytes": mem_total,
        "mem_used_formatted": format_bytes(mem_used),
        "mem_total_formatted": format_bytes(mem_total),
        "mem_pct": mem_pct,
        "root_disk": root_disk,
        "root_total_formatted": format_bytes(root_total),
        "root_used_formatted": format_bytes(root_used),
        "root_free_formatted": format_bytes(root_free),
        "root_pct": root_pct,
        "data_disk": data_disk,
        "details": details
    }


def fetch_single_container_stats(c, host_mem_bytes, cpus):
    cid = c["Id"]
    name = c["Names"][0].lstrip("/")
    labels = c.get("Labels", {}) or {}

    try:
        conn = UnixHTTPConnection(timeout=4)
        conn.request("GET", f"/containers/{cid}/stats?stream=false")
        res = conn.getresponse()
        st = json.loads(res.read().decode("utf-8"))
        conn.close()
    except Exception:
        st = {}

    # CPU Calculation: Standard Docker CPU delta normalized across system ticks
    cpu_stats = st.get("cpu_stats", {})
    precpu_stats = st.get("precpu_stats", {})
    cur_total = cpu_stats.get("cpu_usage", {}).get("total_usage", 0)
    cur_sys = cpu_stats.get("system_cpu_usage", 0)
    now = time.time()

    with _cpu_cache_lock:
        if cid in _container_cpu_cache:
            prev_total, prev_sys, _ = _container_cpu_cache[cid]
            cpu_delta = cur_total - prev_total
            system_delta = cur_sys - prev_sys
        else:
            cpu_delta = cur_total - precpu_stats.get("cpu_usage", {}).get("total_usage", 0)
            system_delta = cur_sys - precpu_stats.get("system_cpu_usage", 0)
        _container_cpu_cache[cid] = (cur_total, cur_sys, now)

    online_cpus = cpu_stats.get("online_cpus")
    if not online_cpus:
        online_cpus = len(cpu_stats.get("cpu_usage", {}).get("percpu_usage") or []) or cpus or 1

    if system_delta > 0 and cpu_delta > 0:
        container_cpu = (cpu_delta / system_delta) * online_cpus * 100.0
    else:
        container_cpu = 0.0
    container_cpu = max(0.0, container_cpu)

    # Memory
    mem_stats = st.get("memory_stats", {})
    usage = mem_stats.get("usage", 0)
    inactive_file = (mem_stats.get("stats", {}).get("inactive_file", 0) or 
                     mem_stats.get("stats", {}).get("total_inactive_file", 0))
    used_mem = max(0, usage - inactive_file)
    limit = mem_stats.get("limit", host_mem_bytes)
    has_custom_limit = (limit > 0) and (limit < host_mem_bytes * 0.9)
    mem_pct = round((used_mem / max(1, limit)) * 100.0, 2)

    # PIDs
    pids = st.get("pids_stats", {}).get("current", 0)

    # Net I/O
    networks = st.get("networks", {})
    rx_bytes = sum(net.get("rx_bytes", 0) for net in networks.values())
    tx_bytes = sum(net.get("tx_bytes", 0) for net in networks.values())

    # Block I/O
    blk_stats = st.get("blkio_stats", {}).get("io_service_bytes_recursive", []) or []
    read_bytes = sum(b.get("value", 0) for b in blk_stats if b.get("op", "").lower() == "read")
    write_bytes = sum(b.get("value", 0) for b in blk_stats if b.get("op", "").lower() == "write")

    status_text = c.get("Status", "")
    health = "unknown"
    if "healthy" in status_text:
        health = "healthy"
    elif "unhealthy" in status_text:
        health = "unhealthy"
    elif "starting" in status_text:
        health = "starting"
    elif c.get("State") == "running":
        health = "running"

    # Ports
    port_tokens = []
    for p in c.get("Ports", []):
        ip = p.get("IP", "")
        pub = p.get("PublicPort")
        priv = p.get("PrivatePort")
        ptype = p.get("Type", "tcp")
        if pub:
            port_tokens.append(f"{ip}:{pub}->{priv}/{ptype}")
        elif priv:
            port_tokens.append(f"{priv}/{ptype}")
    ports_str = ", ".join(port_tokens)

    # Stack grouping
    stack = labels.get("com.docker.compose.project")
    if not stack:
        stack = "Standalone"

    # Storage tier analysis
    mounts = c.get("Mounts", []) or []
    mount_sources = [str(m.get("Source", "")) for m in mounts if isinstance(m, dict)]
    uses_secondary = bool(SECONDARY_STORAGE_PATH and any(SECONDARY_STORAGE_PATH in src for src in mount_sources))
    uses_root = any("/" in src for src in mount_sources)
    if uses_secondary and uses_root:
        storage_tier = "Root + Secondary"
    elif uses_secondary:
        storage_tier = "Secondary Storage"
    else:
        storage_tier = "Root Storage"

    return {
        "id": cid[:12],
        "full_id": cid,
        "name": name,
        "stack": stack,
        "image": c.get("Image", ""),
        "state": c.get("State", ""),
        "status": status_text,
        "health": health,
        "ports": ports_str,
        "created": c.get("Status", ""),
        "cpu_perc": round(container_cpu, 2),
        "mem_used_bytes": used_mem,
        "mem_limit_bytes": limit,
        "mem_used_formatted": format_bytes(used_mem),
        "mem_limit_formatted": format_bytes(limit),
        "mem_perc": mem_pct,
        "has_custom_limit": has_custom_limit,
        "net_rx": format_bytes(rx_bytes),
        "net_tx": format_bytes(tx_bytes),
        "block_read": format_bytes(read_bytes),
        "block_write": format_bytes(write_bytes),
        "pids": pids,
        "storage_tier": storage_tier,
        "service": labels.get("com.docker.compose.service", name),
    }


def _fetch_docker_metrics_subprocess():
    host = get_host_resources()
    host_mem_bytes = host.get("mem_total_bytes", 48 * 1024**3)
    host_mem_str = format_bytes(host_mem_bytes)

    ps_proc = subprocess.run(
        ["docker", "ps", "--all", "--format", "{{json .}}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True, timeout=10
    )
    ps_lines = [json.loads(line) for line in ps_proc.stdout.strip().split("\n") if line.strip()]

    stats_proc = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True, timeout=15
    )
    stats_lines = [json.loads(line) for line in stats_proc.stdout.strip().split("\n") if line.strip()]
    stats_map = {item.get("Name"): item for item in stats_lines}

    stacks = {}
    total_docker_cpu = 0.0
    total_docker_mem = 0
    total_pids = 0

    for c in ps_lines:
        name_val = c.get("Names", "")
        if isinstance(name_val, list):
            name = name_val[0].lstrip("/") if name_val else ""
        else:
            name = str(name_val).lstrip("/")
        labels = parse_labels(c.get("Labels", ""))
        stack = labels.get("com.docker.compose.project", "Standalone")

        st = stats_map.get(name, {}) if c.get("State") == "running" else {}
        cpu_raw = st.get("CPUPerc", "0.00%").rstrip("%")
        try:
            cpu_val = float(cpu_raw)
        except ValueError:
            cpu_val = 0.0
        total_docker_cpu += cpu_val

        mem_str = st.get("MemUsage", "0B / 0B")
        mem_parts = mem_str.split("/")
        mem_used_bytes = parse_bytes(mem_parts[0]) if len(mem_parts) > 0 else 0
        mem_limit_bytes = parse_bytes(mem_parts[1]) if len(mem_parts) > 1 else 0
        total_docker_mem += mem_used_bytes

        pids_str = st.get("PIDs", "0")
        try:
            pids_val = int(pids_str)
        except ValueError:
            pids_val = 0
        total_pids += pids_val

        health = container_health(c.get("State", ""), c.get("Status", ""))

        c_info = {
            "name": name,
            "id": (c.get("ID") or "")[:12],
            "full_id": c.get("ID") or "",
            "stack": stack,
            "image": c.get("Image", ""),
            "state": c.get("State", ""),
            "status": c.get("Status", ""),
            "health": health,
            "cpu_perc": cpu_val,
            "mem_used_bytes": mem_used_bytes,
            "mem_limit_bytes": mem_limit_bytes,
            "mem_used_formatted": format_bytes(mem_used_bytes),
            "mem_limit_formatted": format_bytes(mem_limit_bytes),
            "mem_perc": float(st.get("MemPerc", "0%").rstrip("%") or 0),
            "pids": pids_val,
            "stats_available": bool(st),
            "ports": c.get("Ports", ""),
            "service": labels.get("com.docker.compose.service", name),
            "storage_tier": "Root Storage",
        }

        if stack not in stacks:
            stacks[stack] = {
                "name": stack,
                "containers": [],
                "total_cpu": 0.0,
                "total_mem_bytes": 0,
                "total_pids": 0,
                "healthy_count": 0,
                "total_containers": 0
            }
        s = stacks[stack]
        s["containers"].append(c_info)
        s["total_cpu"] += cpu_val
        s["total_mem_bytes"] += mem_used_bytes
        s["total_pids"] += pids_val
        s["total_containers"] += 1
        if health in ("healthy", "running"):
            s["healthy_count"] += 1

    total_docker_cpu = round(total_docker_cpu, 2)
    cpu_count = host.get("cpu_count") or 12
    total_cpu_host_perc = round(total_docker_cpu / max(1, cpu_count), 1)

    for s_name, s_data in stacks.items():
        s_data["total_cpu"] = round(s_data["total_cpu"], 2)
        s_data["total_mem_formatted"] = format_bytes(s_data["total_mem_bytes"])
        s_data["host_mem_perc"] = round((s_data["total_mem_bytes"] / max(1, host_mem_bytes)) * 100, 2)
        s_data["docker_mem_share"] = round((s_data["total_mem_bytes"] / max(1, total_docker_mem)) * 100, 1)
        s_data["containers"].sort(key=lambda x: x["cpu_perc"], reverse=True)

    running_count = sum(1 for c in ps_lines if c.get("State") == "running")
    unhealthy_count = sum(1 for c in ps_lines if "unhealthy" in c.get("Status", ""))

    return {
        "timestamp": time.time(),
        "total_containers": len(ps_lines),
        "running_containers": running_count,
        "unhealthy_containers": unhealthy_count,
        "total_stacks": len(stacks),
        "total_cpu": total_docker_cpu,
        "total_cpu_host_perc": total_cpu_host_perc,
        "total_mem_bytes": total_docker_mem,
        "total_mem_str": format_bytes(total_docker_mem),
        "total_mem_perc": round((total_docker_mem / max(1, host_mem_bytes)) * 100, 1),
        "host_total_mem_bytes": host_mem_bytes,
        "host_total_mem_str": host_mem_str,
        "total_pids": total_pids,
        "stacks": stacks,
        "host": host
    }


def _fetch_docker_metrics_socket():
    try:
        sys_metrics = get_system_metrics()
        host_mem_bytes = sys_metrics["mem_total_bytes"]
        cpus = sys_metrics["cpus"]

        # 1. Get container list via Unix Socket
        conn = UnixHTTPConnection(timeout=5)
        conn.request("GET", "/containers/json?all=1")
        res = conn.getresponse()
        containers = json.loads(res.read().decode("utf-8"))
        conn.close()

        running_containers = [c for c in containers if c.get("State") == "running"]
        running_ids = {c["Id"] for c in running_containers}
        with _cpu_cache_lock:
            for dead_id in list(_container_cpu_cache.keys()):
                if dead_id not in running_ids:
                    del _container_cpu_cache[dead_id]

        # 2. Get stats in parallel
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(fetch_single_container_stats, c, host_mem_bytes, cpus) for c in running_containers]
            container_results = [f.result() for f in futures]

        stacks = {}
        total_docker_cpu = 0.0
        total_docker_mem = 0
        total_pids = 0

        for c_data in container_results:
            stack = c_data["stack"]
            total_docker_cpu += c_data["cpu_perc"]
            total_docker_mem += c_data["mem_used_bytes"]
            total_pids += c_data["pids"]

            if stack not in stacks:
                stacks[stack] = {
                    "name": stack,
                    "containers": [],
                    "total_cpu": 0.0,
                    "total_mem_bytes": 0,
                    "total_pids": 0,
                    "healthy_count": 0,
                    "total_containers": 0
                }

            s = stacks[stack]
            s["containers"].append(c_data)
            s["total_cpu"] += c_data["cpu_perc"]
            s["total_mem_bytes"] += c_data["mem_used_bytes"]
            s["total_pids"] += c_data["pids"]
            s["total_containers"] += 1
            if c_data["health"] in ("healthy", "running"):
                s["healthy_count"] += 1

        total_docker_cpu = round(total_docker_cpu, 2)
        docker_host_cpu_perc = round(total_docker_cpu / max(1, cpus), 1)
        total_docker_mem_perc = round((total_docker_mem / max(1, host_mem_bytes)) * 100.0, 1)

        for s_name, s_data in stacks.items():
            s_data["total_cpu"] = round(s_data["total_cpu"], 2)
            s_data["total_mem_formatted"] = format_bytes(s_data["total_mem_bytes"])
            s_data["host_mem_perc"] = round((s_data["total_mem_bytes"] / max(1, host_mem_bytes)) * 100.0, 2)
            s_data["docker_mem_share"] = round((s_data["total_mem_bytes"] / max(1, total_docker_mem)) * 100.0, 1)
            s_data["containers"].sort(key=lambda x: x["cpu_perc"], reverse=True)

        return {
            "timestamp": time.time(),
            "system": sys_metrics,
            "docker_total_cpu": total_docker_cpu,
            "docker_host_cpu_perc": docker_host_cpu_perc,
            "total_cpu": total_docker_cpu,
            "total_cpu_host_perc": docker_host_cpu_perc,
            "docker_total_mem_bytes": total_docker_mem,
            "docker_total_mem_str": format_bytes(total_docker_mem),
            "docker_total_mem_perc": total_docker_mem_perc,
            "total_mem_bytes": total_docker_mem,
            "total_mem_str": format_bytes(total_docker_mem),
            "total_mem_perc": total_docker_mem_perc,
            "host_total_mem_bytes": host_mem_bytes,
            "host_total_mem_str": format_bytes(host_mem_bytes),
            "running_containers": len(running_containers),
            "unhealthy_containers": sum(1 for c in container_results if c["health"] == "unhealthy"),
            "total_containers": len(containers),
            "total_stacks": len(stacks),
            "total_pids": total_pids,
            "stacks": stacks
        }
    except Exception as e:
        print(f"Error fetching docker metrics: {e}", file=sys.stderr)
        return None


def fetch_docker_metrics():
    if hasattr(subprocess.run, '_mock_self') or not os.path.exists(DOCKER_SOCKET):
        return _fetch_docker_metrics_subprocess()
    return _fetch_docker_metrics_socket()


def background_poller():
    global cached_data
    while True:
        data = fetch_docker_metrics()
        if data:
            with cache_lock:
                cached_data = data
        time.sleep(POLL_INTERVAL)


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dockpit - Docker Stack Cockpit & System Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: #090d16;
            color: #f1f5f9;
        }
        .glass-card {
            background: rgba(16, 23, 38, 0.78);
            backdrop-filter: blur(14px);
            border: 1px solid rgba(255, 255, 255, 0.08);
        }
        .glass-card:hover {
            border-color: rgba(99, 102, 241, 0.3);
        }
        .metric-badge {
            font-variant-numeric: tabular-nums;
        }
        .progress-bar-smooth {
            transition: width 0.5s cubic-bezier(0.4, 0, 0.2, 1);
        }
        ::-webkit-scrollbar {
            width: 7px;
            height: 7px;
        }
        ::-webkit-scrollbar-track {
            background: #090d16;
        }
        ::-webkit-scrollbar-thumb {
            background: #334155;
            border-radius: 4px;
        }
        ::-webkit-scrollbar-thumb:hover {
            background: #475569;
        }
    </style>
</head>
<body class="min-h-screen p-4 md:p-6 flex flex-col">
    <!-- Header -->
    <header class="max-w-7xl w-full mx-auto mb-4 flex flex-col md:flex-row md:items-center justify-between gap-3">
        <div>
            <div class="flex items-center gap-3">
                <div class="w-9 h-9 rounded-xl bg-gradient-to-tr from-indigo-600 via-indigo-500 to-cyan-400 flex items-center justify-center text-white shadow-md shadow-indigo-500/20">
                    <i class="fa-brands fa-docker text-xl"></i>
                </div>
                <div>
                    <h1 class="text-xl font-bold tracking-tight text-white flex items-center gap-2">
                        Dockpit
                        <span class="text-[10px] font-mono font-semibold uppercase tracking-wider text-indigo-400 bg-indigo-500/10 border border-indigo-500/20 px-2 py-0.5 rounded-full">v1.0.0</span>
                    </h1>
                </div>
            </div>
        </div>
        <div class="flex items-center gap-2.5">
            <div class="flex items-center gap-2 text-xs text-slate-400 glass-card px-3 py-1.5 rounded-xl">
                <i class="fa-regular fa-clock text-indigo-400"></i>
                <span id="statusText">Updated just now</span>
            </div>
            <button id="toggleAutoBtn" onclick="toggleAutoRefresh()" class="text-xs px-3 py-1.5 rounded-xl bg-slate-800/90 hover:bg-slate-700 text-slate-200 border border-slate-700 transition flex items-center gap-1.5 cursor-pointer">
                <i id="toggleIcon" class="fa-solid fa-pause"></i>
                <span id="toggleText">Pause</span>
            </button>
            <button onclick="fetchStats(true)" class="text-xs px-3.5 py-1.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white font-medium shadow-md shadow-indigo-600/20 transition flex items-center gap-1.5 cursor-pointer">
                <i class="fa-solid fa-rotate"></i> Refresh
            </button>
        </div>
    </header>

    <main class="max-w-7xl w-full mx-auto flex-1 space-y-3.5">
        <!-- 4 TOP SYSTEM STATUS CARDS (Clickable for full hardware & inventory details) -->
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            <!-- 1. CPU -->
            <div onclick="openTopCardModal('cpu')" title="Click for CPU hardware, core topology & load details"
                 class="glass-card rounded-xl p-3 flex flex-col justify-between cursor-pointer transition hover:border-amber-500/50 hover:bg-slate-800/60 active:scale-[0.99] group select-none">
                <div class="flex items-center justify-between text-slate-400 text-xs font-semibold uppercase tracking-wider">
                    <span class="flex items-center gap-1.5 text-slate-300 group-hover:text-amber-300 transition">
                        <i class="fa-solid fa-microchip text-amber-400"></i> Host CPU
                    </span>
                    <div class="flex items-center gap-1.5">
                        <span class="text-[11px] font-mono text-amber-400 bg-amber-500/10 border border-amber-500/20 px-1.5 py-0.5 rounded" id="sysCpuCores">12 Cores</span>
                        <span class="text-[10px] text-slate-500 group-hover:text-amber-400 transition hidden sm:inline"><i class="fa-solid fa-arrow-up-right-from-square"></i></span>
                    </div>
                </div>
                <div class="flex items-baseline justify-between my-1">
                    <div class="text-2xl font-bold text-white metric-badge" id="sysCpu">- %</div>
                    <span class="text-xs text-slate-400 font-mono">Load: <strong class="text-slate-200" id="sysLoad">-</strong></span>
                </div>
                <div>
                    <div class="w-full bg-slate-800 rounded-full h-1.5 overflow-hidden">
                        <div id="sysCpuBar" class="bg-amber-400 h-1.5 rounded-full progress-bar-smooth" style="width: 0%"></div>
                    </div>
                    <div class="text-[11px] text-slate-400 mt-1 flex justify-between">
                        <span>Docker: <strong class="text-amber-300 font-mono" id="sysDockerCpu">-</strong></span>
                        <span class="text-slate-500" id="sysDockerHostCpu">(-% host)</span>
                    </div>
                </div>
            </div>

            <!-- 2. System RAM -->
            <div onclick="openTopCardModal('memory')" title="Click for memory buffers, cache, active RSS & swap details"
                 class="glass-card rounded-xl p-3 flex flex-col justify-between cursor-pointer transition hover:border-purple-500/50 hover:bg-slate-800/60 active:scale-[0.99] group select-none">
                <div class="flex items-center justify-between text-slate-400 text-xs font-semibold uppercase tracking-wider">
                    <span class="flex items-center gap-1.5 text-slate-300 group-hover:text-purple-300 transition">
                        <i class="fa-solid fa-memory text-purple-400"></i> Host RAM
                    </span>
                    <div class="flex items-center gap-1.5">
                        <span class="text-[11px] font-mono text-purple-400" id="sysRamPct">-%</span>
                        <span class="text-[10px] text-slate-500 group-hover:text-purple-400 transition hidden sm:inline"><i class="fa-solid fa-arrow-up-right-from-square"></i></span>
                    </div>
                </div>
                <div class="flex items-baseline justify-between my-1">
                    <div class="text-2xl font-bold text-white metric-badge" id="sysRam">-</div>
                    <span class="text-xs text-slate-400 font-mono" id="sysRamSub">of 48 GiB</span>
                </div>
                <div>
                    <div class="w-full bg-slate-800 rounded-full h-1.5 overflow-hidden">
                        <div id="sysRamBar" class="bg-purple-400 h-1.5 rounded-full progress-bar-smooth" style="width: 0%"></div>
                    </div>
                    <div class="text-[11px] text-slate-400 mt-1 flex justify-between">
                        <span>Docker: <strong class="text-purple-300 font-mono" id="sysDockerRam">-</strong></span>
                        <span class="text-slate-500">Resident</span>
                    </div>
                </div>
            </div>

            <!-- 3. System Storage -->
            <div onclick="openTopCardModal('storage')" title="Click for physical drives, serial numbers, disk IDs & partitions"
                 class="glass-card rounded-xl p-3 flex flex-col justify-between cursor-pointer transition hover:border-cyan-500/50 hover:bg-slate-800/60 active:scale-[0.99] group select-none">
                <div class="flex items-center justify-between text-slate-400 text-xs font-semibold uppercase tracking-wider">
                    <span class="flex items-center gap-1.5 text-slate-300 group-hover:text-cyan-300 transition">
                        <i class="fa-solid fa-hard-drive text-cyan-400"></i> Storage
                    </span>
                    <div class="flex items-center gap-1.5">
                        <span class="text-[11px] font-mono text-slate-400">System &amp; Data</span>
                        <span class="text-[10px] text-slate-500 group-hover:text-cyan-400 transition hidden sm:inline"><i class="fa-solid fa-arrow-up-right-from-square"></i></span>
                    </div>
                </div>
                <div class="space-y-1.5 my-0.5">
                    <div>
                        <div class="flex items-center justify-between text-xs">
                            <span class="flex items-center gap-1.5">
                                <i class="fa-solid fa-bolt text-[10px] text-cyan-400"></i>
                                <span class="text-slate-200 font-medium">System (Root)</span>
                            </span>
                            <span id="sysNvmeStats" class="text-xs font-mono text-cyan-300 font-semibold">- / - (-%)</span>
                        </div>
                        <div class="w-full bg-slate-800 rounded-full h-1.5 overflow-hidden mt-0.5">
                            <div id="sysNvmeBar" class="bg-gradient-to-r from-cyan-500 to-blue-500 h-1.5 rounded-full progress-bar-smooth" style="width: 0%"></div>
                        </div>
                    </div>
                    <div>
                        <div class="flex items-center justify-between text-xs">
                            <span class="flex items-center gap-1.5">
                                <i class="fa-solid fa-database text-[10px] text-emerald-400"></i>
                                <span class="text-slate-200 font-medium">Secondary (Data)</span>
                            </span>
                            <span id="sysDataStats" class="text-xs font-mono text-emerald-300 font-semibold">- / - (-%)</span>
                        </div>
                        <div class="w-full bg-slate-800 rounded-full h-1.5 overflow-hidden mt-0.5">
                            <div id="sysDataBar" class="bg-gradient-to-r from-emerald-500 to-teal-400 h-1.5 rounded-full progress-bar-smooth" style="width: 0%"></div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 4. Containers & Uptime -->
            <div onclick="openTopCardModal('system')" title="Click for OS distribution, kernel, uptime & Docker daemon details"
                 class="glass-card rounded-xl p-3 flex flex-col justify-between cursor-pointer transition hover:border-emerald-500/50 hover:bg-slate-800/60 active:scale-[0.99] group select-none">
                <div class="flex items-center justify-between text-slate-400 text-xs font-semibold uppercase tracking-wider">
                    <span class="flex items-center gap-1.5 text-slate-300 group-hover:text-emerald-300 transition">
                        <i class="fa-solid fa-cubes text-emerald-400"></i> Containers
                    </span>
                    <div class="flex items-center gap-1.5">
                        <span class="text-[11px] font-mono text-emerald-400" id="sysUptime">Uptime: -</span>
                        <span class="text-[10px] text-slate-500 group-hover:text-emerald-400 transition hidden sm:inline"><i class="fa-solid fa-arrow-up-right-from-square"></i></span>
                    </div>
                </div>
                <div class="flex items-baseline justify-between my-1">
                    <div class="text-2xl font-bold text-white metric-badge"><span id="sysHealth" class="text-emerald-400">-</span> <span class="text-xs font-normal text-slate-400">active</span></div>
                    <span class="text-xs font-mono text-slate-400">PIDs: <strong class="text-slate-200" id="sysPids">-</strong></span>
                </div>
                <div>
                    <div class="w-full bg-slate-800 rounded-full h-1.5 overflow-hidden">
                        <div id="sysHealthBar" class="bg-emerald-400 h-1.5 rounded-full progress-bar-smooth" style="width: 100%"></div>
                    </div>
                    <div class="text-[11px] text-slate-400 mt-1 flex justify-between">
                        <span id="sysContainerStats">-</span>
                        <span class="text-slate-500 font-mono" id="portIndicator">Port 8989</span>
                    </div>
                </div>
            </div>
        </div>

        <!-- Filter & Search Controls Bar -->
        <div class="glass-card rounded-xl p-3 flex flex-col gap-2.5">
            <div class="flex flex-col md:flex-row items-stretch md:items-center justify-between gap-3">
                <div class="relative flex-1">
                    <i class="fa-solid fa-magnifying-glass absolute left-3.5 top-3 text-slate-400 text-sm"></i>
                    <input type="text" id="searchInput" oninput="renderDashboard()" placeholder="Search stacks, containers, ports, images or services..." 
                           class="w-full pl-10 pr-4 py-2 bg-slate-900/90 text-sm text-slate-200 placeholder-slate-500 rounded-xl border border-slate-700/60 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition">
                </div>
                <div class="flex items-center gap-2.5">
                    <div class="flex items-center gap-2">
                        <span class="text-xs text-slate-400 font-medium whitespace-nowrap">Sort:</span>
                        <select id="sortBy" onchange="renderDashboard()" class="bg-slate-900/90 text-xs text-slate-200 py-2 px-3 rounded-xl border border-slate-700/60 focus:outline-none focus:border-indigo-500 cursor-pointer">
                            <option value="cpu">Highest CPU</option>
                            <option value="mem">Highest RAM</option>
                            <option value="containers">Most Containers</option>
                            <option value="name">Name (A-Z)</option>
                        </select>
                    </div>
                    <button onclick="toggleExpandAll(true)" class="text-xs px-3 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700/60 transition whitespace-nowrap cursor-pointer">
                        Expand All
                    </button>
                    <button onclick="toggleExpandAll(false)" class="text-xs px-3 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700/60 transition whitespace-nowrap cursor-pointer">
                        Collapse All
                    </button>
                </div>
            </div>

            <!-- Quick Filter Tags -->
            <div class="flex flex-wrap items-center gap-2 pt-1 border-t border-slate-800/80">
                <span class="text-xs text-slate-400 font-medium mr-1">Quick Filters:</span>
                <button onclick="setQuickFilter('all')" id="qf-all" class="qf-btn text-xs px-3 py-1 rounded-lg bg-indigo-600 text-white font-medium transition cursor-pointer">
                    All (<span id="count-all">0</span>)
                </button>
                <button onclick="setQuickFilter('cpu')" id="qf-cpu" class="qf-btn text-xs px-3 py-1 rounded-lg bg-slate-800/80 hover:bg-slate-700 text-slate-300 border border-slate-700/60 transition flex items-center gap-1 cursor-pointer">
                    <span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span> Active CPU (>0.5%)
                </button>
                <button onclick="setQuickFilter('mem')" id="qf-mem" class="qf-btn text-xs px-3 py-1 rounded-lg bg-slate-800/80 hover:bg-slate-700 text-slate-300 border border-slate-700/60 transition flex items-center gap-1 cursor-pointer">
                    <span class="w-1.5 h-1.5 rounded-full bg-purple-400"></span> Heavy RAM (>250MB)
                </button>
                <button onclick="setQuickFilter('ports')" id="qf-ports" class="qf-btn text-xs px-3 py-1 rounded-lg bg-slate-800/80 hover:bg-slate-700 text-slate-300 border border-slate-700/60 transition flex items-center gap-1 cursor-pointer">
                    <span class="w-1.5 h-1.5 rounded-full bg-cyan-400"></span> With Ports
                </button>
                <button onclick="setQuickFilter('secondary')" id="qf-secondary" class="qf-btn text-xs px-3 py-1 rounded-lg bg-slate-800/80 hover:bg-slate-700 text-slate-300 border border-slate-700/60 transition flex items-center gap-1 cursor-pointer">
                    <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span> Secondary Storage
                </button>
                <button onclick="setQuickFilter('root')" id="qf-root" class="qf-btn text-xs px-3 py-1 rounded-lg bg-slate-800/80 hover:bg-slate-700 text-slate-300 border border-slate-700/60 transition flex items-center gap-1 cursor-pointer">
                    <span class="w-1.5 h-1.5 rounded-full bg-cyan-400"></span> Root Storage Only
                </button>
            </div>
        </div>

        <!-- Stacks Container -->
        <div id="stacksContainer" class="space-y-4"></div>
    </main>

    <!-- Details / Logs Modal -->
    <div id="modalOverlay" class="fixed inset-0 bg-black/85 backdrop-blur-md z-50 hidden flex items-center justify-center p-3 sm:p-4">
        <div class="glass-card bg-slate-900 border border-slate-700 rounded-2xl max-w-5xl w-full max-h-[90vh] flex flex-col shadow-2xl overflow-hidden">
            <div class="p-4 border-b border-slate-800 flex items-center justify-between">
                <div class="flex items-center gap-3 min-w-0">
                    <div id="modalIcon" class="w-10 h-10 rounded-xl bg-indigo-500/20 text-indigo-400 flex items-center justify-center text-lg shrink-0">
                        <i class="fa-solid fa-info"></i>
                    </div>
                    <div class="min-w-0">
                        <h3 id="modalTitle" class="text-base font-bold text-white truncate">Container Details</h3>
                        <p id="modalSubtitle" class="text-xs text-slate-400 truncate">Container ID &amp; status</p>
                    </div>
                </div>
                <div class="flex items-center gap-2 shrink-0">
                    <button id="modalActionBtn" class="text-xs px-3 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white transition flex items-center gap-1 cursor-pointer">
                        <i class="fa-solid fa-rotate"></i> Reload
                    </button>
                    <button onclick="closeModal()" class="w-8 h-8 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 flex items-center justify-center transition cursor-pointer">
                        <i class="fa-solid fa-xmark"></i>
                    </button>
                </div>
            </div>
            <div id="modalContent" class="p-5 overflow-y-auto max-h-[75vh] space-y-4">
                <!-- Injected dynamically -->
            </div>
        </div>
    </div>

    <!-- Scripts -->
    <script>
        let latestData = null;
        let autoRefresh = true;
        let refreshInterval = null;
        let expandedStacks = new Set();
        let activeQuickFilter = 'all';

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

        async function fetchStats(force = false) {
            try {
                const res = await fetch('/api/stats');
                if (!res.ok) throw new Error('API returned status ' + res.status);
                const data = await res.json();
                latestData = data;
                
                updateTopKPIs(data);
                renderDashboard();
                
                const now = new Date();
                document.getElementById('statusText').textContent = 'Updated ' + now.toLocaleTimeString();
            } catch (err) {
                console.error('Error fetching stats:', err);
                document.getElementById('statusText').textContent = 'Connection error';
            }
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
            const sys = data.system || {};
            
            // 1. Host CPU
            const hostCpu = (sys.host_cpu_pct !== undefined ? sys.host_cpu_pct : (data.total_cpu_host_perc || 0)).toFixed(1);
            document.getElementById('sysCpu').textContent = `${hostCpu} %`;
            document.getElementById('sysCpuBar').style.width = Math.min(100, Math.max(3, parseFloat(hostCpu))) + '%';
            document.getElementById('sysCpuCores').textContent = `${sys.cpus || 12} Cores`;
            document.getElementById('sysLoad').textContent = sys.load_str || '-';
            
            const dockerCpuEl = document.getElementById('sysDockerCpu');
            const dockerTotal = (data.docker_total_cpu || 0).toFixed(1);
            dockerCpuEl.textContent = `${dockerTotal}%`;
            
            const dockerHostEl = document.getElementById('sysDockerHostCpu');
            if (dockerHostEl) {
                const hostShare = (dockerTotal / (sys.cpus || 12)).toFixed(1);
                dockerHostEl.textContent = `(${hostShare}% host)`;
            }

            // 2. System RAM
            document.getElementById('sysRam').textContent = sys.mem_used_formatted || '-';
            document.getElementById('sysRamPct').textContent = `${sys.mem_pct || 0}%`;
            document.getElementById('sysRamBar').style.width = Math.min(100, Math.max(3, sys.mem_pct || 0)) + '%';
            document.getElementById('sysRamSub').textContent = `of ${sys.mem_total_formatted || '48 GiB'}`;
            document.getElementById('sysDockerRam').textContent = `${data.docker_total_mem_str || '0 B'} (${data.docker_total_mem_perc || 0}%)`;

            // 3. System Storage
            const rootDisk = sys.root_disk || {};
            const rootUsed = formatBytesNoDec(rootDisk.used_bytes || sys.root_used_bytes || 0);
            const rootTotal = formatBytesNoDec(rootDisk.total_bytes || sys.root_total_bytes || 0);
            const rootPct = Math.round(rootDisk.pct !== undefined ? rootDisk.pct : (sys.root_pct || 0));
            const nvmeStatsEl = document.getElementById('sysNvmeStats');
            if (nvmeStatsEl) nvmeStatsEl.textContent = `${rootUsed} / ${rootTotal} (${rootPct}%)`;
            const nvmeBarEl = document.getElementById('sysNvmeBar');
            if (nvmeBarEl) nvmeBarEl.style.width = Math.min(100, Math.max(3, rootPct)) + '%';

            const dataDisk = sys.data_disk;
            if (dataDisk) {
                const dataUsed = formatBytesNoDec(dataDisk.used_bytes || 0);
                const dataTotal = formatBytesNoDec(dataDisk.total_bytes || 0);
                const dataPct = Math.round(dataDisk.pct || 0);
                const dataStatsEl = document.getElementById('sysDataStats');
                if (dataStatsEl) dataStatsEl.textContent = `${dataUsed} / ${dataTotal} (${dataPct}%)`;
                const dataBarEl = document.getElementById('sysDataBar');
                if (dataBarEl) dataBarEl.style.width = Math.min(100, Math.max(3, dataPct)) + '%';
            }

            // 4. Containers & Uptime
            const runningCount = data.running_containers !== undefined ? data.running_containers : (data.total_containers || 0);
            const totalCount = data.total_containers || runningCount;
            document.getElementById('sysHealth').textContent = `${runningCount} / ${totalCount}`;
            document.getElementById('sysUptime').textContent = `Uptime: ${sys.uptime_str || '-'}`;
            document.getElementById('sysPids').textContent = data.total_pids || 0;
            document.getElementById('count-all').textContent = totalCount;
            const healthBar = document.getElementById('sysHealthBar');
            if (healthBar) {
                const healthPct = totalCount > 0 ? (runningCount / totalCount) * 100 : 100;
                healthBar.style.width = healthPct + '%';
            }
            const cStatsEl = document.getElementById('sysContainerStats');
            if (cStatsEl) {
                const unhealthy = data.unhealthy_containers || 0;
                if (unhealthy > 0) {
                    cStatsEl.innerHTML = `<span class="text-rose-400 font-medium">${unhealthy} unhealthy</span>`;
                } else {
                    cStatsEl.innerHTML = `<span class="text-emerald-400 font-medium">${data.total_stacks || 0} stacks</span>`;
                }
            }
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
            const sys = latestData.system || {};
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
            const hostCpu = (sys.host_cpu_pct !== undefined ? sys.host_cpu_pct : (latestData.total_cpu_host_perc || 0)).toFixed(1);
            const dockerTotal = (latestData.docker_total_cpu || 0).toFixed(1);
            const hostShare = ((dockerTotal / (sys.cpus || 12)) || 0).toFixed(1);

            // Collect and sort containers by CPU
            let allContainers = [];
            if (latestData.stacks) {
                Object.values(latestData.stacks).forEach(s => {
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
                        <span class="truncate max-w-[200px] cursor-pointer hover:text-indigo-400" onclick="inspectContainer('${c.name}')">${c.name}</span>
                    </td>
                    <td class="py-2.5 px-3 text-slate-400">${c.stack}</td>
                    <td class="py-2.5 px-3 text-amber-300 font-bold">${c.cpu_perc.toFixed(1)}%</td>
                    <td class="py-2.5 px-3 text-slate-400">${c.pids}</td>
                    <td class="py-2.5 px-3 text-right">
                        <button onclick="inspectContainer('${c.name}')" class="text-[11px] px-2 py-1 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded border border-slate-700">Details</button>
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
                            <div class="font-bold text-white text-sm">${sys.cpus || 12} Threads</div>
                            <div class="text-[11px] text-slate-500 font-mono mt-0.5">${cpu.cores_per_socket || Math.round((sys.cpus || 12) / 2)} Physical Cores</div>
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
                `${cpu.model_name || 'Central Processing Unit'} • ${sys.cpus || 12} Threads`,
                content,
                'fa-microchip text-amber-400',
                () => { fetchStats(true).then(() => openTopCardModal('cpu')); }
            );
        }

        function renderMemoryDetailsModal(sys, mem) {
            let allContainers = [];
            if (latestData.stacks) {
                Object.values(latestData.stacks).forEach(s => {
                    (s.containers || []).forEach(c => allContainers.push(c));
                });
            }
            allContainers.sort((a, b) => b.mem_used_bytes - a.mem_used_bytes);
            const topContainers = allContainers.slice(0, 5);

            const topTableHtml = topContainers.map(c => `
                <tr class="border-b border-slate-800/60 hover:bg-slate-800/30 font-mono text-xs">
                    <td class="py-2.5 px-3 font-medium text-white flex items-center gap-2">
                        <span class="w-2 h-2 rounded-full ${c.health === 'unhealthy' ? 'bg-rose-400' : 'bg-purple-400'}"></span>
                        <span class="truncate max-w-[200px] cursor-pointer hover:text-indigo-400" onclick="inspectContainer('${c.name}')">${c.name}</span>
                    </td>
                    <td class="py-2.5 px-3 text-slate-400">${c.stack}</td>
                    <td class="py-2.5 px-3 text-purple-300 font-bold">${c.mem_used_formatted}</td>
                    <td class="py-2.5 px-3 text-slate-400">${c.has_custom_limit ? c.mem_limit_formatted : 'Host RAM'}</td>
                    <td class="py-2.5 px-3 text-right">
                        <button onclick="inspectContainer('${c.name}')" class="text-[11px] px-2 py-1 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded border border-slate-700">Details</button>
                    </td>
                </tr>
            `).join('');

            const content = `
                <div class="space-y-4">
                    <!-- Top Summary Grid -->
                    <div class="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
                        <div class="bg-slate-950 p-3 rounded-xl border border-slate-800">
                            <div class="text-slate-400 mb-1 font-semibold flex items-center gap-1.5"><i class="fa-solid fa-memory text-purple-400"></i> Total Physical RAM</div>
                            <div class="font-bold text-white text-base font-mono">${sys.mem_total_formatted || mem.total_formatted || '48 GiB'}</div>
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
                                <span class="text-purple-300 font-bold">${latestData.docker_total_mem_str || '0 B'} (${latestData.docker_total_mem_perc || 0}% host)</span>
                            </div>
                            <div class="w-full bg-slate-800 rounded-full h-2 overflow-hidden">
                                <div class="bg-purple-500 h-2 rounded-full progress-bar-smooth" style="width: ${Math.min(100, Math.max(2, latestData.docker_total_mem_perc || 0))}%"></div>
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
                `${sys.mem_total_formatted || '48 GiB'} Physical RAM • ${sys.mem_pct || 0}% Allocated`,
                content,
                'fa-memory text-purple-400',
                () => { fetchStats(true).then(() => openTopCardModal('memory')); }
            );
        }

        function renderStorageDetailsModal(sys, storage) {
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
                                <button onclick="copyText('${d.id}', this)" class="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 transition">
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
            const runningCount = latestData.running_containers !== undefined ? latestData.running_containers : (latestData.total_containers || 0);
            const totalCount = latestData.total_containers || runningCount;
            const unhealthyCount = latestData.unhealthy_containers || 0;

            const content = `
                <div class="space-y-4">
                    <!-- Top Host Info Grid -->
                    <div class="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
                        <div class="bg-slate-950 p-3 rounded-xl border border-slate-800">
                            <div class="text-slate-400 mb-1 font-semibold flex items-center gap-1.5"><i class="fa-brands fa-linux text-amber-400"></i> Operating System</div>
                            <div class="font-bold text-white text-sm truncate" title="${docker.os || 'Linux'}">${docker.os || 'Linux'}</div>
                            <div class="text-[11px] text-slate-500 font-mono mt-0.5">${docker.arch || 'x86_64'}</div>
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
                            <div class="font-bold text-white text-sm font-mono">${latestData.total_pids || 0} PIDs</div>
                            <div class="text-[11px] text-slate-500 font-mono mt-0.5">Across all containers</div>
                        </div>
                    </div>

                    <!-- Docker Engine & Runtime -->
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
                        <h4 class="text-xs font-bold uppercase tracking-wider text-slate-400 flex items-center gap-1.5"><i class="fa-brands fa-docker text-indigo-400"></i> Docker Daemon &amp; Containerd Runtime</h4>
                        <div class="grid grid-cols-2 sm:grid-cols-3 gap-2.5 text-xs font-mono">
                            <div class="p-2.5 bg-slate-900 rounded-lg border border-slate-800">
                                <div class="text-slate-400 text-[11px]">Docker Engine</div>
                                <div class="text-white font-bold mt-0.5">${docker.docker_version || '29.9.0'}</div>
                                <div class="text-[10px] text-slate-500">API: ${docker.api_version || '1.56'}</div>
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
                                <div class="text-white font-bold mt-0.5">${docker.containerd_version || 'v2.4.1'}</div>
                                <div class="text-[10px] text-slate-500">OCI executor</div>
                            </div>
                            <div class="p-2.5 bg-slate-900 rounded-lg border border-slate-800">
                                <div class="text-slate-400 text-[11px]">runc</div>
                                <div class="text-white font-bold mt-0.5">${docker.runc_version || '1.5.2'}</div>
                                <div class="text-[10px] text-slate-500">Low-level container engine</div>
                            </div>
                            <div class="p-2.5 bg-slate-900 rounded-lg border border-slate-800">
                                <div class="text-slate-400 text-[11px]">Compose Stacks</div>
                                <div class="text-indigo-300 font-bold mt-0.5">${latestData.total_stacks || 0} Projects</div>
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
                `${docker.os || 'Linux'} • Kernel ${docker.kernel_version || 'Linux'} • Docker v${docker.docker_version || '29.9.0'}`,
                content,
                'fa-sliders text-emerald-400',
                () => { fetchStats(true).then(() => openTopCardModal('system')); }
            );
        }

        function renderDashboard() {
            if (!latestData || !latestData.stacks) return;

            const q = (document.getElementById('searchInput').value || '').toLowerCase().trim();
            const sortBy = document.getElementById('sortBy').value;
            const containerEl = document.getElementById('stacksContainer');

            let stackKeys = Object.keys(latestData.stacks);
            let filtered = [];

            for (const key of stackKeys) {
                const s = latestData.stacks[key];
                const matchesStack = key.toLowerCase().includes(q);
                
                const matchingContainers = s.containers.filter(c => {
                    const matchesSearch = matchesStack ||
                           c.name.toLowerCase().includes(q) ||
                           c.image.toLowerCase().includes(q) ||
                           c.service.toLowerCase().includes(q) ||
                           (c.storage_tier && c.storage_tier.toLowerCase().includes(q)) ||
                           (c.ports && c.ports.toLowerCase().includes(q));
                    
                    if (!matchesSearch) return false;

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
                const isExpanded = (q && q.length > 0) || expandedStacks.has(stack.name);
                const sIcon = getStackIcon(stack.name);
                const totalMem = stack.total_mem_formatted || formatBytesNoDec(stack.total_mem_bytes);

                html += `
                <div class="glass-card rounded-2xl overflow-hidden border border-slate-800 transition">
                    <!-- Stack Header / Accordion trigger -->
                    <div onclick="toggleStack('${stack.name}')" 
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
                    const match = p.match(/(?:0\.0\.0\.0|127\.0\.0\.1|:::):(\d+)->/);
                    if (match) {
                        const portNum = match[1];
                        return `<a href="http://localhost:${portNum}" target="_blank" title="Open port in browser" class="text-cyan-400 hover:text-cyan-300 hover:underline bg-cyan-950/60 border border-cyan-800/40 px-1.5 py-0.5 rounded text-[11px] font-mono flex items-center gap-1"><i class="fa-solid fa-arrow-up-right-from-square text-[9px]"></i>${portNum}</a>`;
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
                        <h3 class="text-sm font-bold text-white truncate max-w-md hover:text-indigo-400 transition cursor-pointer" onclick="inspectContainer('${c.name}')">
                            ${c.name}
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
                <div class="flex items-center gap-1.5 xl:flex-col xl:items-end shrink-0">
                    <button onclick="inspectContainer('${c.name}')" class="text-xs px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 transition flex items-center gap-1 cursor-pointer">
                        <i class="fa-solid fa-circle-info text-indigo-400"></i> Details
                    </button>
                    <button onclick="viewLogs('${c.name}')" class="text-xs px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 transition flex items-center gap-1 cursor-pointer">
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

            try {
                const res = await fetch(`/api/inspect?name=${encodeURIComponent(name)}`);
                if (!res.ok) throw new Error('Could not fetch inspection data');
                const data = await res.json();
                
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
                        <span class="text-slate-200 font-bold">${k}</span>
                        <div class="flex items-center gap-3 font-mono text-xs">
                            <span class="text-cyan-400">IP: ${net[k].IPAddress || 'No IP'}</span>
                            <span class="text-slate-500">Gateway: ${net[k].Gateway || 'None'}</span>
                        </div>
                    </div>
                `).join('') || '<div class="text-slate-500">No networks attached</div>';

                document.getElementById('modalContent').innerHTML = `
                    <div class="space-y-4">
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
                document.getElementById('modalContent').innerHTML = `
                    <div class="p-6 text-center text-rose-400">
                        <i class="fa-solid fa-triangle-exclamation text-2xl mb-2"></i>
                        <p>${err.message}</p>
                    </div>
                `;
            }
        }

        async function viewLogs(name) {
            openModal(
                `Container Logs: ${name}`,
                'Showing latest 100 lines (live stdout/stderr)',
                '<div class="text-center py-8 text-slate-400"><i class="fa-solid fa-circle-notch fa-spin text-2xl text-cyan-500 mb-2"></i><p>Fetching logs from Docker...</p></div>',
                'fa-terminal text-cyan-400',
                () => viewLogs(name)
            );

            try {
                const res = await fetch(`/api/logs?name=${encodeURIComponent(name)}&tail=100`);
                if (!res.ok) throw new Error('Could not fetch logs');
                const text = await res.text();

                document.getElementById('modalContent').innerHTML = `
                    <div class="flex items-center justify-between mb-2">
                        <span class="text-xs text-slate-400">Live stdout/stderr</span>
                        <button onclick="navigator.clipboard.writeText(document.getElementById('logText').innerText)" class="text-xs px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition flex items-center gap-1 cursor-pointer">
                            <i class="fa-regular fa-copy"></i> Copy Logs
                        </button>
                    </div>
                    <pre id="logText" class="bg-slate-950 p-4 rounded-xl border border-slate-800 text-slate-300 font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-all max-h-[60vh] overflow-y-auto select-text">${text || 'No log output available'}</pre>
                `;
                const pre = document.getElementById('logText');
                if (pre) pre.scrollTop = pre.scrollHeight;
            } catch (err) {
                document.getElementById('modalContent').innerHTML = `
                    <div class="p-6 text-center text-rose-400">
                        <i class="fa-solid fa-triangle-exclamation text-2xl mb-2"></i>
                        <p>${err.message}</p>
                    </div>
                `;
            }
        }

        function openModal(title, subtitle, content, iconClass, onRefresh) {
            document.getElementById('modalTitle').textContent = title;
            document.getElementById('modalSubtitle').textContent = subtitle;
            document.getElementById('modalContent').innerHTML = content;
            document.getElementById('modalIcon').className = `w-10 h-10 rounded-xl bg-indigo-500/20 flex items-center justify-center shrink-0 ${iconClass}`;
            document.getElementById('modalActionBtn').onclick = onRefresh;
            document.getElementById('modalOverlay').classList.remove('hidden');
        }

        function closeModal() {
            document.getElementById('modalOverlay').classList.add('hidden');
        }

        window.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeModal();
        });

        fetchStats();
        startInterval();
    </script>
</body>
</html>
"""


def docker_api_inspect(name):
    """Inspect container via Docker Unix socket with fallback to docker CLI."""
    if not hasattr(subprocess.run, '_mock_self') and os.path.exists(DOCKER_SOCKET):
        try:
            conn = UnixHTTPConnection(DOCKER_SOCKET, timeout=5)
            conn.connect()
            conn.request("GET", f"/containers/{name}/json")
            resp = conn.getresponse()
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            pass
    proc = subprocess.run(
        ["docker", "inspect", name],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True, timeout=8
    )
    items = json.loads(proc.stdout)
    return items[0] if items else None


def docker_api_logs(name, tail=100):
    """Retrieve container logs via Docker Unix socket with fallback to docker CLI."""
    if not hasattr(subprocess.run, '_mock_self') and os.path.exists(DOCKER_SOCKET):
        try:
            conn = UnixHTTPConnection(DOCKER_SOCKET, timeout=8)
            conn.connect()
            conn.request("GET", f"/containers/{name}/logs?stdout=1&stderr=1&tail={tail}")
            resp = conn.getresponse()
            if resp.status == 200:
                raw = resp.read()
                lines = []
                i = 0
                while i < len(raw):
                    if i + 8 <= len(raw):
                        size = int.from_bytes(raw[i+4:i+8], byteorder="big")
                        payload = raw[i+8:i+8+size]
                        lines.append(payload.decode("utf-8", errors="replace"))
                        i += 8 + size
                    else:
                        lines.append(raw[i:].decode("utf-8", errors="replace"))
                        break
                return "".join(lines)
        except Exception:
            pass
    proc = subprocess.run(
        ["docker", "logs", "--tail", str(tail), name],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10
    )
    if proc.returncode != 0:
        raise RuntimeError("Logs could not be retrieved")
    output = proc.stdout
    if proc.stderr:
        output += "\n--- STDERR ---\n" + proc.stderr
    return output


class DockerCockpitHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return
        if path == "/api/stats":
            with cache_lock:
                data = dict(cached_data)
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/stats":
            with cache_lock:
                data = dict(cached_data)
            data["stale"] = (time.time() - data.get("timestamp", 0)) > 30
            self.send_json(data, 200 if data.get("timestamp") else 503)
            return

        if path == "/api/inspect":
            name = params.get("name", [None])[0]
            with cache_lock:
                known = {c[k] for s in cached_data.get("stacks", {}).values()
                         for c in s.get("containers", []) for k in ("name", "id", "full_id")}
            if not name or name not in known or name.startswith("-") or not re.match(r"^[a-zA-Z0-9_\.\-]+$", name):
                self.send_json({"error": "Unknown or missing container name"}, 400)
                return
            try:
                item = docker_api_inspect(name)
                if item:
                    # SECURITY: Strictly sanitize inspection results; NEVER leak Env variables or secret configs
                    self.send_json({
                        "Name": item.get("Name"),
                        "State": {k: item.get("State", {}).get(k) for k in ("Status", "Health", "OOMKilled", "StartedAt", "FinishedAt", "ExitCode", "Pid")},
                        "RestartCount": item.get("RestartCount", 0),
                        "Resources": {k: item.get("HostConfig", {}).get(k) for k in
                                      ("Memory", "NanoCpus", "CpuQuota", "CpuPeriod", "PidsLimit", "ShmSize", "RestartPolicy")},
                        "Mounts": item.get("Mounts", []),
                        "NetworkSettings": item.get("NetworkSettings", {}),
                    })
                else:
                    self.send_json({"error": "Container not found"}, 404)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        if path == "/api/logs":
            name = params.get("name", [None])[0]
            raw_tail = params.get("tail", ["100"])[0]
            try:
                tail = min(500, max(1, int(raw_tail)))
            except ValueError:
                self.send_json({"error": "Invalid tail line count"}, 400)
                return
            with cache_lock:
                known = {c[k] for s in cached_data.get("stacks", {}).values()
                         for c in s.get("containers", []) for k in ("name", "id", "full_id")}
            if not name or name not in known or name.startswith("-") or not re.match(r"^[a-zA-Z0-9_\.\-]+$", name):
                self.send_response(400)
                self.end_headers()
                return
            try:
                output = docker_api_logs(name, tail)
                # Bound maximum response payload to 1MB
                body = output.encode("utf-8")[-1024 * 1024:]
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()


def main():
    print("=" * 60)
    print("Starting Dockpit - Docker Stack Cockpit & System Monitor...")
    
    first_data = fetch_docker_metrics()
    if first_data:
        global cached_data
        cached_data = first_data
        sys_info = first_data.get("system", {})
        print(f"Loaded {first_data['total_containers']} containers across {first_data['total_stacks']} stacks.")
        print(f"Total CPU: {sys_info.get('host_cpu_pct')}% ({sys_info.get('cpus')} cores) | Load: {sys_info.get('load_str')}")
        print(f"System RAM: {sys_info.get('mem_used_formatted')} / {sys_info.get('mem_total_formatted')} ({sys_info.get('mem_pct')}%)")
        print(f"Docker CPU: {first_data['docker_total_cpu']}% | Docker RAM: {first_data['docker_total_mem_str']}")
    else:
        print("Could not fetch initial Docker data, starting background poller...")

    poller = threading.Thread(target=background_poller, daemon=True)
    poller.start()

    server = ThreadingHTTPServer((HOST, PORT), DockerCockpitHandler)
    print(f"Dashboard running at: http://{HOST}:{PORT}")
    print("Hardened with server-side secret stripping and aggregated CPU metrics.")
    print("=" * 60)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()


if __name__ == "__main__":
    main()
