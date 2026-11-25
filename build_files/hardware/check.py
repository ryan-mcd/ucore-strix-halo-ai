#!/usr/bin/env python3
"""Check installed hardware support without loading modules or probing devices."""
import json
from pathlib import Path
import subprocess


# These are module-availability requirements, not assertions about which driver
# binds every board revision or expansion card. The working Bluefin inventory
# and actual Framework Desktop acceptance determine device-level support.
FRAMEWORK_DESKTOP_MODULES = {
    'graphics_and_display': ('amdgpu',),
    'npu': ('amdxdna',),
    'network': ('mt7925e', 'r8169'),
    'bluetooth': ('btusb', 'btmtk'),
    'audio': ('snd_hda_intel', 'snd_hda_codec_realtek', 'snd_usb_audio'),
    'nvme_storage': ('nvme', 'nvme_core'),
    'usb_and_expansion': ('xhci_pci', 'thunderbolt', 'usb_storage', 'uas'),
    'input': ('usbhid', 'hid_generic'),
    'thermal_monitoring': ('k10temp',),
    'tpm': ('tpm_crb',),
}


def output(*args):
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()
    except subprocess.CalledProcessError as error:
        raise SystemExit(f'{" ".join(args)} failed:\n{error.output}') from error


def firmware_exists(name):
    root = Path('/usr/lib/firmware')
    return any((root / (name + suffix)).is_file() for suffix in ('', '.xz', '.zst'))


def check_modules(kernel):
    modules = {}
    for names in FRAMEWORK_DESKTOP_MODULES.values():
        for name in names:
            modules[name] = output('modinfo', '-k', kernel, '-F', 'filename', name)

    # Firmware may be declared by shared modules, not the PCI entry point.
    # GPU declarations span many unrelated ASICs, so live gfx1151 acceptance
    # remains necessary instead of treating every amdgpu blob as board-specific.
    pending = ['mt7925e', 'r8169', 'btmtk', 'amdxdna']
    visited = set()
    firmware = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        firmware.update(output('modinfo', '-k', kernel, '-F', 'firmware', name).splitlines())
        pending.extend(filter(None, output('modinfo', '-k', kernel, '-F', 'depends', name).split(',')))
    if not any('mt7925' in name.lower() for name in firmware):
        raise SystemExit(f'{kernel}: MT7925 firmware declarations were not found')
    if not any(name.startswith('amdnpu/') for name in firmware):
        raise SystemExit(f'{kernel}: AMD NPU firmware declarations were not found')
    missing = sorted(name for name in firmware if not firmware_exists(name))
    if missing:
        raise SystemExit(f'{kernel}: missing platform firmware: {missing}')
    return modules


def main():
    required = (
        'linux-firmware', 'amd-gpu-firmware', 'mt7xxx-firmware',
        'realtek-firmware', 'iwlwifi-mvm-firmware', 'atheros-firmware',
        'NetworkManager-wifi', 'NetworkManager-bluetooth', 'NetworkManager-wwan',
        'wpa_supplicant', 'iwd', 'bluez', 'ModemManager', 'fwupd',
        'mesa-dri-drivers', 'mesa-vulkan-drivers', 'mesa-va-drivers', 'libva-utils', 'vulkan-tools',
        'alsa-utils', 'alsa-ucm', 'pipewire', 'pipewire-alsa', 'pipewire-pulseaudio', 'wireplumber',
        'nvme-cli', 'smartmontools', 'lm_sensors', 'bolt', 'tuned',
    )
    kernels = output('rpm', '-q', '--qf', '%{VERSION}-%{RELEASE}.%{ARCH}\n', 'kernel-core').splitlines()
    if any(kernel.endswith('.x86_64') for kernel in kernels):
        required += ('amd-ucode-firmware',)
    # Fedora/Universal Blue can satisfy a capability with a differently named
    # package, such as mesa-va-drivers-freeworld. Check the installed provider.
    providers = set(output('rpm', '-q', '--whatprovides', '--qf', '%{NAME}\n', *required).splitlines())
    modules = {}
    for kernel in kernels:
        # Bluefin's kernel package selection includes all three module sets.
        # Keep the inherited uCore kernel and require matching modules; never
        # pull a different kernel merely to satisfy this check.
        output('rpm', '-q', *(f'{name}-{kernel}' for name in (
            'kernel-modules-core', 'kernel-modules', 'kernel-modules-extra')))
        if not kernel.endswith('.x86_64'):
            continue
        modules[kernel] = check_modules(kernel)

    inventory = output('rpm', '-qa', '--qf', '%{NAME} %{VERSION}-%{RELEASE}.%{ARCH}\n').splitlines()
    inventory = sorted(line for line in inventory if (
        'firmware' in line.split()[0] or line.split()[0] in providers or line.startswith('kernel-')))
    destination = Path('/usr/share/ai-box/hardware-packages.json')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(dict(
        profile='Fedora hardware-support and linux-firmware recommendations',
        target_model='Framework Desktop (AMD Ryzen AI Max 300 Series)',
        subsystems=FRAMEWORK_DESKTOP_MODULES,
        target_module_checks_executed=bool(modules),
        packages=inventory, target_modules=modules,
        qualification='Installed package/module/firmware checks only; no hardware or boot qualification',
    ), indent=2) + '\n')
    print('Installed Fedora hardware profile checked; target hardware qualification remains required.')


if __name__ == '__main__':
    main()
