import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


path = Path(__file__).resolve().parents[1] / 'build_files/hardware/check.py'
spec = importlib.util.spec_from_file_location('hardware_profile', path)
hardware = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hardware)


class FrameworkDesktopProfile(unittest.TestCase):
    def module_info(self, *args):
        field, name = args[-2:]
        if field == 'filename':
            return '(builtin)' if name == 'nvme_core' else f'/modules/{name}.ko.zst'
        if field == 'firmware':
            return {
                'mt7925_common': 'mediatek/mt7925/wifi.bin',
                'amdxdna': 'amdnpu/17f0_11/npu.sbin',
            }.get(name, '')
        if field == 'depends':
            return 'mt7925_common' if name == 'mt7925e' else ''
        self.fail(f'Unexpected module query: {args}')

    def test_shared_firmware_and_builtin_modules_are_supported(self):
        with patch.object(hardware, 'output', side_effect=self.module_info), \
                patch.object(hardware, 'firmware_exists', return_value=True) as exists:
            modules = hardware.check_modules('test.x86_64')
        self.assertEqual(modules['nvme_core'], '(builtin)')
        self.assertIn('snd_hda_intel', modules)
        self.assertIn('thunderbolt', modules)
        self.assertIn('amdxdna', modules)
        self.assertIn('snd_hda_codec_realtek', modules)
        self.assertEqual({call.args[0] for call in exists.call_args_list}, {
            'mediatek/mt7925/wifi.bin', 'amdnpu/17f0_11/npu.sbin'})

    def test_split_realtek_layout_replaces_the_monolithic_module(self):
        def modern(*args):
            if args[-1] == 'snd_hda_codec_realtek':
                raise SystemExit('legacy module absent')
            return self.module_info(*args)

        with patch.object(hardware, 'output', side_effect=modern), \
                patch.object(hardware, 'firmware_exists', return_value=True):
            modules = hardware.check_modules('7.2.test.x86_64')
        self.assertNotIn('snd_hda_codec_realtek', modules)
        for name in hardware.REALTEK_CODEC_PROFILES['split']:
            self.assertIn(name, modules)

    def test_split_realtek_library_alone_does_not_pass(self):
        def incomplete(*args):
            if args[-1] in ('snd_hda_codec_realtek', 'snd_hda_codec_alc269'):
                raise SystemExit('required codec-family module absent')
            return self.module_info(*args)

        with patch.object(hardware, 'output', side_effect=incomplete), \
                self.assertRaisesRegex(SystemExit, 'codec-family'):
            hardware.check_modules('7.2.test.x86_64')

    def test_missing_both_realtek_layouts_fails(self):
        def no_codec(*args):
            if args[-1] in ('snd_hda_codec_realtek', 'snd_hda_codec_realtek_lib'):
                raise SystemExit('no Realtek codec support')
            return self.module_info(*args)

        with patch.object(hardware, 'output', side_effect=no_codec), \
                self.assertRaisesRegex(SystemExit, 'no Realtek'):
            hardware.check_modules('test.x86_64')

    def test_missing_audio_module_rejects_an_otherwise_working_network_profile(self):
        def missing_audio(*args):
            if args[-2:] == ('filename', 'snd_hda_intel'):
                raise subprocess.CalledProcessError(1, args)
            return self.module_info(*args)

        with patch.object(hardware, 'output', side_effect=missing_audio), \
                self.assertRaises(subprocess.CalledProcessError):
            hardware.check_modules('test.x86_64')

    def test_missing_npu_firmware_rejects_the_platform_profile(self):
        with patch.object(hardware, 'output', side_effect=self.module_info), \
                patch.object(hardware, 'firmware_exists', side_effect=lambda name: not name.startswith('amdnpu/')), \
                self.assertRaisesRegex(SystemExit, 'amdnpu/'):
            hardware.check_modules('test.x86_64')
