"""TFTP subprocess supervision.

pixie ships an in-container ``in.tftpd`` (from the tftpd-hpa package)
so a target's BIOS-PXE / UEFI-PXE first hop can chain into pixie's
HTTP bootstrap without an external TFTP daemon on the LAN.

The supervisor is a thin wrapper around ``subprocess.Popen`` tuned to
pixie's FastAPI lifespan: start on app boot, poll for early death
(same 200ms grace as :class:`pixie.exports._supervisor.NbdServer`),
terminate on shutdown.

Runs OFF by default in unit / dev where udp/69 needs root -- flipped
on inside the container via ``PIXIE_TFTP_ENABLED=1`` (set by the
compose file's default env).
"""

from __future__ import annotations

from pixie.tftp._supervisor import DEFAULT_TFTP_ROOT, TftpServer

__all__ = ["DEFAULT_TFTP_ROOT", "TftpServer"]
