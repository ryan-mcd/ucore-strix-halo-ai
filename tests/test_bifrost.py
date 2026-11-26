"""Credential separation, generated Bifrost access policy and launch boundaries."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bifrost_fixture', ROOT / 'system_files/usr/share/ai-box/bifrost.py')
bifrost = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bifrost)


class BifrostConfiguration(unittest.TestCase):
    def test_provider_and_virtual_key_are_confined_to_the_local_backend(self):
        config = bifrost.configuration(8080)
        self.assertTrue(config['client']['enforce_auth_on_inference'])
        self.assertFalse(config['client']['enable_logging'])
        self.assertTrue(config['client']['disable_content_logging'])
        self.assertFalse(config['client']['allow_per_request_content_storage_override'])
        self.assertFalse(config['logs_store']['enabled'])
        self.assertTrue(config['config_store']['enabled'])
        self.assertEqual(set(config['providers']), {'local'})
        provider = config['providers']['local']
        self.assertEqual(provider['network_config']['base_url'], 'http://127.0.0.1:8080')
        self.assertEqual(provider['keys'][0]['value'], 'env.AI_BOX_BACKEND_KEY')
        auth = config['governance']['auth_config']
        self.assertTrue(auth['is_enabled'])
        self.assertFalse(auth['disable_auth_on_inference'])
        key = config['governance']['virtual_keys'][0]
        self.assertEqual(key['value'], 'env.AI_BOX_BIFROST_KEY')
        self.assertEqual(key['provider_configs'][0]['provider'], 'local')
        self.assertEqual(key['provider_configs'][0]['key_ids'], ['ai-box-backend'])

    def test_credentials_are_private_separate_stable_and_absent_from_config(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(Path, 'home', return_value=Path(temporary)):
            bifrost.install(8080)
            root = bifrost.directory()
            secrets = {name: (root / name).read_bytes() for name in
                       ('client-key', 'admin-password', 'encryption-key')}
            self.assertEqual(len(set(secrets.values())), 3)
            self.assertTrue(secrets['client-key'].startswith(b'sk-bf-'))
            self.assertEqual(len(secrets['encryption-key'].strip()), 32)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            for name, value in secrets.items():
                self.assertEqual((root / name).stat().st_mode & 0o777, 0o600)
                self.assertNotIn(value.strip().decode(), (root / 'config.json').read_text())
            bifrost.install(9000)
            self.assertEqual(secrets, {name: (root / name).read_bytes() for name in secrets})
            config = json.loads((root / 'config.json').read_text())
            self.assertEqual(config['providers']['local']['network_config']['base_url'], 'http://127.0.0.1:9000')

    def test_invalid_backend_or_symlink_cannot_replace_other_files(self):
        for port in (True, 80, 8081, 65536, '8080'):
            with self.subTest(port=port), self.assertRaises(ValueError):
                bifrost.configuration(port)
        with tempfile.TemporaryDirectory() as temporary, patch.object(Path, 'home', return_value=Path(temporary)):
            root = bifrost.directory()
            root.parent.mkdir(parents=True)
            other = Path(temporary) / 'other'
            other.mkdir()
            root.symlink_to(other, target_is_directory=True)
            with self.assertRaises(ValueError):
                bifrost.install(8080)
            self.assertEqual(list(other.iterdir()), [])

    def test_root_cannot_start_the_gateway(self):
        with patch.object(bifrost.sys, 'platform', 'linux'), patch.object(bifrost.os, 'geteuid', return_value=0):
            with self.assertRaisesRegex(ValueError, 'unprivileged'):
                bifrost.serve()

    def test_firewall_failure_prevents_opening_the_listener(self):
        with patch.object(bifrost.sys, 'platform', 'linux'), patch.object(bifrost.os, 'geteuid', return_value=1000), \
             patch.object(bifrost.subprocess, 'run') as run, \
             patch.object(bifrost.time, 'monotonic', side_effect=[0, 121]), \
             patch.object(bifrost.os, 'execve') as execute:
            run.return_value.returncode = 1
            with self.assertRaisesRegex(ValueError, 'firewall'):
                bifrost.serve()
            execute.assert_not_called()

    def test_launch_scrubs_inherited_cloud_keys_and_uses_separate_private_secrets(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(Path, 'home', return_value=Path(temporary)):
            root = bifrost.directory()
            root.parent.mkdir(parents=True)
            (root.parent / 'config.json').write_text(json.dumps({'port': 9000}))
            (root.parent / 'api-key').write_text('dummy-backend-token-' + 'a' * 32)
            with patch.object(bifrost.sys, 'platform', 'linux'), patch.object(bifrost.os, 'geteuid', return_value=1000), \
                 patch.object(bifrost.subprocess, 'run') as run, patch.object(bifrost.os, 'execve') as execute, \
                 patch.object(bifrost.os, 'chdir'), patch.object(bifrost.os, 'umask'):
                run.return_value.returncode = 0
                bifrost.serve()
            binary, arguments, env = execute.call_args.args
            self.assertEqual(binary, bifrost.BINARY)
            self.assertEqual(arguments[-6:], ['-host', '0.0.0.0', '-port', '8081', '-log-level', 'warn'])
            self.assertEqual(set(env), set(bifrost.ENV_FILES) | {'PATH', 'HOME', 'GOMEMLIMIT', 'GOTOOLCHAIN'})
            self.assertNotEqual(env['AI_BOX_BACKEND_KEY'], env['AI_BOX_BIFROST_KEY'])

    def test_firewall_policy_and_pinned_binary_match_image_integration(self):
        source = json.loads((ROOT / 'build_files/bifrost/source.json').read_text())
        self.assertIn('FROM ' + source['image'] + ' AS bifrost', (ROOT / 'Containerfile').read_text())
        policy = ET.parse(ROOT / 'system_files/usr/lib/firewalld/policies/ai-box-api.xml').getroot()
        self.assertEqual(policy.attrib['target'], 'CONTINUE')
        self.assertEqual(policy.find('ingress-zone').attrib['name'], 'ANY')
        self.assertEqual(policy.find('egress-zone').attrib['name'], 'HOST')
        self.assertIsNone(policy.find('port'))
        self.assertEqual({r.find('source').attrib['address'] for r in policy.findall('rule')},
                         {'10.0.0.0/8', '192.168.0.0/16', '100.64.0.0/10'})
        for rule in policy.findall('rule'):
            self.assertEqual(rule.find('port').attrib, {'port': '8081', 'protocol': 'tcp'})
        guard = (ROOT / 'system_files/usr/share/ai-box/api-ingress.nft').read_text()
        self.assertIn('tcp dport 8081 drop', guard)
        self.assertNotIn('flush ruleset', guard)
