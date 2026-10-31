"""Browser regression checks against synthetic data, never the user's containers."""
import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dockpit.app as app
from playwright.sync_api import sync_playwright


def main():
    attack = '<img src=x onerror="window.dockpitXss=1">'
    system = app.get_system_metrics()
    system.update(host_cpu_pct=20, mem_pct=30)
    rows = []
    for name, state, status in [('broken', 'running', 'Up (unhealthy)'), ('stopped', 'exited', 'Exited (1)')]:
        c = {'Id': name.ljust(64, 'a'), 'Names': ['/' + name], 'State': state, 'Status': status, 'Image': attack, 'Labels': {'com.docker.compose.project': 'demo', 'dockpit.name': attack, 'dockpit.url': 'https://example.com/app'}}
        with patch.object(app, 'docker_json', return_value={}):
            row = app.fetch_single_container_stats(c, system['mem_total_bytes'], system['cpus'])
        row['ports'] = '0.0.0.0:8080->80/tcp'
        rows.append(row)
    app.cached_data = app.aggregate_snapshot(rows, system, 'socket')
    app.history.extend([{'timestamp': time.time()-10, 'host_cpu': 10, 'host_memory': 25}, {'timestamp': time.time(), 'host_cpu': 20, 'host_memory': 30}])
    inspect = {'Name': '/broken', 'State': {'Status': 'running', 'OOMKilled': True, 'ExitCode': 137}, 'RestartCount': 2, 'Mounts': [{'Source': attack, 'Destination': attack, 'Type': 'bind'}], 'NetworkSettings': {'Networks': {attack: {'IPAddress': '172.18.0.2'}}}}
    server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.DockerCockpitHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with patch.object(app, 'docker_api_logs', return_value=attack + '\nnormal log line'), patch.object(app, 'docker_api_inspect', return_value=inspect), sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
            page = browser.new_page(viewport={'width': 1440, 'height': 1100})
            errors, external, csp_errors = [], [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('console', lambda msg: csp_errors.append(msg.text) if 'Content Security Policy' in msg.text or 'Refused to' in msg.text else None)
            page.on('request', lambda request: external.append(request.url) if not request.url.startswith('http://127.0.0.1:') else None)
            page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
            page.get_by_text('Expand All', exact=True).click()
            assert page.locator('#stacksContainer').inner_text().count(attack) >= 2
            assert page.locator('#stacksContainer img').count() == 0
            assert page.evaluate('window.dockpitXss') is None
            assert page.locator('#qf-issues').inner_text() == 'Needs attention (2)'
            assert page.locator('#cpuHistory polyline').count() == 1, (errors, csp_errors)
            links = page.locator('#stacksContainer a').evaluate_all('(links) => links.map(link => link.href)')
            assert 'http://127.0.0.1:8080/' in links, links
            assert 'https://example.com/app' in links
            page.locator('[data-action=inspect][data-value=broken]').first.click()
            page.wait_for_function("() => document.getElementById('modalContent').textContent.includes('OOM killed: Yes')")
            assert page.locator('#modalContent img').count() == 0
            assert attack in page.locator('#modalContent').inner_text()
            page.locator('[data-action=close]').click()
            page.locator('[data-action=logs][data-value=broken]').click()
            page.wait_for_function("() => document.getElementById('logText')?.textContent.includes('normal log line')")
            assert attack in page.locator('#logText').inner_text()
            assert page.locator('#logText img').count() == 0
            page.locator('#logSearch').fill('normal')
            assert page.locator('#logText').inner_text() == 'normal log line'
            page.locator('#logPause').click()
            assert page.locator('#logPause').inner_text() == 'Resume'
            with page.expect_download() as download:
                page.get_by_text('Download', exact=True).click()
            assert download.value.suggested_filename == 'broken.log'
            page.locator('[data-action=close]').click()
            page.get_by_text('Diagnostics', exact=True).click()
            page.wait_for_function("() => document.getElementById('diagnosticsText')?.textContent.includes('socket_exists')")
            page.locator('[data-action=close]').click()
            for card in ['cpu', 'memory', 'storage', 'system']:
                page.locator(f'[data-action=card][data-value={card}]').click()
                assert page.locator('#modalContent').inner_text()
                page.locator('[data-action=close]').click()
            page.locator('#sortBy').select_option('name')
            page.reload(wait_until='networkidle')
            assert page.locator('#sortBy').input_value() == 'name'
            assert page.locator('[data-action=logs]').count() == 2  # expansion persisted
            app.cached_data['timestamp'] = time.time() - 120
            page.locator('[data-action=refresh]').click()
            page.wait_for_function("() => document.getElementById('statusText').textContent.startsWith('Stale')")
            assert page.locator('#dataWarning').is_visible()
            page.screenshot(path='/tmp/dockpit-browser-desktop.png', full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile layout overflows'
            page.screenshot(path='/tmp/dockpit-browser-mobile.png', full_page=True)
            assert not errors, errors
            assert not external, external
            assert not csp_errors, csp_errors
            browser.close()
            print('Browser checks passed: XSS text rendering, offline assets, labels, ports, logs, details, history, diagnostics, preferences, stale warning and mobile layout.')
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    main()
