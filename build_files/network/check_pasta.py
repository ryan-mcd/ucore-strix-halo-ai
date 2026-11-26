"""Optional live IPv4 probe in a disposable Docker container with namespace creation allowed.

The outer test container needs nested namespace creation enabled; pasta itself
runs as a temporary non-root account. This is not an inference launch profile.
"""
import http.server
import json
import os
from pathlib import Path
import pwd
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request

if os.geteuid() != 0 or not Path('/.dockerenv').exists():
    raise SystemExit('Run only inside a disposable Docker test container')
subprocess.run(['useradd', '-m', 'pasta_probe'], check=True)
account = pwd.getpwnam('pasta_probe')
assert account.pw_uid != 0
base = Path(tempfile.mkdtemp(prefix='ai-box-pasta-'))
os.chown(base, account.pw_uid, account.pw_gid)
child = base / 'probe.py'
child.write_text('''import http.server, json, pathlib, socket, struct
base = pathlib.Path(__file__).parent
gateway = next(socket.inet_ntoa(struct.pack('<I', int(line.split()[2], 16)))
               for line in pathlib.Path('/proc/net/route').read_text().splitlines()[1:]
               if line.split()[1] == '00000000')
blocked = []
for address in (gateway, '169.254.1.2'):
    try:
        with socket.create_connection((address, 18081), timeout=0.5):
            pass
    except OSError:
        blocked.append(address)
(base / 'result.json').write_text(json.dumps({'blocked': len(blocked),
    'uid_map': pathlib.Path('/proc/self/uid_map').read_text()}))
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'pasta-network-ok')
    def log_message(self, *args):
        pass
http.server.HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
''')


class HostOnly(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'host-only')

    def log_message(self, *args):
        pass


server = http.server.HTTPServer(('127.0.0.1', 18081), HostOnly)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
assert opener.open('http://127.0.0.1:18081', timeout=2).read() == b'host-only'
with (base / 'pasta.log').open('w') as log:
    process = subprocess.Popen(['runuser', '-u', account.pw_name, '--', 'pasta',
        '--config-net', '--no-map-gw', '--map-guest-addr', 'none',
        '-t', '127.0.0.1/18080:8080', '-u', 'none', '-T', 'none', '-U', 'none',
        '--', 'python3', str(child)], cwd=account.pw_dir, stdout=log, stderr=log,
        start_new_session=True)
    try:
        for attempt in range(100):
            try:
                with opener.open('http://127.0.0.1:18080', timeout=0.2) as response:
                    assert response.read() == b'pasta-network-ok'
                break
            except OSError:
                if process.poll() is not None:
                    raise AssertionError((base / 'pasta.log').read_text())
                time.sleep(0.1)
        else:
            raise AssertionError('Loopback forwarding failed: ' + (base / 'pasta.log').read_text())
        result = json.loads((base / 'result.json').read_text())
        assert result['blocked'] == 2, 'A container-to-host mapping remained reachable'
        assert any(int(line.split()[1]) == account.pw_uid for line in result['uid_map'].splitlines()), result
        address = socket.gethostbyname(socket.gethostname())
        assert not address.startswith('127.'), 'Fixture needs a separate outer interface address'
        try:
            with socket.create_connection((address, 18080), timeout=0.5):
                pass
        except OSError:
            pass
        else:
            raise AssertionError('Published port was reachable on a non-loopback address')
        print('PASS: non-root pasta, IPv4 loopback forwarding, no non-loopback publication, gateway/guest host mappings blocked; GPU/inference/IPv6 untested.')
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        server.shutdown()
        server.server_close()
