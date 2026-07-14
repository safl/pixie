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
#   tftpd-hpa:       serves iPXE NBPs to BIOS/UEFI PXE targets. pixie
#                    supervises ``in.tftpd`` as a subprocess (see
#                    pixie.tftp._supervisor); the daemon binds
#                    udp/69 which the LAN-only ``--network=host``
#                    deploy exposes directly.
#   ipxe:            UEFI + BIOS network bootloaders. The package
#                    ships ``undionly.kpxe`` (BIOS) + ``ipxe.efi``
#                    (UEFI) + ``snponly.efi`` under
#                    /usr/lib/ipxe/, which we copy into pixie's
#                    TFTP root so the daemon can serve them.
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
        ipxe \
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

# Bake the iPXE NBPs into the TFTP root pixie's supervisor serves.
# The ``ipxe`` package drops them under ``/usr/lib/ipxe/``; copy the
# common three (BIOS + UEFI + SNP-only UEFI) so a fresh target's
# firmware finds the right one on next-server/filename lookup.
RUN mkdir -p /usr/share/pixie/tftp && \
    cp /usr/lib/ipxe/undionly.kpxe /usr/share/pixie/tftp/ 2>/dev/null || true && \
    cp /usr/lib/ipxe/ipxe.efi      /usr/share/pixie/tftp/ 2>/dev/null || true && \
    cp /usr/lib/ipxe/snponly.efi   /usr/share/pixie/tftp/ 2>/dev/null || true

# Debian's stock iPXE binary is not built with an embedded chain
# script, so on load it falls back to fetching ``autoexec.ipxe`` from
# the TFTP root. Bake one that re-DHCPs and chains to pixie's HTTP
# bootstrap endpoint. Without this the ``ipxe.efi -> autoexec.ipxe``
# hop 404s and the target sits at "Could not open autoexec" until it
# times out. Verified live 2026-07-14 on 10.20.30.10 booting a real
# UEFI target: the target's UEFI TFTP-fetches ipxe.efi, iPXE loads,
# then requests /autoexec.ipxe -- this file is what unblocks that
# fetch and continues the chain to pixie's per-MAC plan.
RUN printf '%s\n' \
    '#!ipxe' \
    '# pixie autoexec: re-DHCP + chain to pixie s HTTP bootstrap.' \
    'dhcp || goto handoff' \
    'chain http://${next-server}:8080/pxe-bootstrap.ipxe || echo pixie: could not reach ${next-server}:8080' \
    ':handoff' \
    'exit' \
    > /usr/share/pixie/tftp/autoexec.ipxe

# Persistent state under /var/lib/pixie: state.db, session-secret,
# blobs/, artifacts/, images/. The Quadlet/compose bind-mounts an
# operator-owned host directory here so a container rebuild keeps
# every catalog entry + downloaded blob.
ENV PIXIE_DATA_DIR=/var/lib/pixie
VOLUME ["/var/lib/pixie"]

# TFTP supervisor is off by default (unit-test / dev fires up the app
# on non-root); flip on inside the container image so a compose bring-
# up ships a working PXE-first hop.
ENV PIXIE_TFTP_ENABLED=1

# --network=host in production (both for udp/69 TFTP and the NBD port
# range). Expose 8080 as documentation for a compose-bridge dev run.
EXPOSE 8080

# curl -f fails on 4xx/5xx without dumping a Python traceback per probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

CMD ["uvicorn", "pixie.web.main:app", "--host", "0.0.0.0", "--port", "8080"]
