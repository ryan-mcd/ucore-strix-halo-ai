"""Boundary tests for configuration rendering and model integrity."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
loader=importlib.machinery.SourceFileLoader('ai_box',str(ROOT/'system_files/usr/bin/ai-box'))
spec=importlib.util.spec_from_loader(loader.name,loader)
box=importlib.util.module_from_spec(spec)
loader.exec_module(box)


def config():
    c=json.loads((ROOT/'config/ai-box.example.json').read_text())
    c.update(image='docker.io/kyuz0/amd-strix-halo-toolboxes@sha256:'+'a'*64,model_sha256='b'*64)
    return c


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

    def test_rejects_unverified_split_models(self):
        c=config(); c['model_file']='model-00001-of-00002.gguf'
        with self.assertRaisesRegex(ValueError,'single-file'):
            box.validated(c)

    def test_duplicate_and_unknown_configuration_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.json'
            path.write_text('{"port":8080,"port":8081}')
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


class SmokeAuthentication(unittest.TestCase):
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
