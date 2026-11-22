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

The intended service uses rootless Podman/Quadlet, host-loopback publication, an API key, reviewed artifact hashes, restricted mounts and memory limits. Runtime support currently covers one single-file GGUF through the Vulkan toolbox. See the security findings before using private data or treating these source-level controls as verified on hardware.

The image also ships AI Toolbox Cockpit for trusted operator model downloads, inventory and toolbox lifecycle management. After deploying the updated image, open an SSH terminal as a separate unprivileged operator and run:

```bash
ai-toolbox-cockpit
```

Do not use `sudo` or run it under the inference account. The image integration keeps upstream foreground server launchers out of the UI because their defaults relax our inference controls; use `ai-box`/Quadlet for serving. Cockpit's Python environment and source are pinned and hash-checked at build time; application updates arrive through image rebuilds. See the [management guide](../ucore-strix-halo-ai-docs/knowledge/operations/management.md) for model promotion and account separation. This is a terminal UI; no new web listener is enabled.

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

Dependabot checks GitHub Actions, the `ucore-minimal:stable` base reference, and Cockpit's Python dependency lock. Cockpit source revision updates require a reviewed archive/hash change. Image builds exercise the installed management UI and check that server launch panels and self-upgrade controls are absent. Renovate is disabled to avoid duplicate updates. The unused desktop disk-image workflow has been removed; use the documented CoreOS provisioning path. See [CI and dependency maintenance](../ucore-strix-halo-ai-docs/knowledge/operations/ci.md) for the reference comparison, release flow and validation limits.
