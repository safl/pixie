#!/usr/bin/env bash
# shellcheck disable=SC2034
#
# pixie usbboot-pc archiso profile -- forked from the archiso ``releng``
# profile (archiso 88). The pixie userspace (operator CLI + boot service
# trio + support units) lives under ``airootfs/``; the package set is
# trimmed to the flash / inventory tooling (no DKMS: the Arch kernel +
# linux-firmware carry the NIC drivers in-tree). Built by
# ``cijoe/scripts/archiso_build.py`` via a privileged ``mkarchiso`` run.
# ``__PIXIE_VERSION__`` placeholders (here + motd / issue / bootloader
# menus) are stamped with the pixie-lab version before the build.

iso_name="pixie-usbboot-pc"
iso_label="PIXIE_$(date --date="@${SOURCE_DATE_EPOCH:-$(date +%s)}" +%y%m)"
iso_publisher="pixie <https://github.com/safl/pixie>"
iso_application="pixie live env (flash / inventory / operator TUI)"
iso_version="__PIXIE_VERSION__"
install_dir="arch"
buildmodes=('iso')
bootmodes=('bios.syslinux'
           'uefi.systemd-boot')
pacman_conf="pacman.conf"
airootfs_image_type="squashfs"
airootfs_image_tool_options=('-comp' 'xz' '-Xbcj' 'x86' '-b' '1M' '-Xdict-size' '1M')
bootstrap_tarball_compression=('zstd' '-c' '-T0' '--auto-threads=logical' '--long' '-19')
file_permissions=(
  ["/etc/shadow"]="0:0:400"
  ["/root"]="0:0:750"
  ["/root/.automated_script.sh"]="0:0:755"
  ["/usr/local/bin/pixie"]="0:0:755"
  ["/usr/local/sbin/pixie-on-tty1"]="0:0:755"
  ["/usr/local/sbin/pixie-trace"]="0:0:755"
  ["/usr/local/sbin/pixie-usb-grow"]="0:0:755"
  ["/usr/local/sbin/pixie-images-discover"]="0:0:755"
  ["/usr/local/sbin/pixie-clock-from-http"]="0:0:755"
  ["/usr/local/sbin/pixie-boot-banner"]="0:0:755"
)
