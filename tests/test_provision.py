"""Privileged commissioning boundaries tested with temporary files and mocked host tools."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('provision', ROOT / 'system_files/usr/share/ai-box/provision.py')
provision = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provision)


class ProvisionBoundaries(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.operator = SimpleNamespace(pw_name='operator_27', pw_uid=10027, pw_gid=10027)
        self.serving = SimpleNamespace(pw_name='serving_83', pw_uid=10083, pw_gid=10083)

    def test_rejects_root_execution_by_an_unprivileged_caller_before_writes(self):
        with patch.object(provision.os, 'geteuid', return_value=1000), \
             patch.object(provision.sys, 'argv', ['ai-box-provision']), \
             patch.object(provision, 'accounts') as accounts, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                provision.main()
            accounts.assert_not_called()

    def test_account_names_must_be_distinct_and_not_shell_fragments(self):
        for operator, serving in [('same', 'same'), ('someone;id', 'worker'), ('operator', '--flag')]:
            with self.subTest(operator=operator), patch.object(provision.pwd, 'getpwnam') as lookup:
                with self.assertRaises(ValueError):
                    provision.accounts(operator, serving)
                lookup.assert_not_called()

    def test_ssh_key_comments_are_accepted_without_becoming_authorization_options(self):
        expected = 'ssh-ed25519 ' + 'A' * 40
        self.assertEqual(provision.public_key(expected + ' ai-box-service\n'), expected)
        self.assertEqual(provision.public_key(expected + ' comment with spaces\n'), expected)
        for value in ('ssh-rsa AAAA', 'ssh-ed25519', 'ssh-ed25519 invalid;option'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                provision.public_key(value)

    def test_root_and_privileged_serving_accounts_are_rejected(self):
        for privileged in ('wheel', 'sudo', 'docker'):
            with self.subTest(group=privileged), \
                 patch.object(provision.pwd, 'getpwnam', side_effect=[self.operator, self.serving]), \
                 patch.object(provision.os, 'getgrouplist', return_value=[10083, 900]), \
                 patch.object(provision.grp, 'getgrgid', side_effect=lambda gid: SimpleNamespace(
                     gr_name=privileged if gid == 900 else 'serving_83')):
                with self.assertRaisesRegex(ValueError, 'administrator'):
                    provision.accounts(self.operator.pw_name, self.serving.pw_name)
        with patch.object(provision.pwd, 'getpwnam', side_effect=[self.operator, SimpleNamespace(pw_uid=0)]):
            with self.assertRaisesRegex(ValueError, 'unprivileged'):
                provision.accounts('operator', 'root')

    def test_subordinate_ranges_are_allocated_after_other_accounts(self):
        for name in ('subuid', 'subgid'):
            (self.root / name).write_text('somebody:100000:65536\n')
        with patch.object(provision, 'SUBIDS', self.root), patch.object(provision, 'run') as run:
            provision.subordinate_ids(self.serving)
        self.assertEqual(run.call_args_list[0].args[0],
                         ['usermod', '--add-subuids', '165536-231071', 'serving_83'])
        self.assertEqual(run.call_args_list[1].args[0],
                         ['usermod', '--add-subgids', '165536-231071', 'serving_83'])

    def test_existing_overlapping_or_small_subordinate_ranges_fail_before_mutation(self):
        for contents in ('serving_83:100000:100\n',
                         'serving_83:100000:65536\nother:120000:65536\n'):
            (self.root / 'subuid').write_text(contents)
            with self.subTest(contents=contents), patch.object(provision, 'SUBIDS', self.root), \
                 patch.object(provision, 'run') as run:
                with self.assertRaises(ValueError):
                    provision.subordinate_ids(self.serving)
                run.assert_not_called()

    def test_valid_numeric_uid_subordinate_ranges_are_reused(self):
        for name in ('subuid', 'subgid'):
            (self.root / name).write_text('10083:200000:65536\nother:300000:65536\n')
        with patch.object(provision, 'SUBIDS', self.root), patch.object(provision, 'run') as run:
            provision.subordinate_ids(self.serving)
            run.assert_not_called()

    def test_atomic_write_rejects_account_controlled_destination_symlinks(self):
        outside = self.root / 'outside'
        outside.write_text('preserve')
        (self.root / 'authorized_keys').symlink_to(outside)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        with self.assertRaises(ValueError):
            provision.write_at(fd, 'authorized_keys', 'replacement')
        self.assertEqual(outside.read_text(), 'preserve')

    def test_ssh_directory_symlink_cannot_redirect_root_writes(self):
        home = self.root / 'home'
        home.mkdir()
        outside = self.root / 'outside'
        outside.mkdir()
        (home / '.ssh').symlink_to(outside, target_is_directory=True)
        account = SimpleNamespace(pw_dir=str(home), pw_uid=os.getuid(), pw_gid=os.getgid())
        with self.assertRaises(OSError):
            with provision.ssh_directory(account):
                self.fail('SSH directory symlink was followed')
        self.assertEqual(list(outside.iterdir()), [])

    def test_failed_preflight_never_marks_provisioning_complete(self):
        with patch.object(provision, 'STATE', self.root), \
             patch.object(provision, 'regular_read', side_effect=FileNotFoundError), \
             patch.object(provision, 'run', return_value=SimpleNamespace(stdout='Enforcing')), \
             patch.object(provision, 'gpu_membership'), \
             patch.object(provision, 'subordinate_ids'), \
             patch.object(provision, 'ssh_enrollment') as ssh, \
             patch.object(provision, 'directory') as directory:
            with self.assertRaisesRegex(ValueError, 'Rootless Podman'):
                provision.provision(self.operator, self.serving)
            ssh.assert_called_once()
            directory.assert_not_called()
        self.assertFalse((self.root / 'provisioned.json').exists())

    def test_completed_state_is_specific_to_the_helper_and_account_pair(self):
        expected = provision.identity(self.operator, self.serving)
        with patch.object(provision, 'regular_read', return_value=json.dumps(expected)), \
             patch.object(provision, 'run') as run:
            provision.provision(self.operator, self.serving)
            run.assert_not_called()
        changed = dict(expected, serving='another_user')
        with patch.object(provision, 'regular_read', return_value=json.dumps(changed)), \
             patch.object(provision, 'run', return_value=SimpleNamespace(stdout='Permissive')):
            with self.assertRaisesRegex(ValueError, 'SELinux'):
                provision.provision(self.operator, self.serving)

    def test_image_unit_needs_only_machine_configuration(self):
        text = (ROOT / 'system_files/usr/lib/systemd/system/ai-box-provision.service').read_text()
        self.assertIn('ExecStart=/usr/bin/ai-box-provision', text)
        self.assertIn('ConditionPathExists=/etc/ai-box/operator-account', text)
        self.assertNotIn('/etc/ucore-autorebase/signed', text)
        self.assertNotIn('/etc/ai-box/provision.sh', text)


if __name__ == '__main__':
    unittest.main()
