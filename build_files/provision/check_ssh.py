"""Real SSH/PAM and SELinux policy lookup; kernel labels/systemd/GPU/Podman simulated."""
import hashlib
import grp
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import time
import tempfile

if os.geteuid() != 0 or not Path('/.dockerenv').exists():
    raise SystemExit('Run only as root inside a disposable Docker container')
import importlib.util
import contextlib
import io
spec = importlib.util.spec_from_file_location('image_provision', '/usr/share/ai-box/provision.py')
provision = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provision)
base = Path(tempfile.mkdtemp(prefix='image-provision-test-'))
operator = os.environ.get('TEST_OPERATOR', 'operator_27')
serving = os.environ.get('TEST_SERVING', 'serving_83')
if any(not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', name) or name == 'root'
       for name in (operator, serving)) or operator == serving:
    raise SystemExit('Test accounts must be distinct non-root Linux usernames')
provision.DRM = Path('/tmp/fake-drm')
provision.DEVICES = Path('/tmp/fake-dri')

# Keep actual policy lookup/registration. A Docker container cannot qualify
# application or kernel enforcement of host SELinux labels.
host_run = provision.run


def simulated_label_verification(arguments, **kwargs):
    if arguments[:2] == ['matchpathcon', '-V']:
        return subprocess.CompletedProcess(arguments, 0, stdout='', stderr='')
    return host_run(arguments, **kwargs)


provision.run = simulated_label_verification


def commission(*arguments):
    import sys
    previous = sys.argv
    sys.argv = ['ai-box-provision', *arguments]
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = provision.main()
    finally:
        sys.argv = previous
    return code, output.getvalue()



def run(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed: {result.stderr}')
    return result


run(['groupadd', '-f', 'render'])
for name in (operator, serving):
    args = ['useradd', '-m', '-d', '/var/home/' + name, '-s', '/bin/bash', '-p', '*']
    run(args + [name])
    home = Path(pwd.getpwnam(name).pw_dir)
    home.chmod(0o700)
    (home / '.ssh').mkdir(mode=0o700)
    if name == operator:
        (home / '.ssh/config').write_text('Host unrelated-host\n    HostName example.invalid\n')
    else:
        (home / '.ssh/authorized_keys').write_text('# Preserve unrelated authorizations\n')
    for path in [home / '.ssh', *(home / '.ssh').iterdir()]:
        account = pwd.getpwnam(name)
        os.chown(path, account.pw_uid, account.pw_gid)
        if path.is_file():
            path.chmod(0o600)

# Force the allocation branch while retaining other account ranges.
for filename in ('subuid', 'subgid'):
    path = Path('/etc') / filename
    path.write_text(''.join(line + '\n' for line in path.read_text().splitlines()
                            if not line.startswith(serving + ':')))

# Simulate only the GPU device enumeration; keep real group identity handling.
Path('/tmp/fake-drm/renderD128/device').mkdir(parents=True)
Path('/tmp/fake-drm/renderD128/device/vendor').write_text('0x1002\n')
Path('/tmp/fake-dri').mkdir()
node = Path('/tmp/fake-dri/renderD128')
node.touch()
os.chown(node, 0, grp.getgrnam('render').gr_gid)


def wrapper(name, contents):
    path = Path(shutil.which(name) or '/usr/bin/' + name)
    path.write_text('#!/bin/bash\nset -eu\n' + contents + '\n')
    path.chmod(0o755)


wrapper('getenforce', 'echo Enforcing')
wrapper('restorecon', 'exit 0')
wrapper('loginctl', '''if [ "$1" = enable-linger ]; then
  touch /tmp/linger-enabled
else
  test -f /tmp/linger-enabled
  echo yes
fi''')
wrapper('systemctl', '''if [ "$1" = --user ]; then
  printf 'LoadState=not-found\nActiveState=inactive\n'
fi''')
wrapper('podman', '''test "$(id -u)" -ne 0
test "$1" = info
test -x .
echo true''')

# Keep real SSH and the actual JSON backend; simulate only target preflight.
helper = Path('/usr/bin/ai-box').read_text()
helper = helper.replace('def doctor():\n', '''def doctor():
    return {'rootless': os.geteuid() != 0, 'ready': not Path('/tmp/fail-preflight').exists(),
            'mem_total_gib': 128, 'render_devices': []}

def real_doctor_unused_in_container():
''', 1)
Path('/usr/bin/ai-box').write_text(helper)
run(['ssh-keygen', '-A'])
Path('/run/sshd').mkdir(exist_ok=True)
config = base / 'sshd_config'
config.write_text('''Port 22
ListenAddress 127.0.0.1
ListenAddress ::1
HostKey /etc/ssh/ssh_host_ed25519_key
Include /etc/ssh/sshd_config
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AuthorizedKeysFile .ssh/authorized_keys
''')
run(['/usr/sbin/sshd', '-t', '-f', str(config)])
daemon = subprocess.Popen(['/usr/sbin/sshd', '-D', '-f', str(config), '-E', '/tmp/sshd-test.log'])
try:
    # Readiness wait is bounded and never reaches an external endpoint.
    import socket
    for _ in range(50):
        try:
            with socket.create_connection(('127.0.0.1', 22), timeout=0.1):
                break
        except OSError:
            time.sleep(0.02)
    else:
        raise AssertionError('Test SSH daemon did not start')
    # Match a sudo invocation from the operator's private home. The serving
    # account cannot use this directory, including for Podman namespace setup.
    os.chdir(pwd.getpwnam(operator).pw_dir)
    inaccessible = subprocess.run(['runuser', '-u', serving, '--', 'test', '-x', '.'])
    assert inaccessible.returncode != 0, 'Fixture caller directory must be inaccessible to serving'
    code, message = commission('--operator', operator, '--serving', serving)
    assert code == 0, message
    operator_home = Path(pwd.getpwnam(operator).pw_dir)
    serving_home = Path(pwd.getpwnam(serving).pw_dir)
    key = operator_home / '.ssh/ai-box-service'
    first = hashlib.sha256(key.read_bytes()).hexdigest()
    mappings = {name: (Path('/etc') / name).read_text() for name in ('subuid', 'subgid')}
    code, message = commission('--repair')
    assert code == 0, message
    code, message = commission('--check')
    assert code == 0, message
    for home in (operator_home, serving_home):
        contexts = run(['matchpathcon', '-n', str(home / '.ssh'),
                        str(home / '.ssh/authorized_keys')]).stdout.splitlines()
        assert all(context.split(':')[2] == 'ssh_home_t' for context in contexts)
    assert (operator_home / '.ssh/config').read_text() == 'Host unrelated-host\n    HostName example.invalid\n'
    assert first == hashlib.sha256(key.read_bytes()).hexdigest(), 'Retry rotated management key'
    assert mappings == {name: (Path('/etc') / name).read_text() for name in mappings}
    assert key.stat().st_uid == pwd.getpwnam(operator).pw_uid
    assert key.stat().st_mode & 0o777 == 0o600
    assert Path('/var/lib/ai-box/provisioned.json').exists()
    authorized = (serving_home / '.ssh/authorized_keys').read_text()
    assert '# Preserve unrelated authorizations' in authorized
    assert 'restrict,from="127.0.0.1,::1",command=' in authorized
    assert 'wheel' not in run(['id', '-nG', serving]).stdout.split()
    command = ['runuser', '-u', operator, '--', 'ssh', '-F', str(operator_home / '.ssh/ai-box-config'), '-T', '-o', 'BatchMode=yes', f'{serving}@localhost',
               'touch', '/tmp/escaped-forced-command']
    response = run(command, input='{"operation":"inspect","args":{}}\n')
    assert json.loads(response.stdout)['ok']
    assert not Path('/tmp/escaped-forced-command').exists()
    # Failing commissioning must never write the completion marker.
    Path('/var/lib/ai-box/provisioned.json').unlink()
    Path('/tmp/fail-preflight').touch()
    code, message = commission()
    assert code != 0
    assert not Path('/var/lib/ai-box/provisioned.json').exists()
    Path('/tmp/fail-preflight').unlink()
    # Reject a service account accidentally granted administrator membership.
    run(['usermod', '-aG', 'wheel', serving])
    code, message = commission()
    assert code != 0
    assert not Path('/var/lib/ai-box/provisioned.json').exists()
    print(f'PASS ({operator}/{serving}): enrollment/repair/check, preserved keys/config, disjoint IDs, permissions, real forced-command SSH/PAM and SELinux policy lookup; kernel labels/systemd/GPU/Podman simulated.')
finally:
    daemon.terminate()
    daemon.wait(timeout=10)
