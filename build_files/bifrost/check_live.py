"""Actual Bifrost binary against a synthetic authenticated local provider.

No production keys, models or external network are used. Optional --ingress
requires NET_ADMIN and may run ONLY in a disposable Linux container: it assigns
loopback IPs and installs fixture nftables tables.
"""
import argparse
import http.client
import http.server
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--ingress', action='store_true')
args = parser.parse_args()
spec = importlib.util.spec_from_file_location('live_bifrost', '/usr/share/ai-box/bifrost.py')
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)
backend_key = 'dummy-backend-key-for-local-tests-only'
client_key = 'sk-bf-dummy-client-key-for-local-tests-only'
received = []


def run(*arguments):
    return subprocess.run(arguments, check=True, capture_output=True, text=True)


class Backend(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.handle_api()

    def do_POST(self):
        self.handle_api()

    def handle_api(self):
        authorized = self.headers.get('Authorization') == 'Bearer ' + backend_key
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        received.append((self.path, authorized, body))
        if not authorized:
            self.send_response(401)
            self.end_headers()
            return
        if self.path.endswith('/models'):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'object': 'list', 'data': [
                {'id': 'fixture', 'object': 'model', 'owned_by': 'ai-box'}]}).encode())
            return
        request = json.loads(body)
        assert request['model'] == 'fixture', request
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream' if request.get('stream') else 'application/json')
        self.end_headers()
        if request.get('stream'):
            for value in [
                {'id': 'fixture-chat', 'object': 'chat.completion.chunk', 'model': 'fixture',
                 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': None}]},
                {'id': 'fixture-chat', 'object': 'chat.completion.chunk', 'model': 'fixture',
                 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
            ]:
                self.wfile.write(('data: ' + json.dumps(value) + '\n\n').encode())
                self.wfile.flush()
            self.wfile.write(b'data: [DONE]\n\n')
        else:
            self.wfile.write(json.dumps({'id': 'fixture-chat', 'object': 'chat.completion',
                'model': 'fixture', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'OK'},
                                               'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}}).encode())

    def log_message(self, *arguments):
        pass


def request(path, token=None, data=None, source='127.0.0.1', extra_headers=None):
    connection = http.client.HTTPConnection('127.0.0.1', 8081,
        source_address=(source, 0), timeout=0.7 if args.ingress else 5)
    try:
        headers = dict(extra_headers or {})
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if data is not None:
            headers['Content-Type'] = 'application/json'
        connection.request('POST' if data is not None else 'GET', path,
                           json.dumps(data).encode() if data is not None else None, headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


if args.ingress:
    run('ip', 'link', 'set', 'lo', 'up')
    run('nft', 'add', 'table', 'inet', 'fixture_accept')
    run('nft', 'add', 'chain', 'inet', 'fixture_accept', 'input',
        '{ type filter hook input priority -100; policy accept; }')
    run('nft', 'add', 'rule', 'inet', 'fixture_accept', 'input', 'tcp', 'dport', '8081', 'accept')
    for _ in range(2):
        run('nft', '-f', '/usr/share/ai-box/api-ingress.nft')
    run('nft', 'list', 'table', 'inet', 'fixture_accept')

backend = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Backend)
threading.Thread(target=backend.serve_forever, daemon=True).start()
with tempfile.TemporaryDirectory(prefix='ai-box-bifrost-fixture-') as temporary:
    root = Path(temporary)
    root.chmod(0o700)
    config = root / 'config.json'
    config.write_text(json.dumps(gateway.configuration(backend.server_port)))
    config.chmod(0o600)
    os.chown(root, 65534, 65534)
    os.chown(config, 65534, 65534)
    env = dict(PATH='/usr/bin:/usr/sbin', HOME=temporary, GOMEMLIMIT='450MiB',
               AI_BOX_BACKEND_KEY=backend_key, AI_BOX_BIFROST_KEY=client_key,
               AI_BOX_BIFROST_ADMIN_PASSWORD='dummy-admin-password-for-tests-only',
               AI_BOX_BIFROST_ENCRYPTION_KEY='a' * 32)
    with tempfile.TemporaryFile(mode='w+') as logs:
        process = subprocess.Popen([gateway.BINARY, '-app-dir', temporary,
            '-host', '0.0.0.0', '-port', '8081', '-log-level', 'warn'], env=env,
            user=65534, group=65534, extra_groups=[], cwd=temporary, umask=0o077,
            stdout=logs, stderr=logs)
        try:
            deadline = time.monotonic() + 60
            while True:
                if process.poll() is not None:
                    raise AssertionError('Bifrost exited during startup')
                try:
                    if request('/health')[0] == 200:
                        break
                except (ConnectionError, TimeoutError):
                    pass
                if time.monotonic() > deadline:
                    raise AssertionError('Bifrost did not become healthy')
                time.sleep(0.1)
            for token in (None, 'incorrect-key', backend_key):
                assert request('/v1/models', token)[0] in (401, 403), token
            assert request('/api/providers')[0] in (401, 403, 503)
            status, body = request('/v1/models', client_key)
            assert status == 200 and b'fixture' in body, (status, body)
            prompt = {'model': 'local/fixture', 'messages': [{'role': 'user', 'content': 'Synthetic OK probe'}]}
            status, body = request('/v1/chat/completions', client_key, prompt)
            assert status == 200 and json.loads(body)['choices'][0]['message']['content'] == 'OK', (status, body)
            status, body = request('/v1/chat/completions', client_key, dict(prompt, stream=True))
            assert status == 200 and b'OK' in body and b'[DONE]' in body, (status, body)
            before = len(received)
            assert request('/v1/chat/completions', client_key, dict(prompt, model='openai/forbidden'))[0] >= 400
            assert len(received) == before
            assert request('/v1/chat/completions', backend_key, prompt,
                           extra_headers={'x-bf-direct-key': 'true'})[0] in (401, 403)
            assert all(authorized for _, authorized, _ in received), received
            assert not list(root.rglob('logs.db')), 'Gateway unexpectedly created a request log database'
            if args.ingress:
                allowed = ['10.0.0.1', '10.255.255.254', '192.168.0.1', '192.168.255.254',
                           '100.64.0.1', '100.127.255.254']
                denied = ['9.255.255.254', '11.0.0.1', '172.16.0.1', '172.31.255.254',
                          '192.167.255.254', '192.169.0.1', '100.63.255.254', '100.128.0.1',
                          '198.51.100.1']
                for source in allowed + denied:
                    run('ip', 'address', 'add', source + '/32', 'dev', 'lo')
                for source in allowed:
                    assert request('/v1/models', source=source)[0] in (401, 403), source
                    assert request('/v1/models', client_key, source=source)[0] == 200, source
                for source in denied:
                    try:
                        request('/v1/models', client_key, source=source)
                    except (ConnectionError, TimeoutError):
                        pass
                    else:
                        raise AssertionError('Forbidden source reached Bifrost: ' + source)
            print('PASS: actual unprivileged Bifrost, virtual-key/admin authentication, '
                  'backend-key isolation, local-provider confinement, completion and SSE streaming'
                  + ('; real nft ingress boundaries and earlier-accept bypass denial.' if args.ingress else '.'))
        except Exception:
            logs.seek(0)
            print(logs.read()[-12000:])  # Fixture contains synthetic credentials only.
            raise
        finally:
            process.terminate()
            process.wait(timeout=10)
            backend.shutdown()
            backend.server_close()
