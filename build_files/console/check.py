"""Check installed console policy without changing the builder's console."""
from pathlib import Path
import subprocess


unit = Path('/usr/lib/systemd/system/ai-box-console-awake.service')
enabled = Path('/etc/systemd/system/multi-user.target.wants') / unit.name
assert unit.is_file()
assert enabled.is_symlink() and enabled.resolve() == unit
subprocess.run(['systemd-analyze', 'verify', str(unit)], check=True)
subprocess.run(['/usr/bin/setterm', '--help'], check=True, stdout=subprocess.DEVNULL)
print('Console policy: installed/enabled unit, systemd syntax and setterm passed; live display behavior is untested.')
