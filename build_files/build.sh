#!/bin/bash
set -euo pipefail

# Only host tooling belongs here; do not layer ROCm/PyTorch onto the host.
packages=(python3 toolbox crun slirp4netns amd-gpu-firmware)
missing=()
for package in "${packages[@]}"; do
    if ! rpm -q "$package" >/dev/null 2>&1; then
        missing+=("$package")
    fi
done
if ((${#missing[@]})); then
    if command -v dnf5 >/dev/null 2>&1; then
        dnf5 install -y "${missing[@]}"
        dnf5 clean all
    else
        rpm-ostree install "${missing[@]}"
        rpm-ostree cleanup -m
    fi
fi
cp -avf /ctx/system_files/. /
chmod 0755 /usr/bin/ai-box
# Do not expose a rootful Podman API socket or start AI services at image build.
