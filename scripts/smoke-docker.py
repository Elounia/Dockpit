"""Start and clean up an isolated Dockpit container against the local Docker daemon."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen
import uuid


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def main():
    image = sys.argv[1] if len(sys.argv) > 1 else 'dockpit:test'
    name = 'dockpit-smoke-' + uuid.uuid4().hex[:12]
    sock = Path(os.environ.get('DOCKPIT_TEST_SOCKET', '/var/run/docker.sock'))
    try:
        docker('run', '-d', '--name', name, '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges:true', '--tmpfs', '/tmp', '--group-add', str(sock.stat().st_gid), '-p', '127.0.0.1::8989', '-v', f'{sock}:/var/run/docker.sock:ro', '-v', '/proc:/host/proc:ro', '-v', '/sys:/host/sys:ro', '-v', '/:/host/root:ro', image)
        container = json.loads(docker('inspect', name))[0]
        port = container['NetworkSettings']['Ports']['8989/tcp'][0]['HostPort']
        base = 'http://127.0.0.1:' + port
        deadline = time.monotonic() + 60
        while True:
            try:
                with urlopen(base + '/readyz', timeout=4) as response:
                    assert json.load(response)['ready']
                break
            except (URLError, OSError):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Container did not become ready')
                time.sleep(1)
        for path in ['/', '/static/app.js', '/static/app.css', '/static/vendor/fontawesome/webfonts/fa-solid-900.woff2']:
            with urlopen(base + path, timeout=5) as response:
                assert response.status == 200
                assert response.read()
        with urlopen(base + '/api/stats', timeout=5) as response:
            data = json.load(response)
        assert data['backend'] == 'socket'
        names = [c['name'] for s in data['stacks'].values() for c in s['containers']]
        assert name in names
        for endpoint in ['inspect', 'logs']:
            with urlopen(base + '/api/' + endpoint + '?name=' + name, timeout=10) as response:
                assert response.status == 200
                response.read()
        with urlopen(base + '/api/diagnostics', timeout=5) as response:
            assert json.load(response)['last_error'] is None
        assert container['Config']['User'] == 'dockpit:dockpit'
        print('Docker checks passed: non-root, read-only filesystem, socket access, readiness, inventory, inspect, logs and bundled assets.')
    finally:
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
