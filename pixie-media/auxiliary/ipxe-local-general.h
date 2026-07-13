/* pixie-server's iPXE feature trims.
 *
 * Copied into iPXE's ``src/config/local/general.h`` at build
 * time by ``cijoe/scripts/pixie_ipxe_build.py``. iPXE's build
 * system uses ``src/config/local/*.h`` as override headers
 * applied after the defaults in ``src/config/general.h``.
 *
 * We need a slim binary -- just enough to:
 *   1. DHCP for an IP + next-server.
 *   2. ``chain http://...`` to pixie-web's pxe-bootstrap.ipxe.
 *
 * Everything else gets dropped to keep the binary close to
 * Debian's stock 996 KB (which we know the test firmware
 * accepts). Trims, with rough size impact:
 *
 *   - HTTPS / TLS:                ~150 KB
 *   - FTP / NFS / SLAM:           ~30  KB
 *   - 802.11 wireless:            ~50  KB
 *   - Optional commands:          ~10  KB
 *   - Alternate image formats:    ~30  KB
 */

/* Network protocols: keep HTTP + TFTP only. */
#undef DOWNLOAD_PROTO_HTTPS
#undef DOWNLOAD_PROTO_FTP
#undef DOWNLOAD_PROTO_SLAM
#undef DOWNLOAD_PROTO_NFS

/* Drop wireless entirely -- pixie targets are wired. */
#undef NET80211
#undef CRYPTO_80211_WEP
#undef CRYPTO_80211_WPA
#undef CRYPTO_80211_WPA2

/* Drop optional command surface; the chain script needs only
 * ``dhcp``, ``chain``, ``echo``, ``shell``, ``goto``. */
#undef NSLOOKUP_CMD
#undef TIME_CMD
#undef DIGEST_CMD
#undef LOTEST_CMD
#undef VLAN_CMD
#undef PXE_CMD
#undef REBOOT_CMD
#undef POWEROFF_CMD
#undef IMAGE_TRUST_CMD
#undef PCI_CMD
#undef PARAM_CMD
#undef NEIGHBOUR_CMD
#undef PING_CMD
#undef CONSOLE_CMD
#undef IPSTAT_CMD
#undef PROFSTAT_CMD
#undef NTP_CMD
#undef CERT_CMD

/* Drop alternative image formats. The chain target is always
 * an iPXE script over HTTP (which iPXE runs as IMAGE_SCRIPT)
 * or eventually a UEFI EFI binary (IMAGE_EFI). */
#undef IMAGE_NBI
#undef IMAGE_BZIMAGE
#undef IMAGE_MULTIBOOT
#undef IMAGE_PXE
#undef IMAGE_ELF
#undef IMAGE_COMBOOT
#undef IMAGE_EFI_RUNTIME
