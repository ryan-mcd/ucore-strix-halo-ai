"""Optional real-SSH delegation test; run only as root in a disposable container."""
import json
import os
from pathlib import Path
import pwd
import subprocess
import tempfile

if os.geteuid() != 0 or not (Path('/.dockerenv').exists() or Path('/run/.containerenv').exists()):
    raise SystemExit('This test creates users/host keys and must run only in a disposable container as root')


def run(arguments, **kwargs):
    return subprocess.run(arguments, check=True, capture_output=True, text=True, **kwargs)


with tempfile.TemporaryDirectory(prefix='cockpit-ssh-') as temporary:
    root = Path(temporary)
    root.chmod(0o755)
    operator, serving = 'ai-box-test-op', 'ai-box-test-service'
    for account in (operator, serving):
        run(['useradd', '-m', '-d', str(root / account), '-s', '/bin/bash', account])
        # Password login is disabled by the test SSH config; permit public-key auth.
        run(['usermod', '-p', '*', account])
        (root / account / '.ssh').mkdir(mode=0o700)
    key = root / operator / '.ssh/id_ed25519'
    host_key = root / 'host_ed25519'
    for path in (key, host_key):
        run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(path)])
    authorized = root / serving / '.ssh/authorized_keys'
    authorized.write_text('restrict,from="127.0.0.1,::1",command="/usr/lib/ai-toolbox-cockpit/bin/python -I /usr/share/ai-box/service.py" ' + key.with_suffix('.pub').read_text())
    known = root / operator / '.ssh/known_hosts'
    known.write_text('localhost ' + host_key.with_suffix('.pub').read_text())
    for account in (operator, serving):
        identity = pwd.getpwnam(account)
        home = root / account
        for path in [home, *home.rglob('*')]:
            os.chown(path, identity.pw_uid, identity.pw_gid)
        if account == serving:
            authorized.chmod(0o600)
    Path('/run/sshd').mkdir(exist_ok=True, mode=0o755)
    config = root / 'sshd_config'
    config.write_text(f'''Port 22
ListenAddress 127.0.0.1
HostKey {host_key}
PidFile {root / 'sshd.pid'}
UsePAM no
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AuthorizedKeysFile .ssh/authorized_keys
''')
    log = root / 'sshd.log'
    daemon = subprocess.Popen(['/usr/sbin/sshd', '-D', '-f', str(config), '-E', str(log)])
    try:
        # The synchronous configuration test catches errors before the client runs.
        run(['/usr/sbin/sshd', '-t', '-f', str(config)])
        script = root / 'client.py'
        script.write_text(f'''
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, '/usr/share/ai-box')
from service_panel import request_service
result = request_service('{serving}', 'inspect', {{}})
assert result['account'] == '{serving}'
assert result['preflight']['rootless'] is True
assert result['preflight']['ready'] is False  # No GPU/systemd qualification in this container.
command = ['ssh', '-T', '-o', 'BatchMode=yes', '{serving}@localhost', 'touch', '{root / 'escape'}']
result = subprocess.run(command, input=json.dumps({{'operation':'inspect','args':{{}}}}), capture_output=True, text=True)
assert result.returncode == 0, result.stderr
assert json.loads(result.stdout)['ok'] is True
assert not Path('{root / 'escape'}').exists(), 'SSH original command escaped the forced command'
print('Real SSH: operator-to-serving-account JSON delegation and forced-command confinement passed.')
''')
        script.chmod(0o644)
        result = run(['runuser', '-u', operator, '--', '/usr/lib/ai-toolbox-cockpit/bin/python', '-I', str(script)])
        print(result.stdout.strip())
    finally:
        daemon.terminate()
        daemon.wait(timeout=10)
