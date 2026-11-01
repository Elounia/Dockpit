"""Capture the real dashboard in Chromium using synthetic, non-private data.

Requires Playwright and its Chromium browser. Run from any directory with:
    python3 scripts/capture-screenshots.py
No Docker connection or production data is used.
"""
import math
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dockpit.app as app

GIB = 1024**3
MIB = 1024**2


def sample_data():
    system = {
        'cpus': 16, 'host_cpu_pct': 28.4, 'mem_pct': 42.5,
        'mem_total_bytes': 32 * GIB, 'mem_used_bytes': int(13.6 * GIB),
        'mem_total_formatted': '32.0 GB', 'mem_used_formatted': '13.6 GB',
        'uptime_str': '12d 8h 34m',
        'availability': {'cpu': True, 'memory': True, 'storage': True},
        'root_disk': {'total_bytes': GIB * 1000, 'used_bytes': GIB * 284,
                      'free_bytes': GIB * 716, 'pct': 28.4},
        'data_disk': None,
        'gpu': {'utilization_pct': 38.2, 'memory_used_bytes': GIB * 4,
                'memory_total_bytes': GIB * 12,
                'devices': [{'id': 'GPU-demo', 'name': 'NVIDIA RTX 3060',
                             'vendor': 'NVIDIA', 'utilization_pct': 38.2,
                             'memory_used_bytes': GIB * 4,
                             'memory_total_bytes': GIB * 12}]},
    }
    # stack, container, image, CPU (Docker percent), memory (MiB), health
    specs = [
        ('AI Services', 'ollama', 'ollama/ollama:latest', 142.6, 4096, 'healthy'),
        ('AI Services', 'open-webui', 'ghcr.io/open-webui/open-webui:main', 12.8, 684, 'healthy'),
        ('Media', 'jellyfin', 'jellyfin/jellyfin:latest', 64.2, 1248, 'healthy'),
        ('Media', 'sonarr', 'lscr.io/linuxserver/sonarr:latest', 3.4, 312, 'healthy'),
        ('Media', 'radarr', 'lscr.io/linuxserver/radarr:latest', 2.1, 268, 'healthy'),
        ('Databases', 'postgres', 'postgres:17-alpine', 18.3, 1536, 'healthy'),
        ('Databases', 'redis', 'redis:7-alpine', 4.2, 128, 'healthy'),
        ('Monitoring', 'prometheus', 'prom/prometheus:latest', 9.6, 512, 'healthy'),
        ('Monitoring', 'grafana', 'grafana/grafana:latest', 2.8, 224, 'healthy'),
        ('Proxy & Security', 'traefik', 'traefik:v3', 2.4, 96, 'healthy'),
        ('Proxy & Security', 'authelia', 'authelia/authelia:latest', 1.2, 84, 'healthy'),
        ('Backups', 'restic-backup', 'restic/restic:latest', 0, 0, 'exited'),
    ]
    rows = []
    for stack, name, image, cpu, memory, health in specs:
        state = 'exited' if health == 'exited' else 'running'
        row = app.container_metadata({
            'Id': name.ljust(64, '0'), 'Names': ['/' + name], 'Image': image,
            'State': state,
            'Status': 'Exited (0) 2 hours ago' if state == 'exited' else 'Up 12 days (healthy)',
            'Labels': {'dockpit.stack': stack},
        })
        row.update(cpu_perc=cpu, mem_used_bytes=memory * MIB,
                   mem_used_formatted=app.format_bytes(memory * MIB),
                   mem_limit_bytes=8 * GIB, mem_limit_formatted='8.0 GB',
                   mem_perc=memory / 8192 * 100, has_custom_limit=True,
                   stats_available=state == 'running', pids=16 if memory else 0,
                   net_rx='1.2 GB', net_tx='384.0 MB', block_read='64.0 MB',
                   block_write='128.0 MB')
        rows.append(row)
    with patch.object(app, 'attach_container_gpu'):
        return app.aggregate_snapshot(rows, system, 'socket')


def main():
    app.cached_data = sample_data()
    now = time.time()
    app.history.extend([
        {'timestamp': now - 300 + i, 'host_cpu': 28.4 + 8 * math.sin(i / 23) + 3 * math.sin(i / 7),
         'host_memory': 42.5 + 1.3 * math.sin(i / 80)}
        for i in range(301)
    ])
    app.poll_status.update(last_success=now, last_error=None, backend='socket')
    server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.DockerCockpitHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
            page = browser.new_page(viewport={'width': 1440, 'height': 1000},
                                    device_scale_factor=2, reduced_motion='reduce')
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
            page.evaluate('document.fonts.ready')
            page.wait_for_function("document.getElementById('sysCpu').textContent === '28.4%'")
            page.wait_for_timeout(500)
            assert page.locator('.stack-row').count() == 6
            assert page.locator('#dataWarning').is_hidden()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(ROOT / 'assets/screenshot-overview.png'), full_page=True)
            page.get_by_role('button', name='Expand all stacks', exact=True).click()
            assert page.locator('.container-row').count() == 12
            page.mouse.move(0, 0)
            page.wait_for_timeout(300)
            page.screenshot(path=str(ROOT / 'assets/screenshot-stacks.png'), full_page=True)
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    print('Captured two Chromium screenshots with sample data; no browser errors.')


if __name__ == '__main__':
    main()
