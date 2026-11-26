"""Check the image-owned Bifrost binary, units and firewall without opening ports."""
import os
from pathlib import Path
import subprocess
import tempfile

binary = '/usr/libexec/ai-box-bifrost'
assert Path(binary).stat().st_uid == 0 and Path(binary).stat().st_mode & 0o022 == 0
help_result = subprocess.run([binary, '-h'], capture_output=True, text=True)
assert help_result.returncode == 0, help_result.stderr
for option in ('-app-dir', '-host', '-port', '-log-level'):
    assert option in help_result.stdout + help_result.stderr, option
guard = Path('/usr/lib/systemd/system/ai-box-api-firewall.service')
assert Path('/etc/systemd/system/multi-user.target.wants', guard.name).resolve() == guard
subprocess.run(['systemd-analyze', 'verify', str(guard)], check=True)
gateway = Path('/usr/lib/systemd/user/ai-box-bifrost.service')
assert Path('/etc/systemd/user/default.target.wants', gateway.name).resolve() == gateway
with tempfile.TemporaryDirectory() as temporary:
    Path(temporary, 'ai-box-llama.service').write_text('[Service]\nExecStart=/usr/bin/true\n')
    subprocess.run(['systemd-analyze', '--user', 'verify', str(gateway)],
                   env=dict(os.environ, XDG_RUNTIME_DIR=temporary,
                            SYSTEMD_UNIT_PATH=temporary + ':'), check=True)
assert Path('/usr/share/ai-box/api-ingress.nft').is_file()
subprocess.run(['firewall-offline-cmd', '--check-config'], check=True)
print('Bifrost: native binary, enabled units and firewalld configuration passed; live gateway/ingress checks are separate.')
