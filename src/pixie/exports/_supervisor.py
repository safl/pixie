"""NbdServer: one nbdkit subprocess per registered export.

Port allocation: :attr:`port_base` is scanned upward; a running nbdkit
holds its port for the export's lifetime. :meth:`reload` diff-syncs the
running processes against a desired export list (spawn missing, kill
dropped). :meth:`stop` kills everything.

Idempotent + thread-safe: :meth:`spawn` on an already-alive export is
a no-op; :meth:`terminate` on an unknown export is a no-op.

Filter chain:
* ``--filter=cow`` always. Under nbdkit >= 1.44 this is safe with
  per-connection named exports; earlier nbdkit silently corrupts
  under this combination (the base container image pins ubuntu:26.04
  which ships 1.46 for that reason).
* ``--filter=partition`` when the blob's boot sector has an MBR/GPT
  magic. Full-disk images (nosi's ``img.gz`` shape) need this so
  ``nbd0p1`` shows up; raw filesystem images do not.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

_log = logging.getLogger(__name__)

DEFAULT_NBDKIT_BIN = "nbdkit"
DEFAULT_PORT_BASE = 10809
_PORT_SCAN_WIDTH = 256
_SPAWN_STARTUP_GRACE = 0.2  # seconds -- give nbdkit a moment to bind + fail


def file_looks_partitioned(path: Path) -> bool:
    """True iff the file at ``path`` has an MBR/GPT partition table.

    Checks the classic boot-sector magic ``0x55 0xAA`` at bytes
    510-511. Covers MBR + protective-MBR-for-GPT + hybrid disks.
    Files smaller than 512 bytes, unreadable, or read-erroring return
    False (safer default: skip the partition filter -- if we're
    wrong, the boot fails loudly on mount instead of silently
    stripping to partition 1 of a non-partitioned image).
    """
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return False
    return len(head) == 512 and head[510:512] == b"\x55\xaa"


def _port_available(bind: str, port: int) -> bool:
    """True iff ``bind:port`` accepts a fresh bind. The ~1ms race
    between this check and nbdkit's own bind is fine: nbdkit exits
    loudly on bind failure and the caller reports the error."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((bind, port))
    except OSError:
        return False
    else:
        return True
    finally:
        with contextlib.suppress(OSError):
            s.close()


class NbdServer:
    """Manages nbdkit subprocesses for a collection of exports.

    ``port_base`` is the first port scanned; each spawn takes the
    next free port in a window of :data:`_PORT_SCAN_WIDTH`. The
    server does NOT persist ports; callers who need to survive
    process restart write ``NbdServer.port_for(name)`` to their
    own store post-spawn.
    """

    def __init__(
        self,
        *,
        port_base: int = DEFAULT_PORT_BASE,
        bind: str = "0.0.0.0",
        nbdkit_bin: str = DEFAULT_NBDKIT_BIN,
    ) -> None:
        self.port_base = port_base
        self.bind = bind
        self.bin = nbdkit_bin
        self._procs: dict[str, subprocess.Popen[bytes]] = {}
        self._ports: dict[str, int] = {}
        self._paths: dict[str, Path] = {}
        self._lock = threading.Lock()

    # ---------- public API ------------------------------------------

    def spawn(self, name: str, blob_path: Path) -> int:
        """Spawn nbdkit for ``name`` serving ``blob_path``. Idempotent.

        Returns the port the export listens on (allocated fresh or
        reused if already running). Raises :class:`RuntimeError` if
        nbdkit refuses to start (bad binary, port collision, missing
        file).
        """
        with self._lock:
            return self._spawn_locked(name, blob_path)

    def terminate(self, name: str) -> bool:
        """Kill the nbdkit for ``name``. Returns True iff a process
        was actually killed. No-op if the export isn't running."""
        with self._lock:
            return self._terminate_locked(name)

    def reload(self, desired: dict[str, Path]) -> None:
        """Diff-sync against ``desired``: spawn every name we're not
        already serving, kill every running export that dropped out of
        the map. Order-independent."""
        with self._lock:
            # Kill dropped
            for name in list(self._procs):
                if name not in desired:
                    self._terminate_locked(name)
            # Spawn desired
            for name, path in desired.items():
                self._spawn_locked(name, path)

    def stop(self) -> None:
        """Kill every running nbdkit. Called on app shutdown."""
        with self._lock:
            for name in list(self._procs):
                self._terminate_locked(name)

    def is_running(self, name: str) -> bool:
        with self._lock:
            proc = self._procs.get(name)
            return proc is not None and proc.poll() is None

    def port_for(self, name: str) -> int | None:
        with self._lock:
            return self._ports.get(name)

    def running_exports(self) -> dict[str, int]:
        """Snapshot of currently-running exports and their ports."""
        with self._lock:
            return {n: p for n, p in self._ports.items() if self._procs[n].poll() is None}

    # ---------- internals -------------------------------------------

    def _spawn_locked(self, name: str, blob_path: Path) -> int:
        """Requires ``self._lock``. Idempotent per name."""
        existing = self._procs.get(name)
        if existing is not None and existing.poll() is None:
            return self._ports[name]

        # Reap dead entry so its port slot is available for reuse.
        if existing is not None:
            self._procs.pop(name, None)
            self._ports.pop(name, None)
            self._paths.pop(name, None)

        if not blob_path.is_file():
            raise RuntimeError(f"blob {blob_path!s} does not exist")

        port = self._allocate_port_locked()

        argv: list[str] = [
            self.bin,
            "-p",
            str(port),
            "--ipaddr",
            self.bind,
            "-f",
            "--newstyle",
            "-e",
            name,
            "--filter=cow",
        ]
        # --filter=partition must sit BELOW --filter=cow (nearer the
        # plugin) so client writes land in the cow overlay, not in a
        # partition-filter-managed slice of the backing.
        partitioned = file_looks_partitioned(blob_path)
        if partitioned:
            argv.append("--filter=partition")
        # nbdkit's arg parser treats the first non-flag as the plugin
        # name and everything after as KEY=VALUE plugin params.
        # Filter params come after the plugin.
        argv.extend(["file", f"file={blob_path!s}"])
        if partitioned:
            argv.append("partition=1")

        _log.info("nbdkit spawn %r on port %d: %s", name, port, blob_path)
        try:
            proc = subprocess.Popen(argv, stdout=sys.stderr, stderr=sys.stderr)
        except FileNotFoundError as exc:
            raise RuntimeError(f"nbdkit binary not found: {self.bin!r}") from exc

        # Give the child a moment to bind or fail loudly.
        time.sleep(_SPAWN_STARTUP_GRACE)
        if proc.poll() is not None:
            rc = proc.returncode
            raise RuntimeError(
                f"nbdkit for export {name!r} exited immediately (rc={rc}, port={port}, "
                f"file={blob_path!s}); check the binary + file exist and the port is free"
            )

        self._procs[name] = proc
        self._ports[name] = port
        self._paths[name] = blob_path
        return port

    def _terminate_locked(self, name: str) -> bool:
        """Requires ``self._lock``. Returns True iff we killed a proc."""
        proc = self._procs.pop(name, None)
        self._ports.pop(name, None)
        self._paths.pop(name, None)
        if proc is None:
            return False
        if proc.poll() is None:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=3)
            if proc.poll() is None:
                proc.kill()
        return True

    def _allocate_port_locked(self) -> int:
        """Return the next free port at or above ``self.port_base``."""
        used = set(self._ports.values())
        for p in range(self.port_base, self.port_base + _PORT_SCAN_WIDTH):
            if p in used:
                continue
            if _port_available(self.bind, p):
                return p
        raise RuntimeError(
            f"no free TCP port in range {self.port_base}..{self.port_base + _PORT_SCAN_WIDTH}"
        )
