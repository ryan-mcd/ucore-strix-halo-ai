#!/bin/bash
set -euo pipefail

# Only host tooling belongs here; do not layer ROCm/PyTorch onto the host.
packages=(python3 toolbox crun slirp4netns amd-gpu-firmware openssh-clients)
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
rm "$cockpit_env/bin/ai-toolbox-cockpit"
install -d /usr/share/ai-box/cockpit
install -m 0644 /ctx/cockpit/{requirements.txt,source.txt} /usr/share/ai-box/cockpit/
"$cockpit_env/bin/python" -I /ctx/cockpit/check.py
# Do not expose a rootful Podman API socket or start AI services at image build.
