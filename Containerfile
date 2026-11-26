# Allow build scripts to be referenced without being copied into the final image
FROM scratch AS ctx
COPY build_files /
COPY system_files /system_files

# Bifrost v2.6.0, multi-architecture release; its statically linked gateway/UI
# is image-owned. Model/GPU userspace remains in the rootless runtime container.
FROM docker.io/maximhq/bifrost@sha256:5968d007ba4f74ee01bc4ceb071decb9927a0b7bb43dbb9072208f75e2d522e3 AS bifrost

# Headless container host.
FROM ghcr.io/ublue-os/ucore-minimal:stable
COPY --from=bifrost --chown=0:0 --chmod=0755 /app/main /usr/libexec/ai-box-bifrost

### MODIFICATIONS
## make modifications desired in your image and install packages by modifying the build.sh script
## the following RUN directive does all the things required to run "build.sh" as recommended.

RUN --mount=type=bind,from=ctx,source=/,target=/ctx \
    --mount=type=cache,dst=/var/cache \
    --mount=type=cache,dst=/var/log \
    --mount=type=tmpfs,dst=/tmp \
    /ctx/build.sh

### LINTING
# Remove the inherited runtime-only lock before checking the actual final rootfs.
# bootc validates this image; the legacy OSTree finalizer requires a different layout.
RUN rm -f /tmp/kdump.lock \
    && bootc container lint
