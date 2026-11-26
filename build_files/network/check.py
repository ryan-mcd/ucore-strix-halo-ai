"""Check the installed network helper and actual Quadlet generation without starting containers."""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

assert shutil.which('pasta'), 'The passt package must provide pasta'
help_result = subprocess.run(['pasta', '--help'], capture_output=True, text=True)
help_text = help_result.stdout + help_result.stderr
for option in ('--no-map-gw', '--map-guest-addr'):
    assert option in help_text, f'Installed pasta lacks required option: {option}'

loader = importlib.machinery.SourceFileLoader('network_ai_box', '/usr/bin/ai-box')
spec = importlib.util.spec_from_loader(loader.name, loader)
box = importlib.util.module_from_spec(spec)
loader.exec_module(box)
config = dict(image='docker.io/kyuz0/amd-strix-halo-toolboxes@sha256:' + 'a' * 64,
              model_dir=str(Path.home() / '.local/share/ai-box/models/network-fixture'),
              model_file='fixture.gguf', model_files={'fixture.gguf': 'b' * 64},
              render_device='/dev/dri/renderD128', port=8080, context=8192,
              memory_gib=48, reserve_gib=4)
with tempfile.TemporaryDirectory(prefix='ai-box-network-') as temporary:
    Path(temporary, 'ai-box-llama.container').write_text(box.render(config))
    result = subprocess.run(['/usr/lib/systemd/system-generators/podman-system-generator',
                             '--user', '--dryrun'],
                            env=dict(os.environ, QUADLET_UNIT_DIRS=temporary),
                            capture_output=True, text=True, check=True)
    starts = [line.split('=', 1)[1] for line in result.stdout.splitlines() if line.startswith('ExecStart=')]
    assert len(starts) == 1, 'Quadlet did not generate exactly one service start command'
    arguments = shlex.split(starts[0])
    assert arguments[arguments.index('--network') + 1] == 'pasta:--no-map-gw,--map-guest-addr,none', starts[0]
    assert '--publish' in arguments and '127.0.0.1:8080:8080' in arguments, starts[0]
    assert not any('slirp4netns' in argument for argument in arguments)
print('Network: installed pasta options and rootless Quadlet generation passed; live forwarding is separate acceptance.')
