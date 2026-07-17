"""
PXE bootstrap chain test: containerized pixie + a bridged QEMU client
=====================================================================

Minimal end-to-end proof that pixie's PXE stack is wired correctly:

1. Bring pixie up as a container on the host network with
   ``PIXIE_ADMIN_PASSWORD`` set. Wait for ``/healthz``.
2. Create a host bridge that carries a server-side IP and a client
   tap on it. The tap is owned by the current user so the (non-root)
   QEMU client can open it.
3. Start a test-side dnsmasq on the bridge that:
   - hands the QEMU client a DHCP lease
   - hands the firmware ``undionly.kpxe`` (BIOS) or ``ipxe.efi``
     (UEFI) over TFTP
   - once the client re-DHCPs with the ``iPXE`` user-class, hands
     it ``http://<server>:8080/pxe-bootstrap.ipxe`` as the bootfile
4. Start a QEMU client VM with ``-boot n``, tap NIC, blank disk,
   serial-console to a file.
5. Tail the client serial log until every marker in
   ``[test.pxe.chain_markers]`` appears (or the timeout fires).
6. On failure, dump the tail of the serial log + the tail of
   ``podman logs pixie-pxe-test`` for the post-mortem.

The bind + flash portion (catalog seed + ramboot + NBD serve) is
NOT exercised here; landing that needs the fetch-verb wire-up and
pre-seeded artifacts. This test proves the *bootstrap* chain works
end-to-end: real firmware PXE, real DHCP, real TFTP, real iPXE
chainload, real pixie HTTP response.

Retargetable: False
"""

from __future__ import annotations

import errno
import functools
import http.server
import json
import logging as log
import shutil
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.request
from argparse import ArgumentParser
from pathlib import Path

PIXIE_HTTP_PORT = 8080
CONTAINER_NAME = "pixie-pxe-test"
CONTAINER_TAG = "pixie:pxetest"
HEALTHZ_TIMEOUT = 120
CHAIN_TIMEOUT = 300
FETCH_TIMEOUT = 120  # ramboot / flash: catalog fetch of the payload
WORKSPACE_HTTP_PORT = 8000  # test-side http server hosting workspace files on the bridge
# QEMU virtio-blk serial the test binds pixie-flash-once to. Chosen
# to be a plain-ASCII no-punctuation string because the pixie CLI's
# ``disks.list_disks`` reads serials via lsblk + they must round-trip
# through JSON with no escapes.
FLASH_TARGET_SERIAL = "PIXIETEST"


def add_args(parser: ArgumentParser) -> None:
    del parser


def main(args, cijoe) -> int:
    del args
    cfg = cijoe.getconf("test.pxe", {})
    if not cfg:
        log.error("missing [test.pxe] section in cijoe config")
        return errno.EINVAL

    workspace = Path.cwd() / "_build" / "test-pxe"
    tftproot = workspace / "tftproot"
    workspace.mkdir(parents=True, exist_ok=True)

    image = cfg.get("pixie_image", CONTAINER_TAG)
    admin_password = cfg["admin_password"]
    seed_base = f"http://127.0.0.1:{PIXIE_HTTP_PORT}"
    client_log = workspace / "client.serial.log"
    if client_log.exists():
        client_log.unlink()

    mode = str(cfg.get("mode", "bootstrap")).lower()
    if mode not in ("bootstrap", "ramboot", "inventory", "flash"):
        log.error(
            f"unknown [test.pxe] mode={mode!r}; expected "
            "'bootstrap', 'ramboot', 'inventory', or 'flash'"
        )
        return errno.EINVAL

    container = None
    dnsmasq = None
    workspace_http = None
    client = None
    net_up = False
    try:
        _setup_network(cfg, tftproot)
        net_up = True
        dnsmasq = _start_dnsmasq(cfg, tftproot, workspace)

        # ``inventory`` + ``flash`` both need pixie's own live-env
        # media staged inside the container (they boot into the pixie
        # live env before the CLI can post inventory or auto-flash);
        # nothing else does. Verify the caller's workspace has the
        # three files before we start podman so a missing bake fails
        # fast rather than mid-boot on the client.
        live_env_dir: Path | None = None
        if mode in ("inventory", "flash"):
            live_env_dir = workspace / "live-env"
            missing = [
                name
                for name in ("vmlinuz", "initrd", "live.squashfs")
                if not (live_env_dir / name).is_file()
            ]
            if missing:
                stage_step = "pxe_inventory_stage" if mode == "inventory" else "pxe_flash_stage"
                log.error(
                    f"mode={mode} needs live-env media at {live_env_dir}; "
                    f"missing: {missing}. Run {stage_step} first."
                )
                return errno.ENOENT

        container = _run_container(image, admin_password, live_env_dir=live_env_dir)
        log.info(f"Waiting for pixie /healthz on {seed_base}")
        if not _wait_until(lambda: _http_ready(seed_base), HEALTHZ_TIMEOUT, "pixie /healthz"):
            log.error("pixie container did not become healthy; logs:")
            _dump_container_logs()
            return errno.ETIMEDOUT

        if mode == "inventory":
            log.info("Binding machine to boot_mode=pixie-inventory")
            seed_err = _bind_inventory(seed_base, admin_password, cfg)
            if seed_err:
                log.error(f"inventory bind failed: rc={seed_err}")
                _dump_container_logs()
                return seed_err
        elif mode == "ramboot":
            workspace_http = _start_workspace_http_server(
                workspace, cfg["server_pxe_ip"], required=("bundle.tar.gz", "disk.img")
            )
            if workspace_http is None:
                return errno.ENOENT  # error already logged
            log.info("Seeding pixie catalog + binding machine to ramboot")
            seed_err = _seed_ramboot_and_bind(seed_base, admin_password, cfg)
            if seed_err:
                log.error(f"ramboot seed failed: rc={seed_err}")
                _dump_container_logs()
                return seed_err
        elif mode == "flash":
            workspace_http = _start_workspace_http_server(
                workspace, cfg["server_pxe_ip"], required=("flash-target.img",)
            )
            if workspace_http is None:
                return errno.ENOENT  # error already logged
            log.info(
                "Seeding pixie catalog + binding machine to "
                f"{cfg.get('flash_boot_mode', 'pixie-flash-once')}"
            )
            seed_err = _seed_flash_and_bind(seed_base, admin_password, cfg)
            if seed_err:
                log.error(f"flash seed failed: rc={seed_err}")
                _dump_container_logs()
                return seed_err

        firmware = str(cfg.get("client_firmware", "bios")).lower()
        if firmware == "uefi" and _find_ovmf() is None:
            log.warning("client_firmware=uefi but no OVMF found; falling back to BIOS")
            firmware = "bios"
        log.info(f"Starting client VM (firmware={firmware}, PXE boot on {cfg['tap_iface']})")
        client = _start_client_vm(workspace, cfg, client_log, firmware)

        markers = _build_markers(cfg)
        seen = _wait_for_chain_markers(client_log, markers, CHAIN_TIMEOUT)
        missing = [k for k, ok in seen.items() if not ok]
        if missing:
            log.error(f"PXE chain incomplete; missing markers: {', '.join(missing)}")
            # Client process state: an early QEMU exit points at
            # firmware / KVM / tap problems that swallowed the boot
            # before serial output could happen.
            rc = client.poll() if client is not None else None
            if rc is not None:
                log.error(f"client QEMU exited early with rc={rc}")
            else:
                log.error("client QEMU still running at timeout (booted but no marker match)")
            qemu_log = client_log.with_suffix(".qemu.log")
            _dump_tail(qemu_log, 60)
            _dump_tail(workspace / "dnsmasq.log", 60)
            _dump_tail(client_log, 200)
            _dump_container_logs()
            return errno.EPROTO

        # Ramboot + inventory both prove the server-side inventory
        # roundtrip: the live env's pixie CLI POSTs the blob after
        # boot; here we GET it back from pixie's state.db and assert
        # it holds a non-empty disks list. Different chain shape (NBD
        # for ramboot vs static live-env for inventory) hits the same
        # inventory POST code path. Flash mode has its own post-chain
        # assertions (mode flip + written marker) instead.
        if mode in ("ramboot", "inventory"):
            inv_err = _verify_server_inventory(seed_base, cfg["client_mac"])
            if inv_err:
                _dump_container_logs()
                return inv_err
        elif mode == "flash":
            flash_err = _verify_flash_effects(seed_base, cfg, workspace)
            if flash_err:
                _dump_container_logs()
                return flash_err

        log.info(f"PXE {mode} chain test PASSED (all markers seen)")
        return 0
    finally:
        if client is not None:
            _terminate(client, "client VM")
        _stop_container(container)
        if dnsmasq is not None:
            _terminate(dnsmasq, "dnsmasq", sudo=True)
        if workspace_http is not None:
            workspace_http.shutdown()
            workspace_http.server_close()
        if net_up:
            _teardown_network(cfg)


# ---------- network: host bridge + tap + dnsmasq ---------------------------


def _setup_network(cfg, tftproot: Path) -> None:
    bridge = cfg["bridge"]
    tap = cfg["tap_iface"]
    ip = cfg["server_pxe_ip"]
    user = _whoami()

    _teardown_network(cfg)  # idempotent: clear any leftovers from a prior run
    _sudo(["ip", "link", "add", bridge, "type", "bridge"])
    _sudo(["ip", "addr", "add", f"{ip}/24", "dev", bridge])
    _sudo(["ip", "link", "set", bridge, "up"])
    _sudo(["ip", "tuntap", "add", "dev", tap, "mode", "tap", "user", user])
    _sudo(["ip", "link", "set", tap, "master", bridge])
    _sudo(["ip", "link", "set", tap, "up"])

    # On a runner with docker installed, br_netfilter is loaded and the
    # FORWARD policy is DROP, so frames crossing a Linux bridge get
    # passed to iptables and the client's DHCP broadcast can be dropped.
    # Bypass iptables entirely on this synthetic test bridge.
    for k in (
        "net.bridge.bridge-nf-call-iptables=0",
        "net.bridge.bridge-nf-call-ip6tables=0",
        "net.bridge.bridge-nf-call-arptables=0",
    ):
        _sudo(["sysctl", "-w", k], check=False)

    tftproot.mkdir(parents=True, exist_ok=True)
    for nbp in ("undionly.kpxe", "ipxe.efi"):
        src = Path("/usr/lib/ipxe") / nbp
        if src.is_file():
            shutil.copy2(src, tftproot / nbp)
        else:
            log.warning(f"iPXE NBP not found: {src} (install the 'ipxe' package)")


def _teardown_network(cfg) -> None:
    bridge = cfg["bridge"]
    tap = cfg["tap_iface"]
    _sudo(["ip", "link", "set", tap, "down"], check=False)
    _sudo(["ip", "link", "del", tap], check=False)
    _sudo(["ip", "link", "set", bridge, "down"], check=False)
    _sudo(["ip", "link", "del", bridge], check=False)


def _start_dnsmasq(cfg, tftproot: Path, workspace: Path):
    """Test-side dnsmasq on the bridge: DHCP + TFTP, chainloading pixie's
    HTTP iPXE bootstrap. Pixie serves no DHCP; this is the synthetic
    segment's only DHCP source."""
    conf = workspace / "dnsmasq.conf"
    server_ip = cfg["server_pxe_ip"]
    # dnsmasq is launched as root (via sudo) but drops privileges after
    # binding its sockets. Its default drop target is ``nobody``/
    # ``dnsmasq``, which on a CI runner cannot traverse the 0750
    # ``/home/<user>`` to reach the workspace tftp-root. Pin the drop
    # target to the user who owns the workspace + tap.
    user = _whoami()
    conf.write_text(
        "# Test-only DHCP+TFTP for the synthetic PXE bridge (test\n"
        "# machinery, not part of pixie: production relies on the\n"
        "# operator's LAN DHCP).\n"
        "port=0\n"
        f"user={user}\n"
        "log-dhcp\n"
        f"interface={cfg['bridge']}\n"
        "bind-interfaces\n"
        "except-interface=lo\n"
        f"dhcp-range={cfg['dhcp_range_lo']},{cfg['dhcp_range_hi']},{cfg['pxe_netmask']},1h\n"
        "enable-tftp\n"
        f"tftp-root={tftproot}\n"
        "dhcp-match=set:bios,option:client-arch,0\n"
        "dhcp-match=set:efi,option:client-arch,7\n"
        "dhcp-match=set:efi,option:client-arch,9\n"
        "dhcp-userclass=set:ipxe,iPXE\n"
        "dhcp-boot=tag:!ipxe,tag:bios,undionly.kpxe\n"
        "dhcp-boot=tag:!ipxe,tag:efi,ipxe.efi\n"
        f"dhcp-boot=tag:ipxe,http://{server_ip}:{PIXIE_HTTP_PORT}/pxe-bootstrap.ipxe\n",
        encoding="utf-8",
    )
    log_path = workspace / "dnsmasq.log"
    proc = subprocess.Popen(
        [
            "sudo",
            "-n",
            "dnsmasq",
            "--keep-in-foreground",
            "--log-facility=-",
            f"--conf-file={conf}",
        ],
        stdin=subprocess.DEVNULL,
        stdout=open(log_path, "wb"),  # noqa: SIM115 - lives for the dnsmasq process
        stderr=subprocess.STDOUT,
    )
    return proc


def _whoami() -> str:
    import getpass

    return getpass.getuser()


# ---------- pixie container ------------------------------------------------


def _run_container(image: str, admin_password: str, *, live_env_dir: Path | None = None):
    """Run the pixie container detached on host networking with
    ``PIXIE_ADMIN_PASSWORD`` set. Host networking keeps ``/healthz``
    reachable via loopback while the client's PXE HTTP fetch hits the
    same process via the bridge IP.

    ``live_env_dir``, when passed, bind-mounts the caller's staged
    vmlinuz + initrd + squashfs into the container at
    ``/var/lib/pixie/live-env`` (pixie's default live-env dir), which
    the inventory + flash chain modes need for the ``pixie-live-env.j2``
    template to resolve. Not used in the bootstrap / ramboot modes."""
    subprocess.run(
        ["podman", "rm", "-f", CONTAINER_NAME],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    cmd = [
        "podman",
        "run",
        "-d",
        "--name",
        CONTAINER_NAME,
        "--network=host",
        "-e",
        f"PIXIE_ADMIN_PASSWORD={admin_password}",
        # Suppress pixie's in-container in.tftpd: the test's host-
        # side dnsmasq owns udp/69 on this bridge, and the container
        # is on --network=host so binding :69 inside would collide
        # (in.tftpd already exits rc=71 on the runner because
        # rootless podman can't bind privileged ports). The bootstrap
        # chain here doesn't need pixie's TFTP surface at all.
        "-e",
        "PIXIE_TFTP_ENABLED=0",
    ]
    if live_env_dir is not None:
        # ``:z`` relabels the volume for SELinux so an enforcing
        # runner (rare on GHA but present on some dev machines) can
        # still open the files. ``:ro`` because pixie never writes
        # into live-env at runtime; the operator stages it once.
        cmd.extend(["-v", f"{live_env_dir}:/var/lib/pixie/live-env:z,ro"])
    cmd.append(image)
    subprocess.run(cmd, check=True, capture_output=True)
    return CONTAINER_NAME


def _stop_container(handle, *, name=None) -> None:
    target = handle or name
    if target is None:
        return
    subprocess.run(
        ["podman", "rm", "-f", target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _dump_container_logs() -> None:
    log.error(f"--- podman logs {CONTAINER_NAME} ---")
    res = subprocess.run(
        ["podman", "logs", "--tail", "200", CONTAINER_NAME],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in (res.stdout + res.stderr).splitlines():
        log.error(line)


# ---------- client VM ------------------------------------------------------


_OVMF_PAIRS = (
    ("/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"),
    ("/usr/share/OVMF/OVMF_CODE.fd", "/usr/share/OVMF/OVMF_VARS.fd"),
    ("/usr/share/ovmf/OVMF_CODE.fd", "/usr/share/ovmf/OVMF_VARS.fd"),
)


def _find_ovmf():
    for code, vars_tpl in _OVMF_PAIRS:
        if Path(code).is_file() and Path(vars_tpl).is_file():
            return code, vars_tpl
    return None


def _start_client_vm(workspace: Path, cfg, log_path: Path, firmware: str = "bios"):
    blank_disk = workspace / "client-blank.qcow2"
    if not blank_disk.exists():
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", str(blank_disk), "8G"],
            check=True,
            capture_output=True,
        )
    fw_args: list[str] = []
    if firmware == "uefi":
        ovmf = _find_ovmf()
        if ovmf is None:
            raise RuntimeError("client_firmware=uefi but no OVMF firmware found")
        code, vars_tpl = ovmf
        vars_copy = workspace / "client-ovmf-vars.fd"
        shutil.copy(vars_tpl, vars_copy)
        fw_args = [
            "-drive",
            f"if=pflash,format=raw,unit=0,readonly=on,file={code}",
            "-drive",
            f"if=pflash,format=raw,unit=1,file={vars_copy}",
        ]
    cmd = [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-cpu",
        "host",
        *fw_args,
        "-smp",
        "1",
        "-m",
        # Sized for the pixie live env's own footprint. In inventory
        # mode Debian live-boot's do_httpmount downloads the whole
        # squashfs into a ramfs before mounting, and our netboot-pc
        # bake weighs ~750 MiB. 1 GiB left wget OOM-killed mid-fetch
        # on the runner (kernel panic at ~8.6 s); 4 GiB carries the
        # ramfs + kernel + tmpfs headroom + running processes with
        # room to spare. Ramboot + bootstrap modes fit in less but
        # the shared driver runs one config, so pick the ceiling.
        "4G",
        "-drive",
        f"file={blank_disk},if=none,id=flashdrive,format=qcow2",
        "-device",
        "virtio-blk-pci,drive=flashdrive,serial=PIXIETEST",
        "-nographic",
        # COM1 (ttyS0) for chain markers, COM2 (ttyS1) to null so the
        # kernel-side 8250 driver's view of both ports is deterministic
        # regardless of which the PXE templates prefer.
        "-serial",
        f"file:{log_path}",
        "-serial",
        "null",
        "-boot",
        "n",
        "-netdev",
        f"tap,id=pxe,ifname={cfg['tap_iface']},script=no,downscript=no",
        "-device",
        f"virtio-net,netdev=pxe,mac={cfg['client_mac']},bootindex=1",
    ]
    # Capture QEMU's stdout+stderr so a startup failure (missing
    # /dev/kvm access, tap open EBUSY, invalid firmware path, ...)
    # leaves a diagnosable trail. DEVNULL here has burned us once
    # already: the client silently failed to spawn on the runner and
    # every marker was "missing" without any actionable log.
    qemu_log = log_path.with_suffix(".qemu.log")
    return subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=open(qemu_log, "wb"),
        stderr=subprocess.STDOUT,
    )


# ---------- markers + small utils ------------------------------------------


def _build_markers(cfg):
    """Every ``[test.pxe.chain_markers]`` entry is (key, substring)
    matched against the client serial log AND pixie's container
    logs (so a server-side hit still counts when the client-side
    console doesn't spell the fetch out). The per-MAC ``/pxe/<mac>``
    fetch marker is added automatically.

    iPXE emits colon-form MACs in its console output
    (``http://.../pxe/52:54:00:11:22:33``); uvicorn logs the URL
    percent-encoded (``52%3A54%3A00%3A11%3A22%3A33``). To match
    both without a special case, key on the colon-form MAC (which
    contains no delimiters the encoder rewrites -- ``52:54:...``
    literally in the serial log) OR its percent-encoded twin
    (``52%3A54%3A...``) via the ``/pxe/52`` prefix + first octet;
    both hits are strictly under ``/pxe/`` so no bootstrap-side
    collision."""
    out = [(entry["key"], entry["needle"]) for entry in cfg.get("chain_markers", [])]
    mac_colon = cfg["client_mac"].lower()
    first_octet = mac_colon.split(":", 1)[0]
    # ``/pxe/<mac[0:2]>`` is a stable substring that appears in both
    # iPXE's console output and uvicorn's access log; ``/pxe-bootstrap
    # .ipxe`` (the earlier marker) starts with ``/pxe-`` not ``/pxe/``
    # so there's no ambiguity.
    out.append(("ipxe-fetch-permac", f"/pxe/{first_octet}"))
    return out


def _wait_for_chain_markers(log_path: Path, markers, timeout: int):
    seen = {key: False for key, _ in markers}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not all(seen.values()):
        # Check the serial log for iPXE-side markers.
        if log_path.exists():
            body = log_path.read_text(encoding="utf-8", errors="replace")
            for key, needle in markers:
                if not seen[key] and needle in body:
                    log.info(f"  + {key}: matched {needle!r}")
                    seen[key] = True
        # Also mirror-check container logs so a server-side hit counts
        # even when the client's console doesn't spell the fetch out.
        cont = _container_log_snapshot()
        for key, needle in markers:
            if not seen[key] and needle in cont:
                log.info(f"  + {key}: matched {needle!r} in container logs")
                seen[key] = True
        if all(seen.values()):
            break
        time.sleep(2)
    return seen


def _container_log_snapshot() -> str:
    res = subprocess.run(
        ["podman", "logs", "--tail", "500", CONTAINER_NAME],
        capture_output=True,
        text=True,
        check=False,
    )
    return res.stdout + res.stderr


def _wait_until(predicate, timeout: int, what: str) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(2)
    log.error(f"timed out after {timeout:.0f}s waiting for: {what}")
    return False


def _http_ready(base: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/healthz", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _dump_tail(path: Path, lines: int) -> None:
    if not path.is_file():
        log.error(f"{path}: file does not exist")
        return
    body = path.read_text(encoding="utf-8", errors="replace")
    log.error(f"--- last {lines} lines of {path} ---")
    for line in body.splitlines()[-lines:]:
        log.error(line)


def _sudo(cmd, check: bool = True):
    return subprocess.run(["sudo", "-n", *cmd], check=check, capture_output=True, text=True)


def _terminate(proc, what: str, sudo: bool = False) -> None:
    log.info(f"Terminating {what} (pid={proc.pid})")
    if sudo:
        subprocess.run(["sudo", "-n", "kill", str(proc.pid)], check=False)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------- ramboot / flash mode: workspace HTTP server + seeding ----------


class _WorkspaceFilesHandler(http.server.SimpleHTTPRequestHandler):
    """Serves any file from the workspace's ``_build/test-pxe/``
    directory. Ramboot uses ``bundle.tar.gz`` + ``disk.img``; flash
    uses ``flash-target.img``. The staging step drops files into the
    workspace before we boot; the server just exposes them."""

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _ReusableThreadingHTTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _start_workspace_http_server(workspace: Path, bind_ip: str, *, required: tuple[str, ...]):
    """Serve ``_build/test-pxe/`` on the bridge IP so pixie's catalog
    fetch can reach the payload files (they're not on any real HTTP
    source; a stage step assembled them locally). ``required`` names
    the files whose presence we assert before starting the server;
    returns None on any missing file (already logged)."""
    # Dump the workspace contents so a missing payload is immediately
    # diagnosable without a second CI iteration.
    log.error(f"workspace http server: workspace={workspace}")
    if workspace.is_dir():
        for p in sorted(workspace.iterdir()):
            log.error(f"  {p.name} ({p.stat().st_size} bytes)")
    missing = [name for name in required if not (workspace / name).is_file()]
    if missing:
        log.error(f"workspace payload missing: {missing} under {workspace}")
        return None
    # SimpleHTTPRequestHandler reads ``directory`` from ``__init__``
    # kwargs, not from a class attribute -- bind via functools.partial
    # so ThreadingTCPServer's ``handler(*args, **kwargs)`` construction
    # supplies it. (Setting ``directory`` on the class silently falls
    # back to os.getcwd(), which is why the first run returned 404
    # for every URL: cwd was cijoe/, not cijoe/_build/test-pxe/.)
    handler = functools.partial(_WorkspaceFilesHandler, directory=str(workspace))
    server = _ReusableThreadingHTTPServer((bind_ip, WORKSPACE_HTTP_PORT), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f"Workspace HTTP server listening on http://{bind_ip}:{WORKSPACE_HTTP_PORT}")
    return server


def _seed_ramboot_and_bind(seed_base: str, admin_password: str, cfg) -> int:
    """Login, POST + fetch two catalog entries (netboot bundle + disk
    image), wait for content_sha256, then PUT /machines/<mac> with
    boot_mode=ramboot bound to the disk sha. Returns 0 on success,
    an errno on any failure (already logged)."""
    server_ip = cfg["server_pxe_ip"]
    bundle_url = f"http://{server_ip}:{WORKSPACE_HTTP_PORT}/bundle.tar.gz"
    disk_url = f"http://{server_ip}:{WORKSPACE_HTTP_PORT}/disk.img"

    try:
        cookie = _login(seed_base, admin_password)
    except Exception as exc:
        log.error(f"login failed: {exc}")
        return errno.EACCES

    if err := _add_and_fetch(seed_base, cookie, "test-bundle", bundle_url, "tar.gz"):
        return err
    if err := _add_and_fetch(
        seed_base, cookie, "test-disk", disk_url, "img", netboot_src=bundle_url
    ):
        return err

    try:
        bundle_sha = _wait_content_sha(seed_base, "test-bundle")
        disk_sha = _wait_content_sha(seed_base, "test-disk")
    except TimeoutError as exc:
        log.error(str(exc))
        return errno.ETIMEDOUT

    log.info(f"Bundle sha={bundle_sha[:12]}...; disk sha={disk_sha[:12]}...")
    try:
        _bind_machine(seed_base, cookie, cfg["client_mac"], disk_sha)
    except Exception as exc:
        log.error(f"machine bind failed: {exc}")
        return errno.EPROTO
    log.info(f"Machine {cfg['client_mac']} bound to boot_mode=ramboot")
    return 0


def _login(base: str, password: str) -> str:
    """POST /ui/login, capture the pixie-token Set-Cookie off the 303
    redirect. urllib follows redirects and drops Set-Cookie by
    default; intercept the 303 to grab it."""

    class _CaptureRedirect(urllib.request.HTTPRedirectHandler):
        def http_error_303(self, req, fp, code, msg, headers):  # type: ignore[override]
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    body = f"password={password}".encode()
    req = urllib.request.Request(
        f"{base}/ui/login",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    opener = urllib.request.build_opener(_CaptureRedirect())
    try:
        opener.open(req, timeout=10)
    except urllib.error.HTTPError as exc:
        for raw in exc.headers.get_all("Set-Cookie") or []:
            head = raw.split(";", 1)[0].strip()
            if head.startswith("pixie-token="):
                return head
    raise RuntimeError("no pixie-token cookie in login response")


def _add_and_fetch(
    base: str, cookie: str, name: str, src: str, fmt: str, *, netboot_src: str = ""
) -> int:
    body: dict[str, object] = {"name": name, "src": src, "format": fmt}
    if netboot_src:
        body["netboot_src"] = netboot_src
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/catalog/entries",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Cookie": cookie},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 201:
                log.error(f"POST /catalog/entries returned {resp.status}")
                return errno.EPROTO
    except urllib.error.HTTPError as exc:
        log.error(f"POST /catalog/entries {name}: HTTP {exc.code}")
        return errno.EPROTO
    fetch_req = urllib.request.Request(
        f"{base}/catalog/entries/{name}/fetch",
        data=b"",
        method="POST",
        headers={"Content-Type": "application/json", "Cookie": cookie},
    )
    try:
        with urllib.request.urlopen(fetch_req, timeout=15) as resp:
            if resp.status != 202:
                log.error(f"POST /catalog/entries/{name}/fetch returned {resp.status}")
                return errno.EPROTO
    except urllib.error.HTTPError as exc:
        log.error(f"POST /catalog/entries/{name}/fetch: HTTP {exc.code}")
        return errno.EPROTO
    log.info(f"Fetch triggered for {name}")
    return 0


def _wait_content_sha(base: str, name: str, timeout: float = FETCH_TIMEOUT) -> str:
    """Poll ``GET /catalog`` until ``name`` has a populated
    ``content_sha256``. Raises ``TimeoutError`` on fetch failure or
    deadline miss."""
    deadline = time.monotonic() + timeout
    last_state = "?"
    while time.monotonic() < deadline:
        req = urllib.request.Request(f"{base}/catalog")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                entries = json.loads(resp.read()).get("entries", [])
        except (urllib.error.URLError, OSError):
            time.sleep(1.0)
            continue
        for entry in entries:
            if entry.get("name") != name:
                continue
            state = entry.get("fetch_state") or "?"
            last_state = state
            if state == "error":
                err = entry.get("fetch_error") or "unknown"
                raise TimeoutError(f"fetch failed for {name!r}: {err}")
            sha = entry.get("content_sha256")
            if sha:
                return str(sha)
        time.sleep(1.0)
    raise TimeoutError(
        f"fetch never populated content_sha256 for {name!r} within {timeout}s "
        f"(last state: {last_state})"
    )


_INVENTORY_TIMEOUT_S = 120


def _verify_server_inventory(base: str, mac: str) -> int:
    """After the client boots into the pixie live env and
    ``pixie-on-tty1.service`` runs the real pixie CLI, its
    ``_auto_post_inventory`` background thread POSTs an inventory
    blob to ``/pxe/<mac>/inventory``. That happens some seconds
    after ``systemd`` finishes early boot (Rich imports + cmdline
    parse + wizard startup can take 1-5s on a warm VM), so poll
    for a bounded window rather than one-shot the GET.

    The blob's shape comes from ``pixie.disks.list_disks()`` +
    ``pixie.tui._app.collect_lshw()``. The only field the test
    can be sure exists in a QEMU-virt boot is ``disks`` with at
    least one entry (``/dev/nbd0``); assert on that + that lshw
    is present-or-null (empty on runners without lshw)."""
    url = f"{base}/machines/{mac}/inventory"
    log.info(f"Polling server-side inventory: GET {url}")
    deadline = time.monotonic() + _INVENTORY_TIMEOUT_S
    last_err: str | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code}: {exc.reason}"
            if exc.code != 404:
                log.error(f"GET {url} -> {last_err}")
                return errno.EPROTO
            time.sleep(3.0)
            continue
        except (urllib.error.URLError, OSError) as exc:
            last_err = f"transport: {exc}"
            time.sleep(3.0)
            continue

        inv = body.get("inventory") or {}
        disks = inv.get("disks") or []
        if not disks:
            last_err = "inventory has empty disks list"
            time.sleep(3.0)
            continue
        log.info(
            f"Server-side inventory ok: mac={body.get('mac')} "
            f"disks_count={len(disks)} has_lshw={inv.get('lshw') is not None}"
        )
        return 0

    log.error(f"inventory did not arrive within {_INVENTORY_TIMEOUT_S}s (last: {last_err})")
    return errno.ETIMEDOUT


def _bind_machine(base: str, cookie: str, mac: str, image_sha: str) -> None:
    body = {"boot_mode": "ramboot", "image_content_sha256": image_sha}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/machines/{mac}",
        data=data,
        method="PUT",
        headers={"Content-Type": "application/json", "Cookie": cookie},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status != 200:
            raise RuntimeError(f"PUT /machines/{mac} returned {resp.status}")


def _bind_inventory(seed_base: str, admin_password: str, cfg) -> int:
    """Login + PUT /machines/<mac> with boot_mode=pixie-inventory.
    Distinct from ``_bind_machine`` because inventory needs no catalog
    seed and no image_content_sha256 -- pixie's PXE renderer resolves
    the live-env chain from the operator-staged live-env dir alone.
    Returns 0 on success, an errno on any failure (already logged)."""
    try:
        cookie = _login(seed_base, admin_password)
    except Exception as exc:
        log.error(f"login failed: {exc}")
        return errno.EACCES

    body = {"boot_mode": "pixie-inventory"}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{seed_base}/machines/{cfg['client_mac']}",
        data=data,
        method="PUT",
        headers={"Content-Type": "application/json", "Cookie": cookie},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                log.error(f"PUT /machines/{cfg['client_mac']} returned {resp.status}")
                return errno.EPROTO
    except urllib.error.HTTPError as exc:
        log.error(f"PUT /machines/{cfg['client_mac']}: HTTP {exc.code}")
        return errno.EPROTO
    log.info(f"Machine {cfg['client_mac']} bound to boot_mode=pixie-inventory")
    return 0


# ---------- flash mode: seed synthetic image + bind pixie-flash-once -------


# The synthetic image ``pxe_flash_stage`` writes into the workspace
# starts with this marker so the post-chain assertion can grep the
# QEMU-side disk (byte-for-byte compare on the first ~64 bytes) to
# prove the flash actually wrote the disk. Kept in sync with the
# constant in ``pxe_flash_stage.py``; they are the two ends of the
# same contract.
_FLASH_MARKER = b"PIXIE-FLASH-TARGET-MARKER-v1\n"


def _post_flash_inventory(seed_base: str, mac: str, disk_serial: str) -> int:
    """The pixie-flash bind refuses unless the machine already has an
    inventory entry naming ``disk_serial``. Live-env clients POST that
    themselves on first boot; here we seed it directly via the open
    ``POST /pxe/<mac>/inventory`` endpoint so the bind PUT that
    follows passes the ``target_disk_serial in inventory`` check. This
    row will be overwritten by the real live-env POST later, so the
    seeded disks list is purely a pre-bind formality."""
    body = json.dumps(
        {"disks": [{"path": "/dev/sda", "serial": disk_serial}], "lshw": None}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{seed_base}/pxe/{mac}/inventory",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 204:
                log.error(f"POST /pxe/{mac}/inventory returned {resp.status}")
                return errno.EPROTO
    except urllib.error.HTTPError as exc:
        log.error(f"POST /pxe/{mac}/inventory: HTTP {exc.code}")
        return errno.EPROTO
    return 0


_FLASH_BOOT_MODES = ("pixie-flash-once", "pixie-flash-always")


def _seed_flash_and_bind(seed_base: str, admin_password: str, cfg) -> int:
    """Login, POST + fetch the synthetic flash image, wait for its
    content_sha256, seed a matching inventory disk on the machine,
    then PUT /machines/<mac> with the configured flash boot_mode +
    image_content_sha256 + target_disk_serial. Returns 0 on success,
    an errno on any failure (already logged). ``flash_boot_mode`` in
    the config defaults to ``pixie-flash-once``; the always variant
    exercises the same wire but the post-chain assertion in
    ``_verify_flash_effects`` inverts (mode must NOT flip)."""
    server_ip = cfg["server_pxe_ip"]
    image_url = f"http://{server_ip}:{WORKSPACE_HTTP_PORT}/flash-target.img"
    mac = cfg["client_mac"]
    disk_serial = str(cfg.get("target_disk_serial", FLASH_TARGET_SERIAL))
    boot_mode = str(cfg.get("flash_boot_mode", "pixie-flash-once"))
    if boot_mode not in _FLASH_BOOT_MODES:
        log.error(
            f"unknown [test.pxe] flash_boot_mode={boot_mode!r}; expected one of {_FLASH_BOOT_MODES}"
        )
        return errno.EINVAL

    try:
        cookie = _login(seed_base, admin_password)
    except Exception as exc:
        log.error(f"login failed: {exc}")
        return errno.EACCES

    if err := _add_and_fetch(seed_base, cookie, "flash-target", image_url, "img"):
        return err

    try:
        image_sha = _wait_content_sha(seed_base, "flash-target")
    except TimeoutError as exc:
        log.error(str(exc))
        return errno.ETIMEDOUT

    log.info(f"Flash-target sha={image_sha[:12]}...")

    if err := _post_flash_inventory(seed_base, mac, disk_serial):
        return err

    body = {
        "boot_mode": boot_mode,
        "image_content_sha256": image_sha,
        "target_disk_serial": disk_serial,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{seed_base}/machines/{mac}",
        data=data,
        method="PUT",
        headers={"Content-Type": "application/json", "Cookie": cookie},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                log.error(f"PUT /machines/{mac} returned {resp.status}")
                return errno.EPROTO
    except urllib.error.HTTPError as exc:
        # 422 here means the bind guard rejected -- the most likely
        # cause is that the inventory seed step above did not stick
        # (row create race). Print the body so the operator can see
        # which validator fired.
        log.error(
            f"PUT /machines/{mac}: HTTP {exc.code}: "
            f"{exc.read().decode('utf-8', errors='replace')!r}"
        )
        return errno.EPROTO
    log.info(
        f"Machine {mac} bound to boot_mode={boot_mode} "
        f"(image_sha={image_sha[:12]}..., target_disk_serial={disk_serial})"
    )
    return 0


_FLASH_EFFECT_TIMEOUT_S = 180


def _verify_flash_effects(seed_base: str, cfg, workspace: Path) -> int:
    """After the live env's pixie CLI runs ``_run_auto``, it dds the
    image onto the target disk (matched by serial), then POSTs
    ``/pxe/<mac>/status`` with status=done. The status handler flips
    pixie-flash-once to ipxe-exit; pixie-flash-always stays put.
    Assertions:

    1. For ``pixie-flash-once``: poll ``GET /machines/<mac>`` until
       ``boot_mode`` reads ``ipxe-exit`` -- proves the /done POST
       landed AND the server flipped.
       For ``pixie-flash-always``: sleep long enough that a flip
       WOULD have shown up, then GET once and assert boot_mode
       still reads ``pixie-flash-always``. Same /done POST fires
       from the CLI; this side of the wire is what differs.
    2. Read the QEMU-side qcow2 blank disk raw and grep for the
       flash marker -- proves the CLI actually wrote the image (not
       just POSTed done and reboot-panicked). qemu-img convert to a
       throwaway raw file avoids parsing qcow2 by hand.

    Returns 0 on success, an errno on any failure (already logged)."""
    mac = cfg["client_mac"]
    boot_mode = str(cfg.get("flash_boot_mode", "pixie-flash-once"))
    url = f"{seed_base}/machines/{mac}"
    if boot_mode == "pixie-flash-once":
        log.info(f"Polling for pixie-flash-once -> ipxe-exit flip: GET {url}")
        deadline = time.monotonic() + _FLASH_EFFECT_TIMEOUT_S
        last_mode: str | None = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    body = json.loads(resp.read())
            except (urllib.error.URLError, OSError) as exc:
                last_mode = f"transport: {exc}"
                time.sleep(3.0)
                continue
            last_mode = body.get("boot_mode")
            if last_mode == "ipxe-exit":
                log.info("Mode flipped to ipxe-exit (post-flash /done landed)")
                break
            time.sleep(3.0)
        else:
            log.error(
                f"boot_mode did not flip to ipxe-exit within {_FLASH_EFFECT_TIMEOUT_S}s "
                f"(last: {last_mode!r}); live-env flash pipeline never POSTed /done"
            )
            return errno.ETIMEDOUT
    else:
        # pixie-flash-always: sleep past the point a once-mode flip
        # would have committed (single GET_TIMEOUT worth is enough --
        # the CLI POSTs /done well before that), then assert the mode
        # is unchanged. Using the same 15s window twice would be
        # cheaper but leaves a race where a slow /done arrives after
        # the check; 30s covers real jitter without inflating wall
        # clock.
        settle_s = 30
        log.info(
            f"pixie-flash-always: sleeping {settle_s}s past /done and "
            f"asserting mode stays put: GET {url}"
        )
        time.sleep(settle_s)
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                body = json.loads(resp.read())
        except (urllib.error.URLError, OSError) as exc:
            log.error(f"GET {url} failed: {exc}")
            return errno.EPROTO
        observed = body.get("boot_mode")
        if observed != "pixie-flash-always":
            log.error(
                f"pixie-flash-always must not flip on /done; observed "
                f"boot_mode={observed!r} after {settle_s}s. Server-side flip "
                "logic regressed."
            )
            return errno.EPROTO
        log.info("pixie-flash-always survived /done (mode unchanged as required)")

    # ``client-blank.qcow2`` is the qcow2 the client wrote through;
    # convert to raw so we can seek(0) + read the first bytes without
    # depending on a qcow2 parser. qemu-img is on the runner already
    # (we used it to create the disk). ``-U`` bypasses the shared-
    # write lock QEMU still holds on the qcow2 -- the client VM is
    # tore down in the outer ``finally``, not here, so at this point
    # the file is still in use. Read-only convert is safe with -U;
    # the qcow2 metadata is stable enough to read the leading raw
    # sectors even while the guest keeps running.
    blank_qcow = workspace / "client-blank.qcow2"
    raw_dump = workspace / "client-blank.raw"
    log.info(f"Converting {blank_qcow} to raw for marker check")
    conv = subprocess.run(
        ["qemu-img", "convert", "-U", "-O", "raw", str(blank_qcow), str(raw_dump)],
        capture_output=True,
        text=True,
        check=False,
    )
    if conv.returncode != 0:
        log.error(f"qemu-img convert failed: {conv.stderr.strip()}")
        return errno.EIO
    try:
        with open(raw_dump, "rb") as fh:
            head = fh.read(len(_FLASH_MARKER))
    except OSError as exc:
        log.error(f"reading raw dump failed: {exc}")
        return errno.EIO
    finally:
        raw_dump.unlink(missing_ok=True)
    if head != _FLASH_MARKER:
        log.error(
            f"target disk missing flash marker; head[:{len(_FLASH_MARKER)}]={head!r} "
            f"expected={_FLASH_MARKER!r}"
        )
        return errno.EPROTO
    log.info("Target disk carries the flash marker -- flash pipeline wrote bytes")
    return 0
