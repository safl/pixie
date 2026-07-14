"""TftpServer: manage one ``in.tftpd`` child.

Argv shape:

    in.tftpd --foreground --listen --address <bind>:<port> --secure <root>

``--listen`` runs in daemon-listener mode (not inetd); ``--foreground``
keeps it as a supervised child so our poll() sees it live (without it,
tftpd-hpa double-forks and the parent exits rc=0, which our start()
grace period would mis-flag as a startup failure); ``--secure`` chroots
into ``<root>`` so a malformed RRQ can't wander outside the served
directory.

The wrapper is deliberately narrow -- no reload, no diff-sync, no
multi-server. TFTP is boot-time-critical + operator-visible; a single
supervised in.tftpd is the correct shape.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

_log = logging.getLogger(__name__)

DEFAULT_TFTP_BIN = "in.tftpd"
# NBPs get baked into the container image at build time (Containerfile
# copies undionly.kpxe + ipxe.efi from the ipxe package into this path).
# Runtime code should treat this path as read-only.
DEFAULT_TFTP_ROOT = Path("/usr/share/pixie/tftp")
DEFAULT_TFTP_PORT = 69
_SPAWN_STARTUP_GRACE = 0.2  # seconds -- give in.tftpd a moment to bind or fail


class TftpServer:
    """Wraps a single ``in.tftpd`` child. Thread-safe.

    * ``bind``  -- interface to listen on (default ``0.0.0.0``).
    * ``port``  -- UDP port (default 69). Non-root callers must
      override; udp/69 requires ``CAP_NET_BIND_SERVICE`` or root.
    * ``root``  -- directory served. Callers guarantee this exists
      + is readable; the class does not create it.
    * ``bin``   -- path to the ``in.tftpd`` binary. Overridable for
      unit tests that pretend they have a tftp server.
    """

    def __init__(
        self,
        *,
        bind: str = "0.0.0.0",
        port: int = DEFAULT_TFTP_PORT,
        root: Path = DEFAULT_TFTP_ROOT,
        bin: str = DEFAULT_TFTP_BIN,
    ) -> None:
        self.bind = bind
        self.port = port
        self.root = Path(root)
        self.bin = bin
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        """Spawn ``in.tftpd``. Idempotent: no-op if already running.

        Raises :class:`RuntimeError` if the binary is missing, the
        root directory does not exist, or the child exits within the
        spawn grace period.
        """
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            self._proc = None

            if not self.root.is_dir():
                raise RuntimeError(f"tftp root does not exist: {self.root!s}")

            argv = [
                self.bin,
                # ``--foreground`` is essential -- without it,
                # tftpd-hpa's ``--listen`` mode daemonises (double-forks
                # + parent exits rc=0) and our supervisor mis-detects
                # the immediate parent-exit as a startup failure even
                # though the grandchild is happily bound to udp/69.
                # Verified live 2026-07-14 on 10.20.30.10 booting a
                # real UEFI target.
                "--foreground",
                "--listen",
                "--address",
                f"{self.bind}:{self.port}",
                "--secure",
                str(self.root),
            ]
            _log.info("tftp spawn: %s", " ".join(argv))
            try:
                proc = subprocess.Popen(argv, stdout=sys.stderr, stderr=sys.stderr)
            except FileNotFoundError as exc:
                raise RuntimeError(f"tftp binary not found: {self.bin!r}") from exc

            time.sleep(_SPAWN_STARTUP_GRACE)
            if proc.poll() is not None:
                rc = proc.returncode
                raise RuntimeError(
                    f"in.tftpd exited immediately (rc={rc}, bind={self.bind}:{self.port}, "
                    f"root={self.root!s}); check binary exists + port + root permissions"
                )
            self._proc = proc

    def stop(self) -> None:
        """Terminate the child. Idempotent."""
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=3)
            if proc.poll() is None:
                proc.kill()

    def is_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None
