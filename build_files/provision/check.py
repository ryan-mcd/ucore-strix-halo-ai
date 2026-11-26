"""Validate the installed commissioning entry point without provisioning the build."""
from pathlib import Path
import shutil
import subprocess

for command in ('semanage', 'matchpathcon', 'restorecon'):
    assert shutil.which(command), f'Missing commissioning dependency: {command}'

unit = Path('/usr/lib/systemd/system/ai-box-provision.service')
assert unit.is_file()
enabled = Path('/etc/systemd/system/multi-user.target.wants/ai-box-provision.service')
assert enabled.is_symlink() and enabled.resolve() == unit
text = unit.read_text()
for required in ('ConditionPathExists=/etc/ai-box/operator-account',
                 'ConditionPathExists=/etc/ai-box/serving-account',
                 'ExecStart=/usr/bin/ai-box-provision', 'TimeoutStartSec=180'):
    assert required in text
assert '/etc/ucore-autorebase/signed' not in text
subprocess.run(['systemd-analyze', 'verify', str(unit)], check=True)
subprocess.run(['/usr/bin/ai-box-provision', '--help'], check=True, stdout=subprocess.DEVNULL)
for path in ('/etc/ai-box/operator-account', '/etc/ai-box/serving-account',
             '/var/lib/ai-box/provisioned.json'):
    assert not Path(path).exists(), 'Machine-specific enrollment was embedded in the image'
print('Image provisioning: installed/enabled conditional unit and account-free helper passed.')
