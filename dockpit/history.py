"""Fixed-memory host history; independent of Docker collection and HTTP clients."""
import math
from collections import deque
from pathlib import Path


class HostSampler:
    def __init__(self):
        self.previous = None

    def read(self, proc_dir, timestamp):
        cpu = None
        try:
            with (Path(proc_dir) / 'stat').open() as source:
                fields = [int(value) for value in source.readline().split()[1:9]]
            total, idle = sum(fields), fields[3] + fields[4]
            if self.previous:
                elapsed = total - self.previous[0]
                if elapsed > 0:
                    cpu = max(0, min(100, 100 * (1 - (idle - self.previous[1]) / elapsed)))
            self.previous = total, idle
        except (OSError, ValueError, IndexError):
            self.previous = None
        memory = {}
        try:
            with (Path(proc_dir) / 'meminfo').open() as source:
                for line in source:
                    if line.startswith(('MemTotal:', 'MemAvailable:')):
                        key, value, *_ = line.split()
                        memory[key.rstrip(':')] = int(value) * 1024
        except (OSError, ValueError):
            pass
        total, available = memory.get('MemTotal', 0), memory.get('MemAvailable')
        pct = max(0, min(100, (total - available) / total * 100)) if total and available is not None else None
        return {'timestamp': timestamp, 'host_cpu': cpu, 'host_memory': pct,
                'mem_total_bytes': total, 'mem_available_bytes': available}


class TieredHistory:
    """300 one-second points and 330 ten-second aggregates; missing values stay missing."""
    capacity = 630

    def __init__(self):
        self.recent = deque(maxlen=300)
        self.coarse = deque(maxlen=330)

    def append(self, sample):
        point = {key: sample.get(key) for key in ('timestamp', 'host_cpu', 'host_memory')}
        timestamp = point['timestamp']
        if self.recent and timestamp <= self.recent[-1]['timestamp']:
            return
        while self.recent and (self.recent[0]['timestamp'] <= timestamp - 300 or len(self.recent) == 300):
            old = self.recent.popleft()
            bucket = math.floor(old['timestamp'] / 10) * 10
            if not self.coarse or self.coarse[-1]['timestamp'] != bucket:
                self.coarse.append({'timestamp': bucket, 'sums': [0., 0.], 'counts': [0, 0]})
            aggregate = self.coarse[-1]
            for i, key in enumerate(('host_cpu', 'host_memory')):
                if old[key] is not None:
                    aggregate['sums'][i] += old[key]
                    aggregate['counts'][i] += 1
        self.recent.append(point)
        while self.coarse and self.coarse[0]['timestamp'] < timestamp - 3600:
            self.coarse.popleft()

    def points(self, now, since=0):
        result = []
        for item in self.coarse:
            if max(now - 3600, since) < item['timestamp'] <= now - 300:
                result.append({'timestamp': item['timestamp'], 'counts': list(item['counts']), **{
                    key: item['sums'][i] / item['counts'][i] if item['counts'][i] else None
                    for i, key in enumerate(('host_cpu', 'host_memory'))}})
        result.extend(dict(point) for point in self.recent if point['timestamp'] > max(now - 3600, since))
        return result

    def __len__(self):
        return len(self.recent) + len(self.coarse)

    def extend(self, samples):
        for sample in samples:
            self.append(sample)

    def clear(self):
        self.recent.clear()
        self.coarse.clear()
