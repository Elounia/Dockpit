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
    system['gpu'] = {'utilization_pct': 42, 'memory_used_bytes': 512 * 1024**2, 'memory_total_bytes': 8 * 1024**3,
                     'devices': [{'id': 'GPU-demo', 'name': attack, 'vendor': 'NVIDIA', 'utilization_pct': 42,
                                  'memory_used_bytes': 512 * 1024**2, 'memory_total_bytes': 8 * 1024**3,
                                  'temperature_c': 55, 'power_w': 80}]}
    rows = []
    for name, state, status in [('broken', 'running', 'Up (unhealthy)'), ('stopped', 'exited', 'Exited (1)')]:
        c = {'Id': name.ljust(64, 'a'), 'Names': ['/' + name], 'State': state, 'Status': status, 'Image': attack, 'Labels': {'com.docker.compose.project': 'demo', 'dockpit.name': attack, 'dockpit.url': 'https://example.com/app'}}
        with patch.object(app, 'docker_json', return_value={}):
            row = app.fetch_single_container_stats(c, system['mem_total_bytes'], system['cpus'])
        row['ports'] = '0.0.0.0:8080->80/tcp'
        rows.append(row)
    with patch.object(app, 'attach_container_gpu'):
        rows[0].update(cpu_perc=12.5, mem_used_bytes=64 * 1024**2, pids=3)
        app.cached_data = app.aggregate_snapshot(rows, system, 'socket')
    rows[0].update(gpu_perc=25, gpu_memory_bytes=256 * 1024**2, gpu_stats_available=True)
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
            page.set_default_timeout(10000)
            assert page.locator('#issuesSummary').inner_text() == '1 needs attention'
            assert page.locator('.container-row').count() == 0
            page.locator('#issuesSummary').click()
            assert page.locator('#qf-issues').get_attribute('aria-pressed') == 'true'
            page.locator('#qf-all').click()
            assert page.locator('#qf-cpu, #qf-mem, #qf-secondary, #qf-root').count() == 0
            page.get_by_role('button', name='Expand all stacks', exact=True).click()
            page.locator('#qf-stopped').click()
            assert page.locator('.container-row').count() == 1
            assert page.locator('.container-row').get_attribute('data-value') == 'stopped'
            assert page.locator('.container-row .state-dot.inactive').count() == 1
            assert page.locator('#qf-stopped').inner_text() == 'Stopped (1)'
            page.reload(wait_until='networkidle')
            assert page.locator('#qf-stopped').get_attribute('aria-pressed') == 'true'
            page.locator('#qf-running').click()
            assert page.locator('.container-row').count() == 1
            assert page.locator('.container-row').get_attribute('data-value') == 'broken'
            page.locator('#qf-issues').click()
            assert page.locator('.container-row').count() == 1
            page.locator('#qf-all').click()
            # Stopped-only stacks stay neutral, regardless of the previous exit code.
            page.evaluate("() => { const stopped=latestData.stacks.demo.containers.find(c=>c.name==='stopped'); renderIssues({stacks:{demo:{containers:[stopped]}}}); }")
            assert page.locator('#issuesSummary').inner_text() == 'No active issues'
            assert 'attention' not in (page.locator('#issuesSummary').get_attribute('class') or '')
            page.evaluate('renderIssues(dashboardData())')
            assert page.evaluate("""() => {
                const cases = [
                    [{state:'exited',health:'exited'}, 'stopped', true],
                    [{state:'exited',health:'exited'}, 'issues', false],
                    [{state:'paused',health:'paused'}, 'paused', true],
                    [{state:'created',health:'created'}, 'starting', true],
                    [{state:'running',health:'starting'}, 'starting', true],
                    [{state:'running',health:'unhealthy'}, 'running', true],
                    [{state:'restarting',health:'restarting'}, 'issues', true],
                    [{state:'dead',health:'dead'}, 'issues', true],
                    [{ports:'80/tcp'}, 'ports', false],
                    [{ports:'127.0.0.1:8080->80/tcp'}, 'ports', true],
                ];
                return cases.every(([c,filter,expected]) => matchesQuickFilter(c,filter) === expected);
            }""")
            assert not page.locator('#qf-ports').is_visible()
            page.locator('.filters summary').click()
            page.locator('#qf-ports').click()
            assert page.locator('.filters').get_attribute('open') is None
            assert page.locator('.filters summary').inner_text() == 'Published ports'
            page.locator('#qf-all').click()
            # Retired preference values must not silently filter out containers.
            page.evaluate("() => { const prefs=JSON.parse(localStorage.getItem('dockpit.preferences')); prefs.filter='secondary'; localStorage.setItem('dockpit.preferences',JSON.stringify(prefs)); }")
            page.reload(wait_until='networkidle')
            assert page.locator('#qf-all').get_attribute('aria-pressed') == 'true'
            page.get_by_role('button', name='Collapse all stacks', exact=True).click()
            page.locator('[data-range="900"]').click()
            assert page.evaluate('historyChart.range') == 900
            page.locator('[data-range="3600"]').click()
            assert page.evaluate('historyChart.range') == 3600
            page.locator('[data-range="300"]').click()
            page.locator('#hostHistory').hover()
            assert page.locator('#historyTooltip').is_visible()
            assert 'CPU' in page.locator('#historyTooltip').inner_text()
            assert 'CPUs' in page.locator('#historyTooltip').inner_text()
            assert 'GiB' in page.locator('#historyTooltip').inner_text()
            assert page.evaluate("""() => {
                const previous = historyChart.capacity;
                historyChart.capacity = {cpu_threads:8, memory_bytes:16*1024**3};
                historyChart.showTooltip({timestamp:Date.now()/1000,host_cpu:25,host_memory:50});
                const measured = [historyChart.tooltipFields['cpu-value'].textContent, historyChart.tooltipFields['ram-value'].textContent, historyChart.tooltipFields['cpu-percent'].textContent];
                historyChart.showTooltip({timestamp:Date.now()/1000,host_cpu:null,host_memory:null});
                const missing = historyChart.tooltipFields['ram-value'].textContent;
                historyChart.capacity = previous;
                return measured.join('|') === '2.00 / 8 CPUs|8.0 / 16.0 GiB|25.0%' && missing === 'Unavailable';
            }""")
            page.mouse.move(0,0)
            page.wait_for_timeout(350)
            assert page.evaluate('historyChart.frame') == 0
            assert page.evaluate("() => { const segments=[]; historyChart.curve({moveTo(){},bezierCurveTo(...v){segments.push(v)}}, [{x:0,y:0},{x:10,y:100},{x:20,y:5}]); return segments.every((s,i) => { const a=[0,100][i], b=s[5]; for(let t=0;t<=1;t+=.01){const y=(1-t)**3*a+3*(1-t)**2*t*s[1]+3*(1-t)*t*t*s[3]+t**3*b; if(y<Math.min(a,b)-1e-6 || y>Math.max(a,b)+1e-6)return false;} return true; }); }")
            requests = []
            page.on('request', lambda request: requests.append(request.url) if '/api/' in request.url else None)
            page.evaluate("() => { Object.defineProperty(document,'hidden',{value:true,configurable:true}); document.dispatchEvent(new Event('visibilitychange')); }")
            requests.clear()
            page.wait_for_timeout(1300)
            assert not requests, requests
            page.evaluate("() => { Object.defineProperty(document,'hidden',{value:false,configurable:true}); document.dispatchEvent(new Event('visibilitychange')); }")
            page.wait_for_timeout(400)
            assert any('/api/history?since=' in url and not url.endswith('=0') for url in requests), requests
            page.get_by_role('button', name='Expand all stacks', exact=True).click()
            assert page.locator('#stacksContainer').inner_text().count(attack) >= 2
            assert page.locator('#stacksContainer img').count() == 0
            assert page.evaluate('window.dockpitXss') is None
            assert page.locator('#qf-issues').inner_text() == 'Needs attention (1)'
            assert page.locator('#hostHistory').evaluate('(el) => el.width > 0'), (errors, csp_errors)
            assert page.locator('#sysGpu').inner_text() == '42.0%'
            assert page.locator('.host-metrics-grid').evaluate('(el) => getComputedStyle(el).gridTemplateColumns.split(" ").length') == 5
            assert page.locator('#sysGpuName').inner_text() == attack
            assert '25.0%' in page.locator('#stacksContainer').inner_text()
            assert 'VRAM 256 MB' in page.locator('#stacksContainer').inner_text()
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
            page.locator('#systemIndicator').click()
            page.wait_for_function("() => document.getElementById('diagnosticsText')?.textContent.includes('socket_exists')")
            assert 'Docker connection' in page.locator('#diagnosticsPanel').inner_text()
            assert not page.locator('#diagnosticsText').is_visible()
            page.locator('.raw-details summary').click()
            assert page.locator('#diagnosticsText').is_visible()
            page.locator('[data-action=close]').click()
            for card in ['cpu', 'gpu', 'memory', 'storage', 'system']:
                page.locator(f'[data-action=card][data-value={card}]').click()
                assert page.locator('#modalContent').inner_text()
                if card == 'gpu':
                    assert attack in page.locator('#modalContent').inner_text()
                    assert page.locator('#modalContent img').count() == 0
                    assert '55.0 °C' in page.locator('#modalContent').inner_text()
                page.locator('[data-action=close]').click()
            page.locator('#sortBy').select_option('name')
            page.reload(wait_until='networkidle')
            assert page.locator('#sortBy').input_value() == 'name'
            assert page.locator('[data-action=logs]').count() == 2  # expansion persisted
            # Bulk grouping, safe stack names, refresh persistence, and reset.
            custom_stack = '<img src=x onerror="window.dockpitXss=2">'
            page.get_by_role('button', name='Organize stacks', exact=True).click()
            page.screenshot(path='/tmp/dockpit-stacks-desktop.png', full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Stack editor overflows on mobile'
            page.screenshot(path='/tmp/dockpit-stacks-mobile.png', full_page=True)
            page.set_viewport_size({'width': 1440, 'height': 1100})
            page.locator('#stackSearch').fill('broken')
            page.locator('[data-action=stack-select]').click()
            assert page.locator('#stackSelection').inner_text() == '1 selected'
            page.locator('#stackDestination').fill(custom_stack)
            page.locator('[data-action=stack-assign]').click()
            assert page.evaluate('dashboardData().total_stacks') == 2
            assert custom_stack in page.locator('#stacksContainer h2').all_text_contents()
            assert page.locator('#stacksContainer img').count() == 0
            page.reload(wait_until='networkidle')
            assert page.evaluate('dashboardData().total_stacks') == 2
            page.get_by_role('button', name='Organize stacks', exact=True).click()
            page.locator('#stackSearch').fill('broken')
            page.locator('[data-action=stack-select]').click()
            assert page.locator('#stackSelection').inner_text() == '1 selected'
            assert 'Your choice' in page.locator('#stackChoices').inner_text()
            page.locator('[data-action=stack-reset]').click()
            assert page.evaluate('dashboardData().total_stacks') == 1
            page.get_by_role('button', name='Organize stacks', exact=True).click()
            page.locator('[data-action=stack-select]').click()
            page.locator('#stackDestination').fill('Games')
            page.locator('[data-action=stack-assign]').click()
            assert page.locator('#stacksContainer h2').all_text_contents() == ['Games']
            assert page.locator('[data-action=logs]').count() == 2
            assert page.evaluate("dashboardData().stacks.Games.total_cpu") == 12.5
            assert page.evaluate("dashboardData().stacks.Games.total_mem_bytes") == 64 * 1024**2
            assert page.evaluate("dashboardData().stacks.Games.total_pids") == 3
            assert page.evaluate("dashboardData().stacks.Games.total_containers") == 2
            assert page.evaluate('window.dockpitXss') is None
            # A failed storage write keeps the previous organization intact.
            page.get_by_role('button', name='Organize stacks', exact=True).click()
            page.locator('[data-action=stack-select]').click()
            page.locator('#stackDestination').fill('Unsaved')
            page.evaluate("() => { Storage.prototype.setItem = () => { throw new Error('disabled'); }; }")
            page.locator('[data-action=stack-assign]').click()
            assert 'Could not save' in page.locator('#stackFeedback').inner_text()
            page.locator('[data-action=close]').click()
            assert page.locator('#stacksContainer h2').all_text_contents() == ['Games']
            page.reload(wait_until='networkidle')
            app.cached_data['timestamp'] = time.time() - 120
            page.locator('[data-action=refresh]').click()
            page.wait_for_function("() => document.getElementById('statusText').textContent.startsWith('Stale')")
            assert page.locator('#dataWarning').is_visible()
            page.screenshot(path='/tmp/dockpit-browser-desktop.png', full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile layout overflows'
            page.screenshot(path='/tmp/dockpit-browser-mobile.png', full_page=True)
            # Intel integrated memory must not be displayed as missing VRAM.
            app.cached_data['system']['gpu'] = {
                'utilization_pct': None, 'memory_used_bytes': None, 'memory_total_bytes': None,
                'devices': [{'id': 'card1', 'name': 'Intel UHD Graphics 730', 'vendor': 'Intel',
                             'utilization_pct': None, 'memory_kind': 'shared',
                             'memory_used_bytes': None, 'memory_total_bytes': None,
                             'frequency_mhz': 0, 'max_frequency_mhz': 1450,
                             'temperature_c': None, 'power_w': None,
                             'utilization_note': attack, 'memory_note': 'Uses system RAM dynamically.'}]}
            page.locator('[data-action=refresh]').click()
            page.wait_for_function("() => document.getElementById('sysGpuName').textContent === 'No usage data'")
            assert page.locator('#sysGpu').inner_text() == 'Intel UHD Graphics 730'
            assert page.locator('#sysGpuBar').evaluate('(el) => el.style.width') == '0%'
            page.locator('[data-action=card][data-value=gpu]').click()
            content = page.locator('#modalContent').inner_text()
            assert 'GPU load:' in content and 'Not measured' in content
            assert 'Memory:' in content and 'Shared system RAM' in content
            assert 'Current clock:' in content and '0 MHz' in content
            assert 'Configured max clock:' in content and '1450 MHz' in content
            assert 'Temperature:' not in content and 'Power:' not in content
            assert attack in content and page.locator('#modalContent img').count() == 0
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path='/tmp/dockpit-intel-mobile.png', full_page=True)
            page.locator('[data-action=close]').click()
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
