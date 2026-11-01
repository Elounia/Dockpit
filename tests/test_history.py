"""Host sampling, retention, delta cursors, and actionable diagnostics."""
import json
import threading
from urllib.request import urlopen
from urllib.error import HTTPError
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dockpit import app
from dockpit.history import HostSampler, TieredHistory


class SamplingTests(unittest.TestCase):
    def test_cpu_deltas_memory_and_missing_proc(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'stat').write_text('cpu  100 0 100 800 0 0 0 0\n')
            (root/'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 250 kB\n')
            sampler = HostSampler()
            first = sampler.read(directory, 1)
            self.assertIsNone(first['host_cpu'])
            self.assertEqual(first['host_memory'], 75)
            (root/'stat').write_text('cpu  120 0 110 870 0 0 0 0\n')
            second = sampler.read(directory, 2)
            self.assertAlmostEqual(second['host_cpu'], 30)
            (root/'stat').unlink()
            (root/'meminfo').unlink()
            missing = sampler.read(directory, 3)
            self.assertIsNone(missing['host_cpu'])
            self.assertIsNone(missing['host_memory'])

    def test_fixed_capacity_retention_and_ten_second_averages(self):
        history = TieredHistory()
        for timestamp in range(1, 8001):
            history.append({'timestamp':timestamp, 'host_cpu':timestamp % 10, 'host_memory':None})
        points = history.points(8000)
        self.assertLessEqual(len(history), 630)
        self.assertEqual(len(history.recent), 300)
        self.assertTrue(all(p['timestamp'] > 4400 for p in points))
        self.assertEqual(points[-1]['timestamp'], 8000)
        full_bucket = next(p for p in points if p['timestamp'] == 6000)
        self.assertEqual(full_bucket['host_cpu'], 4.5)
        self.assertIsNone(full_bucket['host_memory'])
        self.assertEqual([p['timestamp'] for p in history.points(8000, since=7997)], [7998,7999,8000])
        history.append({'timestamp':8000, 'host_cpu':99})
        self.assertEqual(history.points(8000)[-1]['host_cpu'], 0)

    def test_host_sampler_does_not_call_docker_or_hardware_inventory(self):
        with patch.object(app, 'fetch_docker_metrics') as docker, patch.object(app, 'get_system_metrics') as inventory, patch.object(app, 'history', TieredHistory()), patch.object(app, 'host_sample', {}):
            app.sample_host_once()
            self.assertTrue(app.host_sample)
            self.assertEqual(len(app.history), 1)
            docker.assert_not_called()
            inventory.assert_not_called()

    def test_poll_failure_is_timed_and_keeps_last_snapshot(self):
        with patch.object(app, 'fetch_docker_metrics', side_effect=OSError()), patch.object(app, 'cached_data', {'timestamp':123}), patch.object(app, 'poll_status', {}):
            self.assertFalse(app.poll_once())
            self.assertEqual(app.cached_data['timestamp'], 123)
            self.assertGreaterEqual(app.poll_status['poll_timing']['last_ms'], 0)
            self.assertIn('ram_bytes', app.poll_status['process'])

    def test_history_api_delta_cursor_reset_and_validation(self):
        now = time.time()
        history = TieredHistory()
        history.extend([{'timestamp':now-1,'host_cpu':10,'host_memory':20}, {'timestamp':now,'host_cpu':30,'host_memory':40}])
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.DockerCockpitHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}/api/history'
        try:
            with patch.object(app, 'history', history), patch.object(app, 'cached_data', {'system': {'cpus':8, 'mem_total_bytes':16 * 1024**3}}), patch.object(app, 'host_sample', {}):
                with urlopen(url) as response:
                    first = json.load(response)
                self.assertEqual(len(first['points']), 2)
                self.assertEqual(first['capacity'], {'cpu_threads':8, 'memory_bytes':16 * 1024**3})
                with urlopen(url + '?since=' + str(first['cursor'])) as response:
                    self.assertEqual(json.load(response)['points'], [])
                with urlopen(url + '?since=' + str(now + 1)) as response:
                    reset = json.load(response)
                self.assertTrue(reset['reset'])
                self.assertEqual(len(reset['points']), 2)
                for cursor in ['nan', '-1', 'inf', 'bad']:
                    with self.assertRaises(HTTPError) as error:
                        urlopen(url + '?since=' + cursor)
                    self.assertEqual(error.exception.code, 400)
                    error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_html_edits_are_served_without_restarting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / 'index.html'
            page.write_text('<html>First version</html>')
            server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.DockerCockpitHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(app, 'STATIC_DIR', root):
                    url = f'http://127.0.0.1:{server.server_port}/'
                    with urlopen(url) as response:
                        self.assertIn(b'First version', response.read())
                        self.assertEqual(response.headers['Cache-Control'], 'no-store')
                    page.write_text('<html>Updated version</html>')
                    with urlopen(url) as response:
                        self.assertIn(b'Updated version', response.read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_missing_measurements_and_gpu_have_reasons_and_fixes(self):
        data = {'system': {'gpu': {'devices':[{'name':'Intel iGPU','vendor':'Intel'}]}}, 'hidden_containers':2, 'inventory_total':5}
        with patch('dockpit.app.os.path.exists', return_value=False):
            diagnostics = app.diagnostics_payload(data, {'last_error':'Docker access failed'}, True, [])
        for measurement in diagnostics['measurements'].values():
            self.assertFalse(measurement['ok'])
            self.assertTrue(measurement['reason'])
            self.assertTrue(measurement['hint'])
        self.assertIn('/dev/dri', diagnostics['measurements']['GPU']['hint'])
        self.assertEqual((diagnostics['hidden_containers'],diagnostics['total_containers']), (2,5))
        self.assertTrue(diagnostics['last_error_hint'])
