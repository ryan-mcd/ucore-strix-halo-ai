"""Image-owned root commissioning. Account identities come only from root configuration."""
import argparse
import contextlib
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import stat
import subprocess
import sys

CONFIG = Path('/etc/ai-box')
STATE = Path('/var/lib/ai-box')
LOCK = Path('/run/ai-box-provision.lock')
DRM = Path('/sys/class/drm')
DEVICES = Path('/dev/dri')
GROUPS = Path('/etc/group')
SUBIDS = Path('/etc')
HOST_KEY = Path('/etc/ssh/ssh_host_ed25519_key.pub')
ACCOUNT = re.compile(r'[a-z_][a-z0-9_-]{0,31}\Z')
HANDLER = '/usr/lib/ai-toolbox-cockpit/bin/python -I /usr/share/ai-box/service.py'


def run(arguments, **kwargs):
    return subprocess.run(arguments, check=True, capture_output=True, text=True, **kwargs)


def regular_read(path, owner=0):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
            raise ValueError(f'Unsafe ownership or permissions: {path}')
        value = stream.read(1024 * 1024 + 1)
        if len(value) > 1024 * 1024:
            raise ValueError(f'File is too large: {path}')
        return value


@contextlib.contextmanager
def directory(path, uid=0, gid=0):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != uid:
            raise ValueError(f'Unexpected directory owner: {path}')
        os.fchmod(fd, 0o700)
        yield fd
    finally:
        os.close(fd)


def write_at(fd, name, value, uid=0, gid=0, mode=0o600):
    # All destination access is relative to an already-open directory, and
    # replacement is atomic. Account-controlled symlinks are never followed.
    if '/' in name:
        raise ValueError('Expected a filename')
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f'Refusing a non-regular destination: {name}')
    temporary = '.ai-box-' + secrets.token_hex(12)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         mode, dir_fd=fd)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            os.fchown(stream.fileno(), uid, gid)
            os.fchmod(stream.fileno(), mode)
            stream.write(value)
        os.rename(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def read_at(fd, name):
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return ''
    with os.fdopen(descriptor) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f'Refusing a non-regular source: {name}')
        value = stream.read(1024 * 1024 + 1)
        if len(value) > 1024 * 1024:
            raise ValueError(f'File is too large: {name}')
        return value


def accounts(operator, serving):
    if any(not ACCOUNT.fullmatch(name) for name in (operator, serving)) or operator == serving:
        raise ValueError('Configure two distinct Linux account names')
    identities = tuple(pwd.getpwnam(name) for name in (operator, serving))
    if any(identity.pw_uid == 0 for identity in identities):
        raise ValueError('Operator and serving accounts must both be unprivileged')
    service = identities[1]
    memberships = os.getgrouplist(service.pw_name, service.pw_gid)
    if any(grp.getgrgid(gid).gr_name in ('wheel', 'sudo', 'docker') or gid == 0 for gid in memberships):
        raise ValueError('The serving account must not have administrator or root-container groups')
    if service.pw_shell.endswith(('/nologin', '/false')) or not Path(service.pw_shell).is_file():
        raise ValueError('The serving account needs a usable SSH login shell')
    for identity in identities:
        home = Path(identity.pw_dir).resolve()
        if not re.fullmatch(r'[A-Za-z0-9_./-]+', str(home)):
            raise ValueError('Account home path contains unsupported SSH configuration characters')
        if not home.is_dir() or home.stat().st_uid != identity.pw_uid:
            raise ValueError(f'Account needs its own home directory: {identity.pw_name}')
    return identities


def configured_accounts():
    # /etc is root-controlled; do not accept an account-controlled config directory.
    info = CONFIG.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('Account configuration must be in a root-owned directory')
    return accounts(*(regular_read(CONFIG / (role + '-account')).strip()
                      for role in ('operator', 'serving')))


def gpu_membership(service):
    groups = set()
    for node in DRM.glob('renderD*'):
        if (node / 'device/vendor').read_text().strip() != '0x1002':
            continue
        gid = (DEVICES / node.name).stat().st_gid
        group = grp.getgrgid(gid)
        if gid == 0 or group.gr_name in ('wheel', 'sudo', 'docker'):
            raise ValueError('Unsafe AMD render-device group')
        lines = GROUPS.read_text().splitlines()
        matches = [line.split(':') for line in lines if line.split(':')[0] == group.gr_name]
        if matches and any(int(row[2]) != gid for row in matches):
            raise ValueError('Conflicting render-group identity')
        if not matches:
            with GROUPS.open('a') as stream:
                stream.write(f'{group.gr_name}:x:{gid}:\n')
        groups.add(group.gr_name)
    if not groups:
        raise ValueError('No AMD render device found')
    existing = set(os.getgrouplist(service.pw_name, service.pw_gid))
    changed = any(grp.getgrnam(name).gr_gid not in existing for name in groups)
    if changed:
        run(['usermod', '-aG', ','.join(sorted(groups)), service.pw_name])
    return changed


def subordinate_ids(service):
    for name, flag in [('subuid', '--add-subuids'), ('subgid', '--add-subgids')]:
        path = SUBIDS / name
        rows = []
        for line in path.read_text().splitlines() if path.exists() else []:
            if line.strip() and not line.startswith('#'):
                owner, start, count = line.split(':')
                rows.append((owner, int(start), int(count)))
        aliases = {service.pw_name, str(service.pw_uid)}
        owned = [row for row in rows if row[0] in aliases]
        if owned:
            if not any(count >= 65536 for _, _, count in owned):
                raise ValueError(f'{name}: existing serving allocation is too small')
            if any(start < other_start + other_count and other_start < start + count
                   for _, start, count in owned
                   for other, other_start, other_count in rows if other not in aliases):
                raise ValueError(f'{name}: overlapping serving allocation')
            continue
        start = max([100000] + [first + count for _, first, count in rows])
        if start + 65536 >= 2**32:
            raise ValueError(f'{name}: no subordinate range available')
        run(['usermod', flag, f'{start}-{start + 65535}', service.pw_name])


@contextlib.contextmanager
def ssh_directory(identity):
    home = Path(identity.pw_dir).resolve()
    # Use the actual passwd home, including /home -> /var/home on CoreOS.
    home_fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.mkdir('.ssh', 0o700, dir_fd=home_fd)
            os.chown('.ssh', identity.pw_uid, identity.pw_gid, dir_fd=home_fd, follow_symlinks=False)
        except FileExistsError:
            pass
        fd = os.open('.ssh', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home_fd)
        try:
            if os.fstat(fd).st_uid != identity.pw_uid:
                raise ValueError('SSH directory must belong to its account')
            os.fchmod(fd, 0o700)
            yield home / '.ssh', fd
        finally:
            os.close(fd)
    finally:
        os.close(home_fd)


def public_key(value):
    fields = value.strip().split()
    if len(fields) < 2 or fields[0] != 'ssh-ed25519' or not re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', fields[1]):
        raise ValueError('Expected an Ed25519 management public key')
    return ' '.join(fields[:2])


def ssh_enrollment(operator, service):
    with ssh_directory(operator) as (operator_ssh, operator_fd), ssh_directory(service) as (service_ssh, service_fd):
        key = operator_ssh / 'ai-box-service'
        if not read_at(operator_fd, key.name):
            run(['runuser', '-u', operator.pw_name, '--', 'ssh-keygen', '-q', '-t', 'ed25519',
                 '-N', '', '-C', 'ai-box-service', '-f', str(key)])
        public = public_key(run(['runuser', '-u', operator.pw_name, '--', 'ssh-keygen', '-y', '-f', str(key)]).stdout)
        blob = public.split()[1]
        restricted = f'restrict,from="127.0.0.1,::1",command="{HANDLER}" {public} ai-box-service'
        # Replace any authorization of this specific management key; preserve others.
        lines = [line for line in read_at(service_fd, 'authorized_keys').splitlines()
                 if blob not in line.split()]
        lines.append(restricted)
        write_at(service_fd, 'authorized_keys', '\n'.join(lines) + '\n', service.pw_uid, service.pw_gid)
        write_at(operator_fd, 'ai-box-service.pub', public + '\n', operator.pw_uid, operator.pw_gid)
        host = regular_read(HOST_KEY).strip().split()
        if len(host) < 2 or host[0] != 'ssh-ed25519':
            raise ValueError('Expected a local Ed25519 SSH host key')
        write_at(operator_fd, 'ai-box-known-hosts', 'localhost,127.0.0.1,::1 ' + ' '.join(host[:2]) + '\n',
                 operator.pw_uid, operator.pw_gid)
        # Dedicated client configuration leaves the operator's normal SSH config alone.
        config = f'''Host localhost
    User {service.pw_name}
    IdentityFile {key}
    IdentitiesOnly yes
    UserKnownHostsFile {operator_ssh / 'ai-box-known-hosts'}
    StrictHostKeyChecking yes
    HostKeyAlgorithms ssh-ed25519
    ForwardAgent no
    ClearAllForwardings yes
'''
        write_at(operator_fd, 'ai-box-config', config, operator.pw_uid, operator.pw_gid)
        run(['restorecon', '-RF', str(operator_ssh), str(service_ssh)])


def inspect_connection(operator, service):
    config = Path(operator.pw_dir).resolve() / '.ssh/ai-box-config'
    result = run(['runuser', '-u', operator.pw_name, '--', 'ssh', '-F', str(config), '-T',
                  '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-p', '22',
                  f'{service.pw_name}@localhost', *HANDLER.split()],
                 input='{"operation":"inspect","args":{}}\n', timeout=60)
    response = json.loads(result.stdout)
    if not response.get('ok'):
        raise ValueError('Restricted serving-account inspection failed')
    state = response['result']
    if not state.get('linger') or not state['preflight'].get('ready'):
        raise ValueError('Serving preflight or linger failed; inspect ai-box doctor as the serving account')


def identity(operator, service):
    return dict(schema=1, operator=operator.pw_name, serving=service.pw_name,
                helper_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())


def provision(operator, service, check=False, repair=False):
    expected = identity(operator, service)
    try:
        completed = json.loads(regular_read(STATE / 'provisioned.json')) == expected
    except (FileNotFoundError, json.JSONDecodeError):
        completed = False
    if check:
        if not completed:
            raise ValueError('This image/account configuration has not completed provisioning')
        inspect_connection(operator, service)
        return
    if completed and not repair:
        print('AI account provisioning already completed for this image helper and account pair.')
        return
    if run(['getenforce']).stdout.strip() != 'Enforcing':
        raise ValueError('Enforcing SELinux is required')
    changed = gpu_membership(service)
    subordinate_ids(service)
    ssh_enrollment(operator, service)
    run(['loginctl', 'enable-linger', service.pw_name])
    # A manager started before membership changes must reacquire its groups.
    action = 'restart' if changed else 'start'
    run(['systemctl', action, f'user@{service.pw_uid}.service'])
    runtime = f'/run/user/{service.pw_uid}'
    result = run(['runuser', '-u', service.pw_name, '--', 'env', f'XDG_RUNTIME_DIR={runtime}',
                  f'DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus', 'podman', 'info',
                  '--format', '{{.Host.Security.Rootless}}'])
    if result.stdout.strip() != 'true':
        raise ValueError('Rootless Podman preflight failed')
    inspect_connection(operator, service)
    with directory(STATE) as fd:
        write_at(fd, 'provisioned.json', json.dumps(expected, indent=2) + '\n')
    print('AI account provisioning complete. Run ai-toolbox-cockpit as the operator.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operator')
    parser.add_argument('--serving')
    parser.add_argument('--configure-only', action='store_true', help='Record account names for next boot')
    parser.add_argument('--check', action='store_true', help='Check completed commissioning and the SSH connection')
    parser.add_argument('--repair', action='store_true', help='Repeat commissioning, preserving keys and unrelated SSH settings')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run this administrator commissioning command with sudo')
    if bool(args.operator) != bool(args.serving) or (args.configure_only and not args.operator):
        parser.error('--operator and --serving must be supplied together')
    if args.check and (args.operator or args.configure_only or args.repair):
        parser.error('--check cannot change the account configuration')
    descriptor = os.open(LOCK, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.operator:
            accounts(args.operator, args.serving)
            with directory(CONFIG) as fd:
                for role, name in [('operator', args.operator), ('serving', args.serving)]:
                    write_at(fd, role + '-account', name + '\n', mode=0o644)
        if args.configure_only:
            print('Account roles configured. The image-owned service will provision them at boot.')
            return 0
        provision(*configured_accounts(), check=args.check, repair=args.repair)
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        # Never print subprocess stdout/stderr: it may contain private host data.
        print(f'ai-box-provision: {error}', file=sys.stderr)
        return 1
    finally:
        os.close(descriptor)


if __name__ == '__main__':
    sys.exit(main())
