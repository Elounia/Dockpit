"""
Unit and regression tests for Dockpit.
Verifies health checks, metrics normalization, read-only API security,
server-side secret stripping, and injection prevention.
"""

import json
from pathlib import Path
import subprocess
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

import dockpit.app as monitor


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
             patch.object(monitor, 'get_host_resources', return_value={'cpu_count': 12, 'mem_total_bytes': 48 * 1024**3}):
            result = monitor.fetch_docker_metrics()
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
            result = monitor.fetch_docker_metrics()
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


class ApiTests(unittest.TestCase):
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
            State={'Status': 'running'},
            RestartCount=0,
            HostConfig={},
            Config={'Env': ['PASSWORD=super_secret_password', 'API_KEY=xyz123']}
        )
        with patch.object(monitor.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps([item]))):
            with urlopen(self.url + '/api/inspect?name=known') as res:
                body = res.read().decode('utf-8')
        self.assertNotIn('super_secret_password', body)
        self.assertNotIn('API_KEY', body)
        self.assertNotIn('Env', body)
        self.assertIn('Resources', body)

    def test_logs_are_capped(self):
        with patch.object(monitor.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'sample log stream', '')) as run:
            with urlopen(self.url + '/api/logs?name=known&tail=99999') as res:
                self.assertEqual(res.read(), b'sample log stream')
        self.assertEqual(run.call_args.args[0], ['docker', 'logs', '--tail', '500', 'known'])


if __name__ == '__main__':
    unittest.main()

