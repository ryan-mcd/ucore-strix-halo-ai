"""Image-owned Bifrost configuration and unprivileged launch. Python stdlib only."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time

BINARY = '/usr/libexec/ai-box-bifrost'
PORT = 8081
ENV_FILES = {
    'AI_BOX_BACKEND_KEY': '../api-key',
    'AI_BOX_BIFROST_KEY': 'client-key',
    'AI_BOX_BIFROST_ADMIN_PASSWORD': 'admin-password',
    'AI_BOX_BIFROST_ENCRYPTION_KEY': 'encryption-key',
}


def directory():
    return Path.home() / '.config/ai-box/bifrost'


def configuration(port):
    if type(port) is not int or not 1024 <= port <= 65535 or port == PORT:
        raise ValueError('Bifrost requires a valid loopback backend port other than 8081')
    return {
        '$schema': 'https://www.getbifrost.ai/schema',
        'source_of_truth': 'config.json',
        'encryption_key': 'env.AI_BOX_BIFROST_ENCRYPTION_KEY',
        'client': {
            'enforce_auth_on_inference': True,
            'allow_direct_keys': False,
            'enable_logging': False,
            'disable_content_logging': True,
            'allow_per_request_content_storage_override': False,
            'allowed_origins': [],
            'max_request_body_size_mb': 8,
        },
        'providers': {
            'local': {
                'keys': [{'id': 'ai-box-backend', 'name': 'local-llama',
                          'value': 'env.AI_BOX_BACKEND_KEY', 'models': ['*'], 'weight': 1}],
                'network_config': {'base_url': f'http://127.0.0.1:{port}',
                                   'default_request_timeout_in_seconds': 600},
                'custom_provider_config': {
                    'base_provider_type': 'openai',
                    'allowed_requests': {'list_models': True, 'chat_completion': True,
                                         'chat_completion_stream': True,
                                         'text_completion': True, 'text_completion_stream': True},
                },
            },
        },
        'config_store': {'enabled': True, 'type': 'sqlite', 'config': {'path': './config.db'}},
        'logs_store': {'enabled': False},
        'framework': {'pricing': {
            'pricing_url': 'file:///usr/share/ai-box/bifrost/empty-catalog.json',
            'model_parameters_url': 'file:///usr/share/ai-box/bifrost/empty-catalog.json',
            'mcp_library_sync_interval': 0,
            'live_models_sync_interval': 0,
        }},
        'governance': {
            'auth_config': {'is_enabled': True, 'admin_username': 'operator',
                            'admin_password': 'env.AI_BOX_BIFROST_ADMIN_PASSWORD',
                            'disable_auth_on_inference': False},
            'virtual_keys': [{
                'id': 'ai-box-client', 'name': 'private-clients',
                'value': 'env.AI_BOX_BIFROST_KEY', 'is_active': True,
                'disable_content_logging': True,
                'provider_configs': [{'provider': 'local', 'allowed_models': ['*'],
                                      'key_ids': ['ai-box-backend'], 'weight': 1}],
            }],
        },
    }


def private_write(path, value):
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('Bifrost private paths must not be symlinks')
    descriptor, name = tempfile.mkstemp(prefix='.bifrost-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(value)
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def install(port):
    config = configuration(port)
    root = directory()
    if root.is_symlink() or root.parent.is_symlink():
        raise ValueError('Bifrost private directories must not be symlinks')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    for filename, prefix, length in [('client-key', 'sk-bf-', 48),
                                     ('admin-password', '', 48), ('encryption-key', '', 32)]:
        path = root / filename
        if path.is_symlink():
            raise ValueError('Bifrost credentials must not be symlinks')
        if not path.exists():
            # The Bifrost encryption key is exactly 32 ASCII bytes.
            value = prefix + (secrets.token_hex(16) if length == 32 else secrets.token_urlsafe(length))
            private_write(path, value + '\n')
        path.chmod(0o600)
        value = path.read_text().strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', value) or (length == 32 and len(value) != 32):
            raise ValueError(f'Invalid Bifrost credential file: {filename}')
    private_write(root / 'config.json', json.dumps(config, indent=2) + '\n')


def serve():
    if sys.platform != 'linux' or os.geteuid() == 0:
        raise ValueError('Run Bifrost as the unprivileged serving account on Linux')
    # The host service owns the independently enforced ingress guard. Never
    # open a wildcard listener if that service failed or is absent. Wait for
    # boot-time host firewall startup before failing this user service.
    deadline = time.monotonic() + 120
    while subprocess.run(['systemctl', '--system', 'is-active', '--quiet',
                         'ai-box-api-firewall.service'], check=False).returncode:
        if time.monotonic() >= deadline:
            raise ValueError('Host API firewall is not active; Bifrost listener refused')
        time.sleep(1)
    root = directory()
    # Rebuild managed config from the installed backend settings on every
    # start, including upgrades of an existing inference deployment.
    config = json.loads((root.parent / 'config.json').read_text())
    install(config['port'])
    # Do not inherit unrelated cloud API keys or proxy settings into Bifrost.
    env = {'PATH': '/usr/bin:/usr/sbin', 'HOME': str(Path.home()),
           'GOMEMLIMIT': '450MiB', 'GOTOOLCHAIN': 'local'}
    for name, relative in ENV_FILES.items():
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError('Bifrost credential is absent or a symlink')
        env[name] = path.read_text().strip()
    os.umask(0o077)
    os.chdir(root)
    os.execve(BINARY, [BINARY, '-app-dir', str(root), '-host', '0.0.0.0',
                       '-port', str(PORT), '-log-level', 'warn'], env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['serve'])
    parser.parse_args()
    serve()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError) as error:
        print(f'ai-box-bifrost: {error}', file=sys.stderr)
        sys.exit(1)
