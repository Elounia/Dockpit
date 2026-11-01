"""Optional, read-only GPU telemetry. Missing counters remain None."""
import csv
import math
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path


def number(value, scale=1):
    try:
        result = float(value) * scale
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def read(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def nvidia(args):
    return subprocess.run(["nvidia-smi", *args], capture_output=True, text=True,
                          check=True, timeout=4).stdout


@lru_cache(maxsize=32)
def pci_device_name(root_dir, vendor_id, device_id):
    """Resolve hardware names using the host's PCI database, without lspci."""
    vendor_id, device_id = vendor_id.removeprefix("0x"), device_id.removeprefix("0x")
    if not re.fullmatch(r"[0-9a-f]{4}", vendor_id) or not re.fullmatch(r"[0-9a-f]{4}", device_id):
        return ""
    for location in ("usr/share/hwdata/pci.ids", "usr/share/misc/pci.ids"):
        try:
            with (Path(root_dir) / location).open() as database:
                matched_vendor = False
                for line in database:
                    if line.startswith("#") or not line.strip():
                        continue
                    if not line.startswith("\t"):
                        if matched_vendor:
                            break
                        matched_vendor = line.startswith(vendor_id + " ")
                    elif matched_vendor and line.startswith("\t" + device_id + " "):
                        return line[5:].strip()[:200]
        except OSError:
            continue
    return ""


def drm_details(card, vendor, name, memory_total):
    """Only expose available counters; integrated RAM is not dedicated VRAM."""
    shared = vendor == "Intel" and any(label in name.lower() for label in (
        "uhd graphics", "hd graphics", "iris xe graphics", "iris plus graphics",
        "iris pro graphics", "iris graphics"))
    details = {"memory_kind": "shared" if shared else "dedicated" if memory_total is not None else "unknown"}
    if vendor == "Intel":
        # These are clock readings, never a substitute for GPU utilization.
        details.update(frequency_mhz=number(read(card / "gt_act_freq_mhz")),
                       max_frequency_mhz=number(read(card / "gt_max_freq_mhz")))
        if details["frequency_mhz"] is None:
            frequencies = [number(read(path)) for path in sorted(card.glob("gt/gt*/rps_act_freq_mhz"))]
            details["frequency_mhz"] = max((f for f in frequencies if f is not None), default=None)
        if details["max_frequency_mhz"] is None:
            frequencies = [number(read(path)) for path in sorted(card.glob("gt/gt*/rps_max_freq_mhz"))]
            details["max_frequency_mhz"] = max((f for f in frequencies if f is not None), default=None)
        details["utilization_note"] = "Load monitoring is not configured for this Intel GPU. Clock frequency does not measure GPU load."
        if shared:
            details["memory_note"] = "Uses system RAM dynamically; there is no separate dedicated VRAM pool. Shared GPU memory usage is not exposed by this interface."
    return details


def get_gpu_metrics(root_dir):
    devices = []
    if shutil.which("nvidia-smi"):
        try:
            output = nvidia(["--query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                             "--format=csv,noheader,nounits"])
            for row in csv.reader(output.splitlines(), skipinitialspace=True):
                if len(row) != 8:
                    continue
                index, uuid, name, busy, used, total, temp, power = row
                devices.append(dict(id=uuid, index=index, name=name, vendor="NVIDIA",
                                    utilization_pct=number(busy), memory_used_bytes=number(used, 1024**2),
                                    memory_total_bytes=number(total, 1024**2), temperature_c=number(temp),
                                    power_w=number(power)))
        except (OSError, subprocess.SubprocessError):
            pass

    # Linux DRM exposes AMD busy/VRAM counters and Intel inventory/clocks.
    nvidia_reported = any(d["vendor"] == "NVIDIA" for d in devices)
    drm = Path(root_dir) / "sys/class/drm"
    for card in sorted(drm.glob("card[0-9]*")):
        if not re.fullmatch(r"card\d+", card.name):
            continue
        device = card / "device"
        vendor = {"0x1002": "AMD", "0x8086": "Intel", "0x10de": "NVIDIA"}.get(read(device / "vendor"))
        if not vendor or (vendor == "NVIDIA" and nvidia_reported):
            continue
        pci_name = pci_device_name(str(root_dir), read(device / "vendor"), read(device / "device"))
        name = read(device / "product_name") or (f"{vendor} {pci_name}" if pci_name else f"{vendor} {read(device / 'device') or card.name}")
        temperatures = sorted(device.glob("hwmon/hwmon*/temp1_input"))
        memory_total = number(read(device / "mem_info_vram_total"))
        devices.append(dict(id=card.name, index=card.name, name=name, vendor=vendor,
                            utilization_pct=number(read(device / "gpu_busy_percent")),
                            memory_used_bytes=number(read(device / "mem_info_vram_used")),
                            memory_total_bytes=memory_total,
                            temperature_c=number(read(temperatures[0]), 0.001) if temperatures else None,
                            power_w=None,
                            **drm_details(card, vendor, name, memory_total)))
    values = [d["utilization_pct"] for d in devices]
    used = [d["memory_used_bytes"] for d in devices]
    total = [d["memory_total_bytes"] for d in devices]
    return {"devices": devices,
            "utilization_pct": round(sum(values) / len(values), 1) if values and all(v is not None for v in values) else None,
            "memory_used_bytes": sum(used) if used and all(v is not None for v in used) else None,
            "memory_total_bytes": sum(total) if total and all(v is not None for v in total) else None}


def attach_container_gpu(rows, gpu, proc_dir):
    """Map NVIDIA process counters to Docker cgroups, for either Docker backend.

    GPU percent is summed process SM activity (100% = one GPU). It can exceed
    100% across GPUs. Never copy whole-device utilization into container stats.
    """
    for row in rows:
        row.update(gpu_perc=None, gpu_memory_bytes=None, gpu_stats_available=False)
    if not rows or not any(d["vendor"] == "NVIDIA" for d in gpu.get("devices", [])):
        return
    try:
        output = nvidia(["pmon", "-s", "um", "-c", "1"])
    except (OSError, subprocess.SubprocessError):
        return
    header = None
    samples = {}
    complete = {"sm": True, "fb": True}
    seen = False
    for line in output.splitlines():
        fields = line.split()
        if line.lstrip().startswith("#"):
            columns = line.lstrip().lstrip("#").split()
            if "gpu" in columns and "pid" in columns:
                header = columns
                for key in complete:
                    complete[key] = complete[key] and key in columns
            continue
        if not header or len(fields) < len(header):
            continue
        sample = dict(zip(header, fields))
        if not sample.get("gpu", "").isdigit():
            continue
        seen = True
        pid = sample.get("pid", "")
        if pid == "-":
            continue  # Explicitly idle GPU.
        if not pid.isdigit():
            complete = dict(sm=False, fb=False)
            continue
        cgroup = read(Path(proc_dir) / pid / "cgroup")
        if not cgroup:
            complete = dict(sm=False, fb=False)
            continue
        ids = re.findall(r"(?<![a-f0-9])[a-f0-9]{64}(?![a-f0-9])", cgroup)
        matches = [r for r in rows if r["state"] == "running" and any(cid.startswith(r["full_id"]) for cid in ids)]
        for key in complete:
            value = number(sample.get(key), 1024**2 if key == "fb" else 1)
            if value is None:
                complete[key] = False
            for row in matches:
                samples.setdefault(row["full_id"], {"sm": [], "fb": []})[key].append(value)
    if not seen:
        return
    for row in rows:
        if row["state"] != "running":
            continue
        values = samples.get(row["full_id"], {})
        for key, target in (("sm", "gpu_perc"), ("fb", "gpu_memory_bytes")):
            measured = values.get(key, [])
            if measured and all(v is not None for v in measured):
                row[target] = round(sum(measured), 2)
            elif not measured and complete[key]:
                row[target] = 0
        row["gpu_stats_available"] = row["gpu_perc"] is not None or row["gpu_memory_bytes"] is not None
