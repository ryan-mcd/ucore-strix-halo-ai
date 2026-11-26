"""Boundary tests for configuration rendering and model integrity."""
import importlib.machinery
import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
loader=importlib.machinery.SourceFileLoader('ai_box',str(ROOT/'system_files/usr/bin/ai-box'))
spec=importlib.util.spec_from_loader(loader.name,loader)
box=importlib.util.module_from_spec(spec)
loader.exec_module(box)


def config():
    c=json.loads((ROOT/'config/ai-box.example.json').read_text())
    c.update(image='docker.io/kyuz0/amd-strix-halo-toolboxes@sha256:'+'a'*64)
    c['model_files'] = {c['model_file']: 'b'*64}
    c['memory_gib'] = 48  # Small synthetic test workload, not a deployment recommendation.
    return c


def split_config():
    c = config()
    c['model_file'] = 'qwen/model-00001-of-00002.gguf'
    c['model_files'] = {f'qwen/model-{i:05d}-of-00002.gguf': 'b'*64 for i in (1, 2)}
    return c


class DoctorPreflight(unittest.TestCase):
    def setUp(self):
        self.files = {
            '/proc/meminfo': 'MemTotal: 134217728 kB\nMemAvailable: 125829120 kB\n',
            '/proc/cmdline': 'quiet',
            '/sys/fs/selinux/enforce': '1\n',
            '/sys/class/drm/renderD128/device/vendor': '0x1002\n',
            '/sys/module/ttm/parameters/pages_limit': '33554432\n',
            '/sys/module/amdgpu/parameters/gttsize': '-1\n',
        }

    def report(self):
        def read(path, *args, **kwargs):
            value = self.files[str(path)]
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(box.platform, 'system', return_value='Linux'), \
             patch.object(box.platform, 'machine', return_value='x86_64'), \
             patch.object(Path, 'read_text', autospec=True, side_effect=read), \
             patch.object(Path, 'exists', return_value=True), \
             patch.object(Path, 'glob', return_value=[Path('/dev/dri/renderD128')]), \
             patch.object(box.os, 'access', return_value=True), \
             patch.object(box.os, 'geteuid', return_value=1000), \
             patch.object(box.shutil, 'which', return_value='/usr/bin/tool'):
            return box.doctor()

    def test_readable_optional_parameters_are_reported(self):
        report = self.report()
        self.assertEqual(report['ttm.pages_limit'], '33554432')
        self.assertEqual(report['amdgpu.gttsize'], '-1')
        self.assertTrue(report['ready'])

    def test_permission_denied_on_either_optional_parameter_preserves_report(self):
        for module, name in [('ttm', 'pages_limit'), ('amdgpu', 'gttsize')]:
            path = f'/sys/module/{module}/parameters/{name}'
            with self.subTest(path=path):
                original = self.files[path]
                self.files[path] = PermissionError(13, 'Permission denied', path)
                report = self.report()
                self.assertEqual(report[f'{module}.{name}'], 'permission denied (optional diagnostic)')
                self.assertTrue(report['ready'])
                self.assertEqual(report['mem_total_gib'], 128)
                self.assertTrue(report['render_devices'][0]['accessible'])
                self.files[path] = original

    def test_missing_optional_parameters_are_not_exposed(self):
        for module, name in [('ttm', 'pages_limit'), ('amdgpu', 'gttsize')]:
            self.files[f'/sys/module/{module}/parameters/{name}'] = FileNotFoundError()
        report = self.report()
        self.assertEqual(report['ttm.pages_limit'], 'not exposed')
        self.assertEqual(report['amdgpu.gttsize'], 'not exposed')
        self.assertTrue(report['ready'])

    def test_optional_parameter_io_error_is_reported(self):
        self.files['/sys/module/amdgpu/parameters/gttsize'] = OSError(5, 'Input/output error')
        report = self.report()
        self.assertIn('Input/output error', report['amdgpu.gttsize'])
        self.assertTrue(report['ready'])

    def test_unreadable_optional_parameter_does_not_bypass_required_checks(self):
        self.files['/sys/module/amdgpu/parameters/gttsize'] = PermissionError()
        for path, value in [('/sys/fs/selinux/enforce', '0'),
                            ('/sys/class/drm/renderD128/device/vendor', '0x8086'),
                            ('/proc/cmdline', 'amd_iommu=off')]:
            with self.subTest(path=path):
                original = self.files[path]
                self.files[path] = value
                self.assertFalse(self.report()['ready'])
                self.files[path] = original


class ConfigBoundaries(unittest.TestCase):
    def test_render_limits_access(self):
        text=box.render(config())
        for required in ('PublishPort=127.0.0.1:8080:8080','ReadOnly=true','NoNewPrivileges=true',
                         'DropCapability=all','GroupAdd=keep-groups','MemoryMax=48G',
                         'MemorySwapMax=0','Pull=never','--parallel 1','--api-key-file'):
            self.assertIn(required,text)
        for forbidden in ('seccomp=unconfined','label=disable','Network=host','--privileged',
                          '/dev/kfd','podman.sock','docker.sock','AutoUpdate='):
            self.assertNotIn(forbidden,text)

    def test_rejects_mutable_or_unapproved_images(self):
        for image in ('docker.io/kyuz0/amd-strix-halo-toolboxes:vulkan-radv',
                      'ghcr.io/untrusted/image@sha256:'+'a'*64,
                      'docker.io/kyuz0/amd-strix-halo-toolboxes@sha256:'+'z'*64):
            c=config(); c['image']=image
            with self.subTest(image=image),self.assertRaises(ValueError):
                box.validated(c)

    def test_rejects_paths_that_can_escape_or_inject_unit_options(self):
        for field,value in [('model_dir','/'),('model_dir',str(Path.home())),
                            ('model_dir','~/.local/share/ai-box/models\nNetwork=host'),
                            ('model_file','../secret.gguf'),('model_file','/root/model.gguf'),
                            ('model_file','%h/model.gguf'),('model_file','model file.gguf'),
                            ('render_device','/dev/dri'),('render_device','/dev/kfd')]:
            c=config(); c[field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):
                box.validated(c)

    def test_rejects_invalid_limits_and_booleans(self):
        for field,value in [('port',80),('port',65536),('context',True),('context',0),
                            ('memory_gib',-1),('reserve_gib',0)]:
            c=config(); c[field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):
                box.validated(c)

    def test_unknown_memory_budget_modes_are_rejected(self):
        c=config(); c['memory_gib']='all'
        with self.assertRaisesRegex(ValueError, 'memory_gib'):
            box.validated(c)

    def test_auto_budget_uses_linux_visible_memory_and_requested_reserve(self):
        c=config(); c.update(memory_gib='auto', reserve_gib=4)
        for total, expected in ((128, 124), (123.73, 119)):
            with self.subTest(total=total), patch.object(box, 'doctor', return_value={'mem_total_gib':total}):
                self.assertIn(f'MemoryMax={expected}G', box.render(c))
        with patch.object(box, 'doctor', return_value={'platform':'Darwin'}):
            with self.assertRaisesRegex(ValueError, 'requires Linux'):
                box.render(c)

    def test_auto_budget_does_not_round_memory_up(self):
        c=config(); c.update(memory_gib='auto', reserve_gib=4)
        result=box.resolve_memory_budget(box.validated(c), {'mem_total_gib':123.999})
        self.assertEqual(result['memory_gib'], 119)

    def test_rejects_unverified_split_models(self):
        c=split_config(); c['model_sha256']=c.pop('model_files')[c['model_file']]
        with self.assertRaisesRegex(ValueError,'every shard'):
            box.validated(c)

    def test_legacy_single_file_configuration_is_preserved(self):
        c=config(); c['model_sha256']=c.pop('model_files')[c['model_file']]
        self.assertEqual(box.validated(c)['model_files'], {'model.gguf': 'b'*64})
        self.assertEqual(box.render(c), box.render(config()))

    def test_complete_split_set_renders_first_shard_and_keeps_controls(self):
        c=split_config()
        text=box.render(c)
        self.assertIn('--model /models/qwen/model-00001-of-00002.gguf ', text)
        for required in ('PublishPort=127.0.0.1:', 'ReadOnly=true', 'DropCapability=all',
                         '--api-key-file', '--parallel 1', 'WantedBy=default.target'):
            self.assertIn(required, text)
        self.assertNotIn('model-00002-of-00002.gguf', text)

    def test_manifest_requires_exact_complete_set(self):
        cases=[]
        c=split_config(); c['model_files'].pop('qwen/model-00002-of-00002.gguf'); cases.append(c)
        c=split_config(); c['model_files']['other-00002-of-00002.gguf']='b'*64; cases.append(c)
        c=split_config(); c['model_file']='qwen/model-00002-of-00002.gguf'; cases.append(c)
        c=split_config(); c['model_file']='qwen/model-00001-of-00000.gguf'; cases.append(c)
        c=split_config(); c['model_file']='qwen/model-00001-of-00001.gguf'; cases.append(c)
        c=split_config(); c['model_files']['qwen/model-00002-of-00002.gguf']=True; cases.append(c)
        c=config(); c['model_files']['../outside.gguf']='b'*64; cases.append(c)
        c=config(); c['model_files']['model.gguf']='not-a-checksum'; cases.append(c)
        c=config(); c['model_sha256']='b'*64; cases.append(c)
        c=config(); c['model_files']=[]; cases.append(c)
        for c in cases:
            with self.subTest(config=c), self.assertRaises(ValueError):
                box.validated(c)

    def test_duplicate_and_unknown_configuration_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.json'
            path.write_text('{"port":8080,"port":8081}')
            with self.assertRaisesRegex(ValueError,'Duplicate'):
                box.read_config(path)
            path.write_text('{"model_files":{"model.gguf":"a","model.gguf":"b"}}')
            with self.assertRaisesRegex(ValueError,'Duplicate'):
                box.read_config(path)
            c=config(); c['extra']='ignored?'
            path.write_text(json.dumps(c))
            with self.assertRaisesRegex(ValueError,'exactly'):
                box.read_config(path)

    def test_model_symlink_cannot_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'models'; root.mkdir()
            secret=Path(directory)/'secret.gguf'; secret.write_text('secret')
            (root/'model.gguf').symlink_to(secret)
            c=config(); c['model_dir']=str(root)
            with self.assertRaisesRegex(ValueError,'outside'):
                box.model_path(c)

    def test_model_directory_symlink_cannot_broaden_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory).resolve()
            dedicated=home/'.local/share/ai-box/models'
            dedicated.parent.mkdir(parents=True)
            dedicated.symlink_to(home, target_is_directory=True)
            c=config(); c['model_dir']=str(dedicated)
            with patch.object(Path, 'home', return_value=home):
                with self.assertRaisesRegex(ValueError, 'broad host mounts'):
                    box.validated(c)

    def test_checksum_mismatch_stops_before_pulling_or_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            model=Path(directory)/'model.gguf'; model.write_bytes(b'wrong bytes')
            with patch.object(box,'read_config',return_value=config()), \
                 patch.object(box,'doctor',return_value={'ready':True,'mem_total_gib':128,
                     'render_devices':[{'path':'/dev/dri/renderD128','accessible':True,'vendor':'0x1002'}]}), \
                 patch.object(box,'model_path',return_value=model),patch.object(box,'run') as run:
                with self.assertRaisesRegex(ValueError,'checksum mismatch'):
                    box.install('unused.json',False)
                run.assert_not_called()

    def test_capacity_stops_before_accessing_model(self):
        with patch.object(box,'read_config',return_value=config()), \
             patch.object(box,'doctor',return_value={'ready':True,'mem_total_gib':32,
                 'render_devices':[{'path':'/dev/dri/renderD128','accessible':True,'vendor':'0x1002'}]}), \
             patch.object(box,'model_path') as model:
            with self.assertRaisesRegex(ValueError,'physical memory'):
                box.install('unused.json',False)
            model.assert_not_called()


class SplitIntegrity(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.root = self.home / '.local/share/ai-box/models'
        self.c = split_config()
        self.c['model_dir'] = str(self.root)
        for name in self.c['model_files']:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            data = name.encode()
            path.write_bytes(data)
            self.c['model_files'][name] = hashlib.sha256(data).hexdigest()
        home_patch = patch.object(Path, 'home', return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)

    def install(self):
        report = dict(ready=True, mem_total_gib=128, render_devices=[
            dict(path='/dev/dri/renderD128', accessible=True, vendor='0x1002')])
        with patch.object(box, 'read_config', return_value=self.c), \
             patch.object(box, 'doctor', return_value=report), \
             patch.object(box, 'run', return_value=SimpleNamespace(
                 stdout='--load-mode --flash-attn --api-key-file --no-webui --no-slots', stderr='')) as run:
            try:
                box.install('unused.json', True)
            except ValueError:
                run.assert_not_called()
                self.assertFalse((self.home / '.config/ai-box/api-key').exists())
                self.assertFalse((self.home / '.config/containers/systemd/ai-box-llama.container').exists())
                raise
            return run

    def test_all_shards_verified_before_service_install(self):
        run = self.install()
        unit = (self.home / '.config/containers/systemd/ai-box-llama.container').read_text()
        self.assertIn('--model /models/qwen/model-00001-of-00002.gguf ', unit)
        self.assertTrue((self.home / '.config/ai-box/api-key').is_file())
        run.assert_any_call(['systemctl', '--user', 'restart', 'ai-box-llama.service'])

    def test_auto_budget_is_resolved_before_installing_quadlet(self):
        self.c.update(memory_gib='auto', reserve_gib=4)
        self.install()
        unit = (self.home / '.config/containers/systemd/ai-box-llama.container').read_text()
        self.assertIn('MemoryMax=124G', unit)
        self.assertNotIn('MemoryMax=auto', unit)

    def test_corrupt_later_shard_stops_install(self):
        (self.root / 'qwen/model-00002-of-00002.gguf').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch.*00002'):
            self.install()

    def test_missing_later_shard_stops_install(self):
        (self.root / 'qwen/model-00002-of-00002.gguf').unlink()
        with self.assertRaisesRegex(ValueError, 'absent'):
            self.install()

    def test_later_shard_symlink_cannot_escape_model_directory(self):
        shard = self.root / 'qwen/model-00002-of-00002.gguf'
        shard.unlink()
        outside = self.home / 'outside.gguf'
        outside.write_bytes(b'not in model directory')
        shard.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.install()


class SmokeAuthentication(unittest.TestCase):
    def test_authenticated_redirect_never_reaches_second_endpoint(self):
        import http.server
        import threading
        received=[]
        key='dummy-key-'+'a'*40
        class Destination(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.headers.get('Authorization'))
                self.send_response(200); self.end_headers()
            def log_message(self, *args):
                pass
        destination=http.server.ThreadingHTTPServer(('127.0.0.1',0),Destination)
        class Source(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path=='/health':
                    self.send_response(200); self.end_headers(); self.wfile.write(b'{"status":"ok"}')
                elif self.headers.get('Authorization')=='Bearer '+key:
                    self.send_response(302)
                    self.send_header('Location',f'http://127.0.0.1:{destination.server_port}/leak')
                    self.end_headers()
                else:
                    self.send_response(401); self.end_headers()
            def log_message(self, *args):
                pass
        source=http.server.ThreadingHTTPServer(('127.0.0.1',0),Source)
        threads=[threading.Thread(target=server.serve_forever,daemon=True) for server in (source,destination)]
        for thread in threads:
            thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                home=Path(directory).resolve()
                (home/'.config/ai-box').mkdir(parents=True)
                (home/'.config/ai-box/api-key').write_text(key)
                c=config(); c.update(port=source.server_port, model_dir=str(home/'.local/share/ai-box/models'))
                with patch.object(Path,'home',return_value=home):
                    with self.assertRaisesRegex(ValueError,'redirects are forbidden'):
                        box.smoke(c)
                self.assertEqual(received, [])
        finally:
            for server in (source,destination):
                server.shutdown(); server.server_close()
            for thread in threads:
                thread.join()

    def test_unauthenticated_api_is_rejected_as_unsafe(self):
        import http.server
        import threading
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok","data":[]}')
            def log_message(self,*args):
                pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        try:
            c=config(); c['port']=server.server_port
            with self.assertRaisesRegex(ValueError,'unauthenticated'):
                box.smoke(c)
        finally:
            server.shutdown(); server.server_close(); worker.join()

    def test_smoke_requires_authenticated_completion(self):
        import http.server
        import threading
        key='test-key-'+'a'*48
        class Handler(http.server.BaseHTTPRequestHandler):
            def respond(self,value,status=200):
                self.send_response(status)
                self.send_header('Content-Type','application/json')
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())
            def do_GET(self):
                if self.path=='/health':
                    self.respond({'status':'ok'})
                elif self.headers.get('Authorization')=='Bearer '+key:
                    self.respond({'data':[{'id':'local-model'}]})
                else:
                    self.respond({'error':'unauthorized'},401)
            def do_POST(self):
                if self.headers.get('Authorization')!='Bearer '+key:
                    self.respond({'error':'unauthorized'},401)
                    return
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if data['model']!='local-model':
                    self.respond({'error':'bad model'},400)
                    return
                self.respond({'choices':[{'message':{'content':'OK'}}]})
            def log_message(self,*args):
                pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                home=Path(directory).resolve()
                (home/'.config/ai-box').mkdir(parents=True)
                (home/'.config/ai-box/api-key').write_text(key)
                c=config(); c['port']=server.server_port
                c['model_dir']=str(home/'.local/share/ai-box/models')
                with patch.object(Path,'home',return_value=home), \
                     patch.dict('os.environ',{'http_proxy':'http://127.0.0.1:1','HTTP_PROXY':'http://127.0.0.1:1','no_proxy':'','NO_PROXY':''}):
                    box.smoke(c)
        finally:
            server.shutdown(); server.server_close(); worker.join()


if __name__=='__main__':
    unittest.main()
