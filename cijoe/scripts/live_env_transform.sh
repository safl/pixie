#!/usr/bin/env bash
#
# live_env_transform.sh
# =====================
#
# Turn a nosi ``arch-headless`` base disk image into the
# ``pixie-live-env`` image that pixie nbdboots ephemerally for the
# live-env boot modes (``pixie-flash-once`` / ``-always`` /
# ``pixie-inventory`` / ``pixie-tui``). This is the TRANSFORM ONLY;
# the orchestration (pull the base via oras, decompress, gzip the
# result, sha256, size-gate, emit the catalog fragment) lives in the
# cijoe step ``cijoe/scripts/live_env_image.py`` which invokes this
# script.
#
# The base is nosi ``arch-headless`` -- lean by design (~1.58 GiB
# baked, already under GitHub's 2 GiB release-asset limit) with the
# CLI's runtime deps (python3, lshw, parted, curl, nvme) already
# present. So the transform is essentially inject-only: vendor the
# pixie CLI + drop the ``pixie-on-tty1`` service trio. A conservative,
# arch-path-aware slim runs too, but it is not load-bearing for the
# size gate the way it was for the ubuntu prototype.
#
# What it does:
#   1. qemu-nbd --connect a COPY of the base (never mutate the input),
#      mount its root partition read-write, with --discard=unmap so a
#      later fstrim hole-punches the freed blocks for compression.
#   2. Vendor the pixie CLI (NO pip-in-chroot): the caller passes a
#      pre-built vendored tree via --vendor; we rsync it into
#      <rootfs>/opt/pixie/lib and drop a launcher /usr/local/bin/pixie
#      that runs the image's stock python3 with
#      PYTHONPATH=/opt/pixie/lib. Verify the import under the image's
#      own python3.
#   3. Port pixie-on-tty1.service + /usr/local/sbin/{pixie-on-tty1,
#      pixie-trace} from the pixie-media archiso airootfs tree; enable
#      via a multi-user.target.wants symlink.
#   4. Conservative slim (docs/man/locale/pkg-cache/__pycache__), then
#      fstrim to reclaim the freed blocks.
#
# Usage:
#   sudo ./live_env_transform.sh \
#       --base   /path/to/arch-headless.img         (raw or qcow2) \
#       --out    /path/to/pixie-live-env.img \
#       --vendor /path/to/opt-pixie-lib             (pip --target tree) \
#       --media  /path/to/pixie-media               (service trio source) \
#       [--force]
#
# Requires: sudo (qemu-nbd + mount need root), qemu-nbd + qemu-img
# (qemu-utils), the ``nbd`` kernel module, rsync.

set -euo pipefail

BASE=""
OUT=""
VENDOR=""
MEDIA=""
FORCE=0

die()  { printf 'live_env_transform: ERROR: %s\n' "$*" >&2; exit 1; }
warn() { printf 'live_env_transform: WARN:  %s\n' "$*" >&2; }
info() { printf 'live_env_transform: %s\n' "$*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --base)   BASE="${2:?--base needs a value}";   shift 2 ;;
        --out)    OUT="${2:?--out needs a value}";     shift 2 ;;
        --vendor) VENDOR="${2:?--vendor needs a value}"; shift 2 ;;
        --media)  MEDIA="${2:?--media needs a value}"; shift 2 ;;
        --force)  FORCE=1; shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done

[ -n "$BASE" ]   || die "--base is required"
[ -n "$OUT" ]    || die "--out is required"
[ -n "$VENDOR" ] || die "--vendor is required"
[ -n "$MEDIA" ]  || die "--media is required"
[ -f "$BASE" ]   || die "base image not found: $BASE"
[ -d "$VENDOR" ] || die "vendored CLI tree not found: $VENDOR"
[ -d "$MEDIA" ]  || die "pixie-media tree not found: $MEDIA"
if [ -e "$OUT" ] && [ "$FORCE" -ne 1 ]; then
    die "--out already exists: $OUT (pass --force)"
fi

# The slim list: paths relative to the image root, removed if present.
# Arch's base is already lean, so this is conservative -- documentation,
# man/info pages, the pacman package cache + sync DBs (the appliance
# never runs pacman), and logs. NEVER lists the kernel, initramfs,
# systemd, python3, /opt/pixie, or any driver module tree.
SLIM_PATHS=(
    usr/share/doc
    usr/share/man
    usr/share/info
    var/cache/pacman/pkg
    var/lib/pacman/sync
    var/log
    var/tmp
    tmp
)
# Locale dirs to KEEP under usr/share/locale (basename globs); the rest
# are removed. Arch ships few locales in headless, but this is cheap.
LOCALE_KEEP=('C' 'C.*' 'en' 'en_*' 'en@*')

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
command -v qemu-nbd >/dev/null 2>&1 || die "qemu-nbd not on PATH (install qemu-utils)"
command -v qemu-img >/dev/null 2>&1 || die "qemu-img not on PATH (install qemu-utils)"
command -v rsync    >/dev/null 2>&1 || die "rsync not on PATH"

WORK="$(mktemp -d /tmp/pixie-live-env.XXXXXX)"
MNT="$WORK/mnt"
mkdir -p "$MNT"

NBD_DEV=""
BINDS_DONE=0
ROOT_MOUNTED=0
cleanup() {
    set +e
    if [ "$BINDS_DONE" -eq 1 ]; then
        for sub in dev/pts dev proc sys run; do
            ${SUDO} umount -lf "$MNT/$sub" 2>/dev/null
        done
    fi
    [ "$ROOT_MOUNTED" -eq 1 ] && ${SUDO} umount -lf "$MNT" 2>/dev/null
    [ -n "$NBD_DEV" ] && ${SUDO} qemu-nbd --disconnect "$NBD_DEV" >/dev/null 2>&1
    rmdir "$MNT" 2>/dev/null
    rm -rf "$WORK" 2>/dev/null
}
trap cleanup EXIT

# ----------------------------------------------------------------------
# 0. Copy the base to --out (never touch the base blob). Keep the output
#    RAW: the orchestrator gzips it and content-addresses on the raw
#    sha, matching how pixie fetches an ``img.gz`` (decompress -> sha of
#    the raw disk image).
# ----------------------------------------------------------------------
BASE_FMT="$(qemu-img info --output=json "$BASE" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["format"])')"
info "base format: $BASE_FMT -> writing raw $OUT"
qemu-img convert -O raw "$BASE" "$OUT"
OUT_FMT="raw"

# ----------------------------------------------------------------------
# 1. qemu-nbd connect + mount the root partition.
# ----------------------------------------------------------------------
${SUDO} modprobe nbd max_part=16 2>/dev/null || die "cannot load the 'nbd' kernel module"

find_free_nbd() {
    local dev size
    for dev in /sys/class/block/nbd*; do
        [ -e "$dev" ] || continue
        case "$(basename "$dev")" in nbd*p*) continue ;; esac
        size="$(cat "$dev/size" 2>/dev/null || echo 0)"
        if [ "$size" = "0" ]; then echo "/dev/$(basename "$dev")"; return 0; fi
    done
    return 1
}

NBD_DEV="$(find_free_nbd)" || die "no free /dev/nbdX device"
info "attaching $OUT to $NBD_DEV ..."
${SUDO} qemu-nbd --connect="$NBD_DEV" -f "$OUT_FMT" --discard=unmap "$OUT" \
    || die "qemu-nbd --connect failed"
${SUDO} partprobe "$NBD_DEV" 2>/dev/null || true
${SUDO} udevadm settle 2>/dev/null || true
sleep 1

# Find the root partition: the ext4/xfs/btrfs partition carrying
# /etc/os-release + an init. Probe rather than hardcode a partition
# index (ESP + root layouts vary).
find_root_part() {
    local part dev fstype
    for part in $(lsblk -rno NAME "$NBD_DEV" | tail -n +2); do
        dev="/dev/$part"
        fstype="$(${SUDO} blkid -o value -s TYPE "$dev" 2>/dev/null || true)"
        case "$fstype" in ext4|ext3|xfs|btrfs) : ;; *) continue ;; esac
        if ${SUDO} mount -o ro "$dev" "$MNT" 2>/dev/null; then
            if ${SUDO} test -f "$MNT/etc/os-release" \
               && { ${SUDO} test -e "$MNT/sbin/init" || ${SUDO} test -L "$MNT/sbin/init"; }; then
                ${SUDO} umount "$MNT"; echo "$dev"; return 0
            fi
            ${SUDO} umount "$MNT"
        fi
    done
    return 1
}

ROOT_DEV="$(find_root_part)" || die "could not find a root partition under $NBD_DEV"
info "root partition: $ROOT_DEV"
${SUDO} mount "$ROOT_DEV" "$MNT" || die "mount $ROOT_DEV failed"
ROOT_MOUNTED=1
info "mounted rootfs: $(${SUDO} sh -c "grep -s '^PRETTY_NAME=' '$MNT/etc/os-release' | cut -d= -f2- | tr -d '\"'" || echo unknown)"

${SUDO} mount --bind /proc "$MNT/proc"
${SUDO} mount --bind /sys  "$MNT/sys"
${SUDO} mount --bind /dev  "$MNT/dev"
${SUDO} mount --bind /dev/pts "$MNT/dev/pts"
${SUDO} mount -t tmpfs tmpfs "$MNT/run" 2>/dev/null || ${SUDO} mount --bind /run "$MNT/run"
BINDS_DONE=1
in_chroot() { ${SUDO} chroot "$MNT" "$@"; }

# ----------------------------------------------------------------------
# 2. Vendor the pixie CLI into /opt/pixie/lib (pre-built by the caller).
# ----------------------------------------------------------------------
info "vendoring the pixie CLI tree into /opt/pixie/lib ..."
${SUDO} mkdir -p "$MNT/opt/pixie/lib"
${SUDO} rsync -a --delete "$VENDOR"/ "$MNT/opt/pixie/lib/"

LAUNCHER="$MNT/usr/local/bin/pixie"
${SUDO} mkdir -p "$MNT/usr/local/bin"
${SUDO} tee "$LAUNCHER" >/dev/null <<'LAUNCH'
#!/bin/sh
# pixie: launch the vendored operator CLI with the image's stock
# python3. The tree under /opt/pixie/lib is a ``pip install --target``
# of pixie-lab + its pure-Python CLI runtime deps, injected at
# image-build time. The CLI reaches the pixie server over stdlib
# urllib, so no httpx / fastapi base deps are needed here.
export PYTHONPATH=/opt/pixie/lib${PYTHONPATH:+:$PYTHONPATH}
exec python3 -c 'from pixie.tui import main; main()' "$@"
LAUNCH
${SUDO} chmod 0755 "$LAUNCHER"

info "verifying the vendored CLI imports under the image's python3 ..."
in_chroot /bin/sh -c 'command -v python3 >/dev/null' \
    || die "the base image has no python3 -- cannot run the pixie CLI"
in_chroot /usr/bin/env PYTHONPATH=/opt/pixie/lib python3 -c \
    'import pixie.tui, rich; print("vendored CLI import OK")' \
    || die "vendored CLI failed to import under the image's python3"

# ----------------------------------------------------------------------
# 3. Port the boot service trio; enable it via the wants symlink.
# ----------------------------------------------------------------------
INCLUDES="$MEDIA/archiso/airootfs"
svc_src="$INCLUDES/etc/systemd/system/pixie-on-tty1.service"
wrap_src="$INCLUDES/usr/local/sbin/pixie-on-tty1"
trace_src="$INCLUDES/usr/local/sbin/pixie-trace"
for f in "$svc_src" "$wrap_src" "$trace_src"; do
    [ -f "$f" ] || die "expected boot-service file missing: $f"
done
info "installing pixie-on-tty1.service + wrapper + pixie-trace ..."
${SUDO} install -D -m 0644 "$svc_src"   "$MNT/etc/systemd/system/pixie-on-tty1.service"
${SUDO} install -D -m 0755 "$wrap_src"  "$MNT/usr/local/sbin/pixie-on-tty1"
${SUDO} install -D -m 0755 "$trace_src" "$MNT/usr/local/sbin/pixie-trace"
if ! in_chroot systemctl enable pixie-on-tty1.service >/dev/null 2>&1; then
    warn "systemctl enable failed in chroot; creating the wants symlink by hand"
    ${SUDO} mkdir -p "$MNT/etc/systemd/system/multi-user.target.wants"
    ${SUDO} ln -sf /etc/systemd/system/pixie-on-tty1.service \
        "$MNT/etc/systemd/system/multi-user.target.wants/pixie-on-tty1.service"
fi

# ----------------------------------------------------------------------
# 4. Slim (conservative), then verify boot-critical bits survived.
# ----------------------------------------------------------------------
info "slimming the rootfs (conservative) ..."
rm_path() {
    if ${SUDO} test -e "$MNT/$1"; then ${SUDO} rm -rf "${MNT:?}/$1"; info "  removed /$1"; fi
}
for p in "${SLIM_PATHS[@]}"; do rm_path "$p"; done

if ${SUDO} test -d "$MNT/usr/share/locale"; then
    find_prune=()
    for keep in "${LOCALE_KEEP[@]}"; do find_prune+=( ! -name "$keep" ); done
    ${SUDO} find "$MNT/usr/share/locale" -mindepth 1 -maxdepth 1 -type d \
        "${find_prune[@]}" -exec rm -rf {} + 2>/dev/null || true
    info "  trimmed /usr/share/locale (kept: ${LOCALE_KEEP[*]})"
fi
${SUDO} find "$MNT" -path "$MNT/boot" -prune -o -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
${SUDO} find "$MNT" -path "$MNT/boot" -prune -o -type f -name '*.pyc' -delete 2>/dev/null || true

info "verifying boot-critical bits survived the slim ..."
check() { if ${SUDO} sh -c "ls $MNT/$1 >/dev/null 2>&1"; then info "  ok: $2"; else die "MISSING: $2 ($1)"; fi; }
check 'usr/bin/python3'          'python3'
check 'opt/pixie/lib/pixie'      'vendored /opt/pixie/lib/pixie'
check 'usr/lib/systemd/systemd'  'systemd'
check 'usr/local/bin/pixie'      'pixie launcher'

# ----------------------------------------------------------------------
# 5. fstrim to hole-punch the freed blocks (qemu-nbd was connected with
#    --discard=unmap), so the orchestrator's gzip shrinks accordingly.
# ----------------------------------------------------------------------
${SUDO} sync
info "fstrim to reclaim freed blocks ..."
${SUDO} fstrim -v "$MNT" 2>/dev/null || warn "fstrim unsupported here; gzip will be looser"

# Tear down BEFORE returning so the image file is consistent on disk.
info "unmounting + disconnecting ..."
${SUDO} umount "$MNT/dev/pts" "$MNT/dev" "$MNT/proc" "$MNT/sys" "$MNT/run" 2>/dev/null || true
BINDS_DONE=0
${SUDO} umount "$MNT" || warn "root umount deferred"
ROOT_MOUNTED=0
${SUDO} qemu-nbd --disconnect "$NBD_DEV" >/dev/null 2>&1 || true
NBD_DEV=""
${SUDO} chown "$(id -u):$(id -g)" "$OUT" 2>/dev/null || true

info "done -- transformed live-env image at $OUT (raw)"
