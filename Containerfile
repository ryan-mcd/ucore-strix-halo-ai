# Allow build scripts to be referenced without being copied into the final image
FROM scratch AS ctx
COPY build_files /
COPY system_files /system_files

# Headless container host; AI userspace lives in separate runtime containers.
FROM ghcr.io/ublue-os/ucore-minimal:stable

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
