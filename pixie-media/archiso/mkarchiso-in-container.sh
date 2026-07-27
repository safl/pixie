#!/usr/bin/env bash
# mkarchiso-in-container.sh -- run mkarchiso against the pixie usbboot-pc
# profile INSIDE a privileged archlinux container.
#
# mkarchiso needs a rootful, --privileged, host-networked container:
# rootless podman fails pacstrap's ``mount --bind /dev`` (userns EPERM),
# and default container networking fails on some hosts (WSL2 netavark).
# The caller (cijoe/scripts/archiso_build.py) invokes this as e.g.
#
#   {podman|docker} run --rm --privileged --network=host \
#       -v <build_dir>:/build <IMAGE> bash /build/mkarchiso-in-container.sh \
#       /build/profile /build/out
#
# Args: PROFILE (mkarchiso profile dir) OUT (ISO output dir).
set -euo pipefail

PROFILE="${1:?usage: mkarchiso-in-container.sh PROFILE OUT}"
OUT="${2:?usage: mkarchiso-in-container.sh PROFILE OUT}"
WORK="/tmp/work"

# Pin a reliable mirror set + serialize downloads. Concurrency was
# triggering TLS 'bad record mac' corruption on the large
# linux-firmware packages during the spike; ParallelDownloads = 1
# keeps a single TLS stream at a time.
cat > /etc/pacman.d/mirrorlist <<'MIRR'
Server = https://geo.mirror.pkgbuild.com/$repo/os/$arch
Server = https://mirror.rackspace.com/archlinux/$repo/os/$arch
Server = https://mirrors.kernel.org/archlinux/$repo/os/$arch
MIRR
sed -i 's/^#*ParallelDownloads.*/ParallelDownloads = 1/' /etc/pacman.conf
grep -q '^ParallelDownloads' /etc/pacman.conf || echo 'ParallelDownloads = 1' >> /etc/pacman.conf

echo "== installing archiso =="
pacman -Sy --noconfirm --needed archiso >/dev/null
echo "archiso: $(pacman -Q archiso)"

# mkarchiso pacstraps the live env using the PROFILE's own pacman.conf;
# serialize there too so the big firmware pull doesn't corrupt.
sed -i 's/^#*ParallelDownloads.*/ParallelDownloads = 1/' "$PROFILE/pacman.conf" || true
grep -q '^ParallelDownloads' "$PROFILE/pacman.conf" || echo 'ParallelDownloads = 1' >> "$PROFILE/pacman.conf"

rm -rf "$WORK"
mkdir -p "$WORK" "$OUT"

echo "== mkarchiso -w $WORK -o $OUT $PROFILE =="
mkarchiso -v -w "$WORK" -o "$OUT" "$PROFILE"

echo "== build output =="
ls -la "$OUT"
