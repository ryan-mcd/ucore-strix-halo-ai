"""Fixed JSON operations for Cockpit's unprivileged serving-account connection."""
import contextlib
import fcntl
import importlib.machinery
import importlib.util
import io
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

loader = importlib.machinery.SourceFileLoader('managed_ai_box', str(Path(__file__).resolve().parents[2] / 'bin/ai-box'))
spec = importlib.util.spec_from_loader(loader.name, loader)
runtime = importlib.util.module_from_spec(spec)
loader.exec_module(runtime)


def captured_run(arguments, **kwargs):
    # Child processes inherit real file descriptors, not redirect_stdout's
    # StringIO. Capture their output so the SSH response remains one JSON object.
    if not kwargs.get('capture_output'):
        kwargs.setdefault('stdout', subprocess.PIPE)
        kwargs.setdefault('stderr', subprocess.PIPE)
    return subprocess.run(arguments, check=True, text=True, **kwargs)


runtime.run = captured_run

UNIT = 'ai-box-llama.service'
REPOSITORY = 'docker.io/kyuz0/amd-strix-halo-toolboxes'
CHANNELS = {'vulkan-radv'}
SETTINGS = {'render_device', 'port', 'context', 'memory_gib', 'reserve_gib'}
REVISION = re.compile(r'[0-9a-f]{40}\Z')
REPO = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
MAX_REQUEST = 1024 * 1024


def fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names):
        raise ValueError('Invalid operation fields')


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field')
        result[key] = value
    return result


def catalog(repo, revision='main'):
    if not isinstance(repo, str) or not REPO.fullmatch(repo):
        raise ValueError('Choose a Hugging Face owner/repository')
    if not isinstance(revision, str) or (revision != 'main' and not REVISION.fullmatch(revision)):
        raise ValueError('Model revision must be an immutable commit')
    opener = urllib.request.build_opener(runtime.NoRedirect(), urllib.request.ProxyHandler({}))
    url = f'https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true'
    with opener.open(url, timeout=30) as response:
        payload = response.read(10 * MAX_REQUEST + 1)
    if len(payload) > 10 * MAX_REQUEST:
        raise ValueError('Model metadata exceeds the size limit')
    data = json.loads(payload, object_pairs_hook=unique)
    commit = data['sha']
    if not REVISION.fullmatch(commit) or (revision != 'main' and commit != revision):
        raise ValueError('Publisher metadata did not match the requested revision')
    files = {}
    for entry in data['siblings']:
        name = entry['rfilename']
        if (not isinstance(name, str) or not runtime.SAFE_PATH.fullmatch(name) or
                not name.endswith('.gguf') or Path(name).is_absolute() or
                '..' in Path(name).parts or '~' in name or Path(name).as_posix() != name or
                re.search(r'(^|/)(mmproj|mtp|dflash|draft)(/|[-_])', name, re.I)):
            continue
        digest = entry.get('lfs', {}).get('sha256', '')
        size = entry.get('size')
        if re.fullmatch(r'[0-9a-f]{64}', digest) and type(size) is int and size > 0:
            files[name] = {'sha256': digest, 'bytes': size}
    variants = []
    for name in sorted(files):
        split = runtime.SPLIT_RE.fullmatch(name)
        names = {name}
        if split:
            prefix, index, count = split.groups()
            if index != '00001' or not 2 <= int(count) <= 65535:
                continue
            names = {f'{prefix}-{i:05d}-of-{int(count):05d}.gguf' for i in range(1, int(count) + 1)}
        if names <= files.keys():
            variants.append(dict(first=name, files={n: files[n]['sha256'] for n in sorted(names)},
                                 bytes=sum(files[n]['bytes'] for n in names)))
    if not variants:
        raise ValueError('No complete GGUF sets with publisher SHA-256 metadata were found')
    return dict(repo=repo, revision=commit, variants=variants)


def selected_model(selection):
    fields(selection, {'repo', 'revision', 'first'})
    if not REVISION.fullmatch(selection['revision']):
        raise ValueError('Select an immutable model revision first')
    result = catalog(selection['repo'], selection['revision'])
    for variant in result['variants']:
        if variant['first'] == selection['first']:
            return result, variant
    raise ValueError('Selected GGUF set is absent or incomplete at this revision')


def model_config(model, variant, settings, image=None):
    fields(settings, SETTINGS)
    variant_id = hashlib.sha256(variant['first'].encode()).hexdigest()
    return runtime.validated(dict(image=image or REPOSITORY + '@sha256:' + '0' * 64,
        model_dir=str(Path.home() / '.local/share/ai-box/models' / model['repo'] / model['revision'] / variant_id),
        model_file=variant['first'], model_files=variant['files'], **settings))


def download(selection):
    model, variant = selected_model(selection)
    config = model_config(model, variant, dict(render_device='/dev/dri/renderD128', port=8080,
        context=8192, memory_gib='auto', reserve_gib=4))
    destination = Path(config['model_dir'])
    if destination.exists():
        runtime.verify_model_files(config)
        return {'message': 'This immutable model set is already downloaded and verified.'}
    staging = Path.home().resolve() / '.local/share/ai-box/models/.incoming' / model['repo'] / model['revision'] / destination.name
    staged = runtime.validated(dict(config, model_dir=str(staging)))
    if staging.resolve() != staging or (staging.exists() and any(p.is_symlink() for p in staging.rglob('*'))):
        raise ValueError('Download staging contains a symlink; administrator inspection is required')
    staging.mkdir(parents=True, exist_ok=True, mode=0o700)
    request = dict(repo=model['repo'], revision=model['revision'], files=list(variant['files']), directory=str(staging))
    result = subprocess.run(['/usr/lib/ai-toolbox-cockpit/bin/python', '-I',
        str(Path(__file__).with_name('hub_download.py'))], input=json.dumps(request),
        text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise ValueError('Download failed. Check network and free storage, then retry to resume the staged download.')
    runtime.verify_model_files(staged)
    shutil.rmtree(staging / '.cache', ignore_errors=True)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Never overwrite an installed revision, including on a concurrent request.
    if destination.exists():
        runtime.verify_model_files(config)
        return {'message': 'Model set was downloaded by another request and verified.'}
    staging.rename(destination)
    return {'message': 'All model files downloaded, SHA-256 verified and promoted into serving storage.'}


def resolve_runtime(channel):
    if channel not in CHANNELS:
        raise ValueError('This managed profile supports Vulkan RADV; other engines need a reviewed service profile')
    tag = f'{REPOSITORY}:{channel}'
    runtime.run(['podman', 'pull', tag], capture_output=True)
    records = json.loads(runtime.run(['podman', 'image', 'inspect', tag], capture_output=True).stdout)
    digests = sorted(d for d in records[0].get('RepoDigests', []) if runtime.IMAGE_RE.fullmatch(d))
    if not digests:
        raise ValueError('No approved-repository digest was returned for the selected runtime')
    return digests[0]


def status():
    result = subprocess.run(['systemctl', '--user', 'show', UNIT, '--no-pager',
        '--property=LoadState,ActiveState,SubState,Result'], capture_output=True, text=True)
    state = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if result.returncode:
        state = {'ActiveState': 'unavailable', 'Result': 'Cannot reach the serving account user manager'}
    linger = subprocess.run(['loginctl', 'show-user', str(os.getuid()), '-p', 'Linger', '--value'],
                            capture_output=True, text=True)
    return dict(service=state, linger=linger.returncode == 0 and linger.stdout.strip() == 'yes')


def inspect():
    result = dict(status(), preflight=runtime.doctor(), account=Path.home().name, config=None)
    if runtime.DEFAULT_CONFIG.exists():
        try:
            result['config'] = runtime.validated(runtime.read_config(runtime.DEFAULT_CONFIG))
        except (ValueError, OSError) as error:
            result['config_error'] = str(error)
    return result


def prepare(args):
    fields(args, {'channel', 'model', 'config', 'settings'})
    fields(args['settings'], SETTINGS)
    if args['model'] is not None:
        model, variant = selected_model(args['model'])
        config = model_config(model, variant, args['settings'])
    elif args['config'] is not None:
        config = runtime.validated(dict(runtime.validated(args['config']), **args['settings']))
    else:
        raise ValueError('Choose a model and download it, or connect to an existing configured service')
    report = runtime.doctor()
    if not report['ready']:
        raise ValueError('Serving host preflight failed; inspect its GPU access, SELinux and rootless tools')
    runtime.verify_model_files(config)
    config['image'] = resolve_runtime(args['channel'])
    return dict(config=config, unit=runtime.render(runtime.resolve_memory_budget(config, report)), preflight=report)


def private_write(path, data):
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError('Private configuration must not be a symlink')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor, name = tempfile.mkstemp(prefix='.cockpit-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def install(config):
    config = runtime.validated(config)
    if not status()['linger']:
        raise ValueError('An administrator must enable linger for the serving account before persistent installation')
    path = runtime.DEFAULT_CONFIG
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('Private configuration must not be a symlink')
    previous = path.read_bytes() if path.exists() else None
    if previous is not None:
        private_write(path.with_suffix('.json.previous'), previous)
    private_write(path, (json.dumps(config, indent=2) + '\n').encode())
    try:
        runtime.install(path, True)
    except Exception:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            private_write(path, previous)
        raise
    return dict(status(), message='Service installed and startup requested. Wait for model readiness, then run the API check.')


def dispatch(request):
    fields(request, {'operation', 'args'})
    operation, args = request['operation'], request['args']
    if operation == 'inspect':
        fields(args, set())
        return inspect()
    if operation == 'catalog':
        fields(args, {'repo'})
        return catalog(args['repo'])
    if operation == 'download':
        fields(args, {'model'})
        return download(args['model'])
    if operation == 'prepare':
        return prepare(args)
    if operation == 'install':
        fields(args, {'config'})
        return install(args['config'])
    if operation in {'start', 'stop', 'restart'}:
        fields(args, set())
        if operation != 'stop' and not status()['linger']:
            raise ValueError('Enable linger for the serving account before starting persistent inference')
        runtime.run(['systemctl', '--user', operation, UNIT], capture_output=True)
        return dict(status(), message=f'{operation.capitalize()} requested for the inference service.')
    if operation == 'smoke':
        fields(args, set())
        runtime.smoke(runtime.read_config(runtime.DEFAULT_CONFIG))
        return {'message': 'API health, authentication rejection and completion checks passed.'}
    if operation == 'logs':
        fields(args, set())
        result = runtime.run(['journalctl', '--user', '-u', UNIT, '-n', '60', '--no-pager'], capture_output=True)
        return {'logs': result.stdout[-16000:]}
    raise ValueError('Unknown management operation')


@contextlib.contextmanager
def operation_lock(request):
    operation = request.get('operation') if isinstance(request, dict) else None
    if operation not in {'download', 'prepare', 'install', 'start', 'stop', 'restart'}:
        yield
        return
    directory = Path.home() / '.config/ai-box'
    if directory.is_symlink():
        raise ValueError('Private management directory must not be a symlink')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = '.download.lock' if operation == 'download' else '.service.lock'
    descriptor = os.open(directory / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('Another management operation is in progress; wait and retry') from error
        yield
    finally:
        os.close(descriptor)


def main():
    os.umask(0o077)
    try:
        if os.geteuid() == 0:
            raise ValueError('Management operations require the unprivileged inference account')
        payload = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(payload) > MAX_REQUEST:
            raise ValueError('Management request exceeds the size limit')
        request = json.loads(payload, object_pairs_hook=unique)
        # Helper output may contain progress, but the RPC response is one JSON object.
        with operation_lock(request), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = dispatch(request)
        response = {'ok': True, 'result': result}
    except (ValueError, OSError, KeyError, TypeError, IndexError, subprocess.SubprocessError) as error:
        response = {'ok': False, 'error': str(error)}
    print(json.dumps(response))
    return 0 if response['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
