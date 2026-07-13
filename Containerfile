# pixie container: one image, one FastAPI process, three logical
# concerns (catalog + exports + machines) mounted at the same
# HTTP root.
#
# Base is Ubuntu 26.04 for nbdkit >= 1.44 (Ubuntu 24.04 ships 1.20;
# Debian trixie ships 1.42 which silently corrupts under
# ``cow + named-export`` per the nbdkit-filter-cow(1) "Export safe"
# column). Pixie's ramboot chain gives each target a writable overlay
# on top of a shared read-only image, so a working ``cow`` filter is
# not optional.
FROM ubuntu:26.04

ARG PIXIE_VERSION=dev
LABEL org.opencontainers.image.title="pixie" \
      org.opencontainers.image.description="Bare-metal netboot appliance: catalog + fetch + NBD + PXE + TFTP + operator TUI in one container" \
      org.opencontainers.image.source="https://github.com/safl/pixie" \
      org.opencontainers.image.url="https://github.com/safl/pixie" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.version="${PIXIE_VERSION}"

# Runtime toolchain. Rationale per group:
#   python3 / pip:   pixie's server + CLIs.
#   curl:            HEALTHCHECK below + the fetch pipeline's
#                    upstream reader (nbdmux-shaped).
#   tar:             netboot-bundle unpack.
#   gzip / zstd / xz-utils:
#                    decompressor CLIs the fetch pipeline pipes to
#                    while streaming into the on-disk image.
#   nbdkit:          the NBD supervisor spawns one nbdkit per export;
#                    the ``file`` plugin and the ``cow`` +
#                    ``partition`` filters ship inside the base
#                    ``nbdkit`` package on Ubuntu (they were separate
#                    packages on older Debian). cow gives each ramboot
#                    target its own writable overlay; partition serves
#                    the first partition of a full-disk image.
#   tftpd-hpa:       BIOS-PXE bootstrap (in-process TFTP path lands
#                    later; today's placeholder assumes a systemd unit
#                    approach that matches bty-tftp's model).
#   qemu-utils:      qemu-img info + qemu-nbd for qcow2 handling.
#   ca-certificates: HTTPS fetch of ORAS + release assets.
#
# The exact apt package names are subject to verification during PR 2
# when the fetch pipeline actually lands and CI builds this image;
# ubuntu:26.04 does not have full listings public at repo-init time.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        ca-certificates \
        curl \
        tar \
        gzip \
        zstd \
        xz-utils \
        nbdkit \
        tftpd-hpa \
        qemu-utils \
 && rm -rf /var/lib/apt/lists/*

# App under /app with editable-style install from the source we copy in.
# At 0.1.0 the wheel isn't published yet, so the container installs
# from source. From PR 2 onwards (once we publish to PyPI on tag) the
# GHA workflow can stage a wheel and pip install that instead, matching
# bty's pattern.
WORKDIR /app
COPY pyproject.toml README.md LICENSE /app/
COPY src/ /app/src/
RUN pip install --break-system-packages --no-cache-dir .

# Persistent state under /var/lib/pixie: state.db, session-secret,
# blobs/, artifacts/, images/. The Quadlet/compose bind-mounts an
# operator-owned host directory here so a container rebuild keeps
# every catalog entry + downloaded blob.
ENV PIXIE_DATA_DIR=/var/lib/pixie
VOLUME ["/var/lib/pixie"]

# --network=host in production (both for udp/69 TFTP and the NBD port
# range). Expose 8080 as documentation for a compose-bridge dev run.
EXPOSE 8080

# curl -f fails on 4xx/5xx without dumping a Python traceback per probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

CMD ["uvicorn", "pixie.web.main:app", "--host", "0.0.0.0", "--port", "8080"]
