"""Serving-account RPC boundaries and model promotion; no network or real engines."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('service_manager', ROOT / 'system_files/usr/share/ai-box/service.py')
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)
IMAGE = service.REPOSITORY + '@sha256:' + 'a' * 64
REVISION = 'b' * 40


class ServiceManager(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        for patcher in [patch.object(Path, 'home', return_value=self.home),
                        patch.object(service.runtime, 'DEFAULT_CONFIG', self.home / '.config/ai-box/config.json')]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.model = dict(repo='example/model', revision=REVISION)
        self.contents = {f'Q4/model-{n:05d}-of-00002.gguf': f'weight-{n}'.encode() for n in (1, 2)}
        self.variant = dict(first=sorted(self.contents)[0],
            files={name:hashlib.sha256(data).hexdigest() for name, data in self.contents.items()}, bytes=16)
        self.selection = dict(self.model, first=self.variant['first'])
        self.settings = dict(render_device='/dev/dri/renderD128', port=8080, context=8192,
                             memory_gib='auto', reserve_gib=4)

    def config(self):
        return service.model_config(self.model, self.variant, self.settings, IMAGE)

    def populate(self, directory):
        for name, data in self.contents.items():
            path = Path(directory) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def test_catalog_pins_revision_groups_shards_and_excludes_sidecars(self):
        siblings = [dict(rfilename=name, size=len(data), lfs={'sha256':self.variant['files'][name]})
                    for name, data in self.contents.items()]
        for name in ['MTP/draft.gguf', 'mmproj-model.gguf', '../outside.gguf', 'incomplete-00001-of-00002.gguf']:
            siblings.append(dict(rfilename=name, size=10, lfs={'sha256':'c'*64}))
        opener = SimpleNamespace(open=lambda *a, **kw: io.BytesIO(json.dumps(dict(sha=REVISION, siblings=siblings)).encode()))
        with patch.object(service.urllib.request, 'build_opener', return_value=opener):
            result = service.catalog('example/model')
        self.assertEqual(result['revision'], REVISION)
        self.assertEqual(len(result['variants']), 1)
        self.assertEqual(result['variants'][0]['files'], self.variant['files'])

    def test_catalog_rejects_unpinned_or_unsafe_input_before_network(self):
        with patch.object(service.urllib.request, 'build_opener') as opener:
            for repo, revision in [('example/../../secret','main'), ('example/model','branch/name'), ('--flag/model','main')]:
                with self.subTest(repo=repo), self.assertRaises(ValueError):
                    service.catalog(repo, revision)
            opener.assert_not_called()

    def test_quantizations_have_separate_immutable_storage(self):
        first = self.config()['model_dir']
        other = dict(self.variant, first='Q5/another.gguf', files={'Q5/another.gguf':'c'*64})
        second = service.model_config(self.model, other, self.settings, IMAGE)['model_dir']
        self.assertNotEqual(first, second)

    def test_download_promotes_only_after_all_checksums_match(self):
        def downloader(*args, **kwargs):
            request = json.loads(kwargs['input'])
            self.populate(request['directory'])
            self.assertEqual(request['revision'], REVISION)
            return SimpleNamespace(returncode=0)
        with patch.object(service, 'selected_model', return_value=(self.model, self.variant)), \
             patch.object(service.subprocess, 'run', side_effect=downloader):
            service.download(self.selection)
        destination = Path(self.config()['model_dir'])
        self.assertTrue(destination.is_dir())
        self.assertEqual((destination / self.variant['first']).read_bytes(), self.contents[self.variant['first']])
        with patch.object(service, 'selected_model', return_value=(self.model, self.variant)), \
             patch.object(service.subprocess, 'run') as downloader:
            service.download(self.selection)
            downloader.assert_not_called()

    def test_partial_or_corrupt_download_does_not_replace_installed_artifacts(self):
        def corrupt(*args, **kwargs):
            request = json.loads(kwargs['input'])
            self.populate(request['directory'])
            (Path(request['directory']) / sorted(self.contents)[-1]).write_bytes(b'bad')
            return SimpleNamespace(returncode=0)
        with patch.object(service, 'selected_model', return_value=(self.model, self.variant)), \
             patch.object(service.subprocess, 'run', side_effect=corrupt):
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                service.download(self.selection)
        self.assertFalse(Path(self.config()['model_dir']).exists())

    def test_prepare_uses_pinned_runtime_and_preserves_restricted_quadlet(self):
        self.populate(self.config()['model_dir'])
        with patch.object(service, 'selected_model', return_value=(self.model, self.variant)), \
             patch.object(service, 'resolve_runtime', return_value=IMAGE), \
             patch.object(service.runtime, 'doctor', return_value={'ready':True,'mem_total_gib':128}):
            result = service.prepare(dict(channel='vulkan-radv', model=self.selection, config=None, settings=self.settings))
        for text in [IMAGE, 'MemoryMax=124G', 'PublishPort=127.0.0.1:', 'DropCapability=all', '--api-key-file']:
            self.assertIn(text, result['unit'])
        for text in ['seccomp=unconfined', 'label=disable', 'Network=host', '--privileged']:
            self.assertNotIn(text, result['unit'])
        self.assertFalse(service.runtime.DEFAULT_CONFIG.exists())

    def test_unsupported_runtime_is_rejected_before_podman(self):
        with patch.object(service.runtime, 'run') as run:
            with self.assertRaises(ValueError):
                service.resolve_runtime('rocm-10.0')
            run.assert_not_called()

    def test_private_install_restores_previous_configuration_on_failure(self):
        path = service.runtime.DEFAULT_CONFIG
        previous = b'{"private": "prior config"}'
        service.private_write(path, previous)
        with patch.object(service, 'status', return_value={'linger':True}), \
             patch.object(service.runtime, 'install', side_effect=ValueError('preflight failed')):
            with self.assertRaises(ValueError):
                service.install(self.config())
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.with_suffix('.json.previous').read_bytes(), previous)

    def test_persistent_install_requires_linger_before_writing_config(self):
        with patch.object(service, 'status', return_value={'linger':False}), \
             patch.object(service.runtime, 'install') as install:
            with self.assertRaisesRegex(ValueError, 'enable linger'):
                service.install(self.config())
            install.assert_not_called()
        self.assertFalse(service.runtime.DEFAULT_CONFIG.exists())

    def test_concurrent_service_operations_are_serialized(self):
        request = {'operation':'install'}
        with service.operation_lock(request):
            with self.assertRaisesRegex(ValueError, 'in progress'):
                with service.operation_lock(request):
                    self.fail('Concurrent install was admitted')
            # Model staging is separate from control of the currently running service.
            with service.operation_lock({'operation':'download'}):
                pass

    def test_rpc_never_accepts_arbitrary_commands_or_extra_arguments(self):
        for request in [dict(operation='shell', args={'command':'id'}),
                        dict(operation='stop', args={'unit':'sshd.service'}),
                        dict(operation='inspect', args={}, extra='ignored?')]:
            with self.subTest(request=request), self.assertRaises(ValueError):
                service.dispatch(request)

    def test_engine_output_is_captured_instead_of_corrupting_rpc(self):
        result = service.runtime.run([sys.executable, '-c',
            'import sys; print("engine output"); print("engine diagnostic", file=sys.stderr)'])
        self.assertEqual(result.stdout.strip(), 'engine output')
        self.assertEqual(result.stderr.strip(), 'engine diagnostic')

    def test_protocol_rejects_root_without_dispatch(self):
        output = io.StringIO()
        with patch.object(service.os, 'geteuid', return_value=0), patch.object(service, 'dispatch') as dispatch, \
             contextlib.redirect_stdout(output):
            self.assertEqual(service.main(), 1)
            dispatch.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())['ok'])

    def test_protocol_is_one_json_response_without_helper_output(self):
        output = io.StringIO()
        def action(request):
            print('progress that is not protocol output')
            return {'message':'done'}
        with patch.object(service.os, 'geteuid', return_value=1000), \
             patch.object(service.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b'{"operation":"inspect","args":{}}'))), \
             patch.object(service, 'dispatch', side_effect=action), contextlib.redirect_stdout(output):
            self.assertEqual(service.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), {'ok':True, 'result':{'message':'done'}})


if __name__ == '__main__':
    unittest.main()
