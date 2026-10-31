"""
Unit and regression tests for Dockpit.
Verifies health checks, metrics normalization, read-only API security,
server-side secret stripping, and injection prevention.
"""

import json
import io
import tempfile
import sys
from pathlib import Path
import subprocess
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import dockpit.app as monitor


def system_fixture():
    return {'cpus': 12, 'mem_total_bytes': 48 * 1024**3, 'host_cpu_pct': 20, 'mem_pct': 30, 'availability': {'cpu': True, 'memory': True, 'storage': True}}


def container_fixture(name='known', state='running', status='Up 1 hour (healthy)', labels=None):
    return {'Id': name.ljust(64, 'a'), 'Names': ['/' + name], 'Image': 'demo:1', 'Labels': labels or {'com.docker.compose.project': 'demo'}, 'State': state, 'Status': status}


class MetricsTests(unittest.TestCase):
    def test_health_stopped_containers_and_whole_host_cpu(self):
        containers = [
            dict(Names=['broken'], ID='abc123456789', State='running', Status='Up 1 hour (unhealthy)', Labels='com.docker.compose.project=demo'),
            dict(Names=['worker'], ID='def456789012', State='running', Status='Up 1 hour', Labels='com.docker.compose.project=demo'),
            dict(Names=['stopped'], ID='aaa111222333', State='exited', Status='Exited (0)', Labels='com.docker.compose.project=demo'),
        ]
        stats = [dict(Name='broken', CPUPerc='120.00%', MemUsage='1GiB / 4GiB', MemPerc='25%', PIDs='3')]
        responses = [subprocess.CompletedProcess([], 0, '\n'.join(map(json.dumps, rows))) for rows in (containers, stats)]
        with patch.object(monitor.subprocess, 'run', side_effect=responses), \
             patch.object(monitor, 'get_system_metrics', return_value=system_fixture()):
            result = monitor._fetch_docker_metrics_subprocess()
        cs = {c['name']: c for c in result['stacks']['demo']['containers']}
        self.assertEqual(result['total_cpu_host_perc'], 10)
        self.assertEqual(result['unhealthy_containers'], 1)
        self.assertEqual(result['running_containers'], 2)
        self.assertEqual(result['total_containers'], 3)
        self.assertEqual(cs['broken']['health'], 'unhealthy')
        self.assertEqual(cs['worker']['health'], 'running')
        self.assertFalse(cs['worker']['stats_available'])
        self.assertEqual(cs['stopped']['health'], 'exited')
        self.assertEqual(cs['stopped']['mem_used_bytes'], 0)

    def test_empty_daemon_is_a_valid_snapshot(self):
        with patch.object(monitor.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '')):
            result = monitor._fetch_docker_metrics_subprocess()
        self.assertIsNotNone(result)
        self.assertEqual(result['stacks'], {})
        self.assertEqual(result['total_containers'], 0)

    def test_system_hardware_and_disk_details(self):
        sys_metrics = monitor.get_system_metrics()
        self.assertIn('details', sys_metrics)
        details = sys_metrics['details']
        self.assertIn('cpu', details)
        self.assertIn('memory', details)
        self.assertIn('storage', details)
        self.assertIn('docker', details)
        self.assertIn('devices', details['storage'])
        self.assertIsInstance(details['storage']['devices'], list)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = monitor.ThreadingHTTPServer(('127.0.0.1', 0), monitor.DockerCockpitHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        monitor.cached_data = {
            'timestamp': time.time() - 60,
            'stacks': {
                'demo': {
                    'containers': [
                        dict(name='known', id='abc123', full_id='abc123456789')
                    ]
                }
            }
        }


class ApiTests(ServerTests):
    def test_dashboard_html_returns_200(self):
        with urlopen(self.url + '/') as res:
            self.assertEqual(res.status, 200)
            body = res.read().decode('utf-8')
            self.assertIn('Dockpit', body)
            self.assertIn('Host CPU', body)

    def test_stale_snapshot_is_marked(self):
        with urlopen(self.url + '/api/stats') as res:
            self.assertTrue(json.load(res)['stale'])

    def test_unknown_names_and_invalid_tail_never_invoke_docker(self):
        with patch.object(monitor.subprocess, 'run') as run:
            for path in ['/api/logs?name=--help', '/api/inspect?name=unknown', '/api/logs?name=known&tail=all']:
                with self.assertRaises(HTTPError) as error:
                    urlopen(self.url + path)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
            run.assert_not_called()

    def test_inspection_excludes_secrets(self):
        item = dict(
            Name='/known',
            State={'Status': 'running', 'Health': {'Status': 'healthy', 'Log': [{'Output': 'private_health_token'}]}},
            RestartCount=0,
            HostConfig={},
            Config={'Env': ['PASSWORD=super_secret_password', 'API_KEY=xyz123']}
        )
        with patch.object(monitor, 'docker_api_inspect', return_value=item):
            with urlopen(self.url + '/api/inspect?name=known') as res:
                body = res.read().decode('utf-8')
        self.assertNotIn('super_secret_password', body)
        self.assertNotIn('API_KEY', body)
        self.assertNotIn('Env', body)
        self.assertNotIn('private_health_token', body)
        self.assertIn('Resources', body)

    def test_logs_are_capped(self):
        with patch.object(monitor, 'docker_api_logs', return_value='sample log stream') as run:
            with urlopen(self.url + '/api/logs?name=known&tail=99999') as res:
                self.assertEqual(res.read(), b'sample log stream')
        run.assert_called_once_with('known', 500, False)


class SocketTests(unittest.TestCase):
    def test_socket_includes_stopped_and_unhealthy_containers(self):
        containers = [container_fixture('broken', status='Up (unhealthy)'), container_fixture('stopped', 'exited', 'Exited (1)')]
        stats = {'cpu_stats': {'cpu_usage': {'total_usage': 200}, 'system_cpu_usage': 1000, 'online_cpus': 12}, 'precpu_stats': {'cpu_usage': {'total_usage': 100}, 'system_cpu_usage': 0}, 'memory_stats': {'usage': 2000, 'stats': {'inactive_file': 500}, 'limit': 4000}}
        def docker(path, *args):
            return containers if path.startswith('/containers/json') else stats
        monitor._container_cpu_cache.clear()
        with patch.object(monitor, 'get_system_metrics', return_value=system_fixture()), patch.object(monitor, 'docker_json', side_effect=docker) as request:
            result = monitor._fetch_docker_metrics_socket()
        rows = {row['name']: row for row in result['stacks']['demo']['containers']}
        self.assertEqual(result['total_containers'], 2)
        self.assertEqual(set(rows), {'broken', 'stopped'})
        self.assertEqual(rows['broken']['health'], 'unhealthy')
        self.assertEqual(result['unhealthy_containers'], 1)
        self.assertEqual(result['total_cpu_host_perc'], 10)
        self.assertEqual(rows['broken']['mem_used_bytes'], 1500)
        self.assertFalse(rows['stopped']['stats_available'])
        self.assertEqual(rows['stopped']['health'], 'exited')
        self.assertEqual(request.call_count, 2)

    def test_missing_stats_are_not_reported_as_available(self):
        with patch.object(monitor, 'docker_json', side_effect=OSError('unavailable')):
            row = monitor.fetch_single_container_stats(container_fixture(), 10000, 12)
        self.assertFalse(row['stats_available'])
        self.assertEqual(row['cpu_perc'], 0)

    def test_schema_matches_cli_and_empty_daemon(self):
        with patch.object(monitor, 'get_system_metrics', return_value=system_fixture()), patch.object(monitor, 'docker_json', return_value=[]):
            socket = monitor._fetch_docker_metrics_socket()
        with patch.object(monitor, 'get_system_metrics', return_value=system_fixture()), patch.object(monitor.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '')):
            cli = monitor._fetch_docker_metrics_subprocess()
        self.assertEqual(set(socket), set(cli))
        self.assertEqual(cli['docker_total_cpu'], 0)
        self.assertIn('system', cli)

    def test_socket_failure_uses_cli_when_available(self):
        with patch.object(monitor.os.path, 'exists', return_value=True), patch.object(monitor.shutil, 'which', return_value='/usr/bin/docker'), patch.object(monitor, '_fetch_docker_metrics_socket', side_effect=OSError()), patch.object(monitor, '_fetch_docker_metrics_subprocess', return_value={'backend': 'cli'}) as cli:
            self.assertEqual(monitor.fetch_docker_metrics()['backend'], 'cli')
            cli.assert_called_once()

    def test_labels_validate_url_and_icon(self):
        row = monitor.container_metadata(container_fixture(labels={'dockpit.name': '<test>', 'dockpit.url': 'javascript:alert(1)', 'dockpit.icon': 'fa-x" onclick="bad'}))
        self.assertEqual(row['display_name'], '<test>')
        self.assertEqual(row['url'], '')
        self.assertEqual(row['icon'], 'fa-cube')
        row = monitor.container_metadata(container_fixture(labels={'dockpit.url': 'https://example.com/app'}))
        self.assertEqual(row['url'], 'https://example.com/app')

    def test_hidden_containers_are_excluded(self):
        with patch.object(monitor, 'get_system_metrics', return_value=system_fixture()), patch.object(monitor, 'docker_json', return_value=[container_fixture(labels={'dockpit.hide': 'true'})]):
            self.assertEqual(monitor._fetch_docker_metrics_socket()['total_containers'], 0)

    def test_missing_host_measurements_have_no_fabricated_capacity(self):
        with tempfile.TemporaryDirectory() as root, patch.object(monitor, 'PROC_DIR', root), patch.object(monitor, 'ROOT_DIR', root + '/missing'), patch.object(monitor, 'get_docker_engine_details', return_value={}), patch.object(monitor, 'get_block_devices', return_value=[]):
            result = monitor.get_system_metrics()
            self.assertEqual(result['mem_total_bytes'], 0)
            self.assertIsNone(result['mem_pct'])
            self.assertIsNone(result['root_disk']['total_bytes'])
            self.assertEqual(result['mem_total_formatted'], 'Unavailable')
            self.assertFalse(result['availability']['memory'])

    def test_poller_recovers_and_keeps_last_snapshot_on_failure(self):
        snapshot = monitor.aggregate_snapshot([], system_fixture(), 'socket')
        with patch.object(monitor, 'fetch_docker_metrics', side_effect=[snapshot, OSError('private token'), snapshot]):
            self.assertTrue(monitor.poll_once())
            self.assertFalse(monitor.poll_once())
            self.assertEqual(monitor.cached_data, snapshot)
            self.assertNotIn('private token', monitor.poll_status['last_error'])
            self.assertTrue(monitor.poll_once())
            self.assertIsNone(monitor.poll_status['last_error'])


class LogTests(unittest.TestCase):
    def read_logs(self, payload, tty=False):
        response = io.BytesIO(payload)
        response.status = 200
        connection = unittest.mock.MagicMock()
        connection.getresponse.return_value = response
        with patch.object(monitor.os.path, 'exists', return_value=True), patch.object(monitor, 'UnixHTTPConnection', return_value=connection), patch.object(monitor, 'docker_api_inspect', return_value={'Config': {'Tty': tty}}):
            return monitor.docker_api_logs('known')

    def test_tty_logs_preserve_raw_text(self):
        self.assertEqual(self.read_logs(b'hello world\nsecond line\n', tty=True), 'hello world\nsecond line\n')

    def test_framed_stdout_and_stderr(self):
        def frame(stream, body):
            return bytes([stream, 0, 0, 0]) + len(body).to_bytes(4, 'big') + body
        self.assertEqual(self.read_logs(frame(1, b'hello\n') + frame(2, b'error\n')), 'hello\nerror\n')

    def test_large_tty_and_framed_payloads_are_bounded(self):
        payload = b'x' * (monitor.MAX_LOG_BYTES * 2)
        self.assertEqual(len(self.read_logs(payload, tty=True)), monitor.MAX_LOG_BYTES)
        frame = bytes([1, 0, 0, 0]) + len(payload).to_bytes(4, 'big') + payload
        self.assertEqual(len(self.read_logs(frame)), monitor.MAX_LOG_BYTES)

    def test_cli_logs_bound_memory_and_collect_both_streams(self):
        original_popen = subprocess.Popen
        def start(command, **kwargs):
            return original_popen([sys.executable, '-c', "import sys; print('stderr line', file=sys.stderr, flush=True); sys.stdout.write('x' * (2 * 1024 * 1024))"], **kwargs)
        with patch.object(monitor.subprocess, 'Popen', side_effect=start):
            result = monitor.cli_logs('known', 100)
        self.assertIn('stderr line', result)
        self.assertEqual(len(result.encode()), monitor.MAX_LOG_BYTES)

    def test_cli_log_failures_do_not_expose_command_output(self):
        original_popen = subprocess.Popen
        with patch.object(monitor.subprocess, 'Popen', side_effect=lambda command, **kwargs: original_popen([sys.executable, '-c', "import sys; print('private failure'); sys.exit(1)"], **kwargs)):
            with self.assertRaisesRegex(RuntimeError, '^Logs could not be retrieved$'):
                monitor.cli_logs('known', 100)

    def test_malformed_log_header_is_rejected(self):
        with self.assertRaises(RuntimeError):
            self.read_logs(b'bad')


class HttpHardeningTests(ServerTests):
    def test_response_security_and_same_origin(self):
        with urlopen(self.url + '/api/stats') as response:
            self.assertIsNone(response.headers.get('Access-Control-Allow-Origin'))
            self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
            self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])

    def test_assets_and_traversal(self):
        for asset in ['app.js', 'app.css', 'vendor/fontawesome/webfonts/fa-solid-900.woff2']:
            with urlopen(self.url + '/static/' + asset) as response:
                self.assertEqual(response.status, 200)
        with self.assertRaises(HTTPError) as error:
            urlopen(self.url + '/static/../../pyproject.toml')
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_readiness_detects_stale_snapshot_but_liveness_is_ok(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(self.url + '/readyz')
        self.assertEqual(error.exception.code, 503)
        error.exception.close()
        with urlopen(self.url + '/healthz') as response:
            self.assertEqual(json.load(response)['status'], 'ok')

    def test_head_has_no_body(self):
        with urlopen(Request(self.url + '/api/stats', method='HEAD')) as response:
            self.assertEqual(response.read(), b'')
            self.assertGreater(int(response.headers['Content-Length']), 0)

    def test_concurrent_docker_requests_are_limited(self):
        for _ in range(4):
            self.assertTrue(monitor.api_slots.acquire(blocking=False))
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(self.url + '/api/logs?name=known')
            self.assertEqual(error.exception.code, 429)
            error.exception.close()
        finally:
            for _ in range(4):
                monitor.api_slots.release()

    def test_history_and_diagnostics(self):
        for endpoint in ['history', 'diagnostics']:
            with urlopen(self.url + '/api/' + endpoint) as response:
                self.assertIsInstance(json.load(response), dict)


if __name__ == '__main__':
    unittest.main()

