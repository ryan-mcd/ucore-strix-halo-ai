#!/bin/bash
set -euo pipefail

# Follow Bluefin's Fedora desktop hardware model: ship drivers, firmware and
# network backends in the image, rather than downloading them on first boot.
# Fedora owns the vendor selection in @hardware-support and linux-firmware's
# recommendations. Explicitly install the recommendations even when the base
# already contains linux-firmware with its weak dependencies omitted.
packages=(
    @hardware-support linux-firmware alsa-firmware alsa-sof-firmware
    NetworkManager-wifi NetworkManager-bluetooth NetworkManager-wwan
    wpa_supplicant iwd bluez ModemManager fwupd pciutils usbutils
    mesa-dri-drivers mesa-vulkan-drivers mesa-va-drivers libva-utils vulkan-tools
    alsa-utils alsa-ucm pipewire pipewire-alsa pipewire-pulseaudio wireplumber
    nvme-cli smartmontools lm_sensors bolt
    python3 toolbox crun slirp4netns openssh-clients
)
dnf5 install -y "${packages[@]}"
mapfile -t firmware_packages < <(rpm -q --recommends linux-firmware)
if ((${#firmware_packages[@]} == 0)); then
    echo 'linux-firmware has no vendor recommendations; review the Fedora hardware profile.' >&2
    exit 1
fi
dnf5 install -y "${firmware_packages[@]}"
dnf5 clean all
# Repository counters, DNF locks and package policy-install scratch data are
# builder state; the installed SELinux policy remains under /etc/selinux.
rm -rf /var/lib/dnf/repos /run/dnf /run/selinux-policy

# Only host support belongs here; ROCm/PyTorch stay in runtime containers.
python3 /ctx/hardware/check.py
cp -avf /ctx/system_files/. /
chmod 0755 /usr/bin/ai-box /usr/bin/ai-toolbox-cockpit

# Keep operator tooling out of the OS Python environment. Both the upstream
# source archive and all Python dependencies are pinned and hash-checked.
cockpit_env=/usr/lib/ai-toolbox-cockpit
python3 -m venv "$cockpit_env"
"$cockpit_env/bin/python" -m pip --isolated --disable-pip-version-check install \
    --no-cache-dir --require-hashes --only-binary=:all: \
    -r /ctx/cockpit/requirements.txt
"$cockpit_env/bin/python" -m pip --isolated --disable-pip-version-check install \
    --no-cache-dir --require-hashes --no-deps --no-build-isolation \
    -r /ctx/cockpit/source.txt
"$cockpit_env/bin/python" -m pip check
# The public entry point must use the image-managed service integration.
rm -f "$cockpit_env/bin/ai-toolbox-cockpit"
install -d /usr/share/ai-box/cockpit
install -m 0644 /ctx/cockpit/{requirements.txt,source.txt} /usr/share/ai-box/cockpit/
"$cockpit_env/bin/python" -I /ctx/cockpit/check.py
# Do not expose a rootful Podman API socket or start AI services at image build.
