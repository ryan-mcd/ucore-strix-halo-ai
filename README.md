# Strix Halo AI box

Implementation of a headless uCore image candidate and a rootless Vulkan llama.cpp service. The platform is a development candidate; production trust, build isolation, network policy and hardware qualification remain incomplete.

The canonical guides, OKF second brain, and security evaluation have moved to the sibling documentation checkout:

- [Documentation and deployment guide](../ucore-strix-halo-ai-docs/README.md)
- [Knowledge entry point](../ucore-strix-halo-ai-docs/knowledge/index.md)
- [Security assessment](../ucore-strix-halo-ai-docs/knowledge/security/assessment.md)
- [Detailed findings and remediation](../ucore-strix-halo-ai-docs/knowledge/security/findings.md)

These links expect the two directories side by side. The documentation destination has not been published to a remote repository by this task.

## Implementation

`Containerfile` and `build_files/build.sh` define the uCore host candidate. `system_files/usr/bin/ai-box` supplies `doctor`, `pin`, `plan`, `install`, and `smoke`. Deployment templates live in `config/` and `system_files/usr/share/ai-box/`.

The compatibility target is the whole Framework Desktop, using working Bluefin as the reference. The image includes Fedora hardware support/firmware, network backends, Mesa graphics/video, ALSA/PipeWire audio, storage diagnostics and USB4 authorization tooling. Build checks cover graphics, NPU, networking, Bluetooth, audio, NVMe, USB/USB4, input, thermal monitoring and TPM module availability on x86_64. Boot, display/audio outputs, expansion cards, power management and firmware updates still require actual platform acceptance. See [hardware support and installation](../ucore-strix-halo-ai-docs/knowledge/hardware/support.md). These additions apply to the built appliance image; an already downloaded stock Fedora CoreOS live ISO is a separate environment and does not acquire them.

The intended service uses rootless Podman/Quadlet, host-loopback publication, an API key, reviewed artifact hashes, restricted mounts and configurable memory limits. Runtime configuration accepts a single GGUF or a complete split GGUF set through the Vulkan toolbox, with a SHA-256 manifest covering every file. Existing single-file `model_sha256` configurations remain accepted. The target is 128 GB Strix Halo with a 4 GiB system reserve and Unsloth Qwen3.8 Flash Next in 4-bit. The template's `memory_gib: "auto"` computes the service limit from floor(Linux MemTotal in GiB) minus that reserve when rendering/installing; an integer override is supported. It replaces the earlier arbitrary 48 GiB bootstrap cap. The exact runtime/loading profile still requires hardware qualification. See the security findings before using private data or treating these source-level controls as verified on hardware.

The image ships AI Toolbox Cockpit as the main management interface. After the administrator provisions the serving account, GPU access, linger and restricted local SSH authorization, open a terminal as the separate unprivileged operator and run:

```bash
ai-toolbox-cockpit
```

In **Inference service**, connect to the serving account, choose the runtime and model/quantization, download the complete verified model set, set capacity, prepare/review the service, and install/start it. The UI writes the runtime digest and model manifest; routine setup requires no JSON editing. Start/stop/restart, status, logs and an authenticated API check are available there. Downloads are public, revision-pinned Hugging Face artifacts staged in the serving account and verified before promotion. The supported service profile is Vulkan RADV; additional runtime profiles still need integration and qualification.

Do not use `sudo` or run Cockpit under the inference account. The operator connects over local SSH using the documented restricted key; inference uses rootless Quadlet and continues after Cockpit closes. The upstream foreground launchers remain disabled. **Operator models/toolboxes** are separate trusted work areas. Cockpit's Python environment and source are pinned and hash-checked at build time; application updates arrive through image rebuilds. See the [management guide](../ucore-strix-halo-ai-docs/knowledge/operations/management.md) for the one-time administrator setup and the complete UI flow. This is a terminal UI; no new web listener is enabled.

## Validate code

Python 3.11+ is required. Runtime validation and tests use only the standard library and do not require the documentation checkout:

```bash
python3 scripts/validate.py
python3 -m unittest discover -s tests -v
bash -n build_files/build.sh
```

Documentation validation runs in the documentation project using its `requirements-dev.txt`. To explicitly validate both checkouts with an interpreter that has those dependencies, run `python3 scripts/validate.py --docs ../ucore-strix-halo-ai-docs`.

## Build

On a Linux builder with Podman and Just, run `just build`. CI validates the workflows, shell, configuration and tests, then builds an OCI image with Buildah on native AMD64. A fresh job copies, signs and verifies the candidate before updating release tags; it does not execute the candidate image. Publishing requires `SIGNING_SECRET` on the default branch. Pull requests, including Dependabot PRs, build without publishing or signing secrets.

Dependabot checks GitHub Actions, the `ucore-minimal:stable` base reference, and Cockpit's Python dependency lock. Cockpit source revision updates require a reviewed archive/hash change. Image builds exercise the installed persistent-service UI flow and check that upstream foreground launchers and self-upgrade controls are absent. Renovate is disabled to avoid duplicate updates. The unused desktop disk-image workflow has been removed; use the documented CoreOS provisioning path. See [CI and dependency maintenance](../ucore-strix-halo-ai-docs/knowledge/operations/ci.md) for the reference comparison, release flow and validation limits.
