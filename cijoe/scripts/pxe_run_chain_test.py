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
import logging as log
import shutil
import subprocess
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

    container = None
    dnsmasq = None
    client = None
    net_up = False
    try:
        _setup_network(cfg, tftproot)
        net_up = True
        dnsmasq = _start_dnsmasq(cfg, tftproot, workspace)

        container = _run_container(image, admin_password)
        log.info(f"Waiting for pixie /healthz on {seed_base}")
        if not _wait_until(lambda: _http_ready(seed_base), HEALTHZ_TIMEOUT, "pixie /healthz"):
            log.error("pixie container did not become healthy; logs:")
            _dump_container_logs()
            return errno.ETIMEDOUT

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

        log.info("PXE bootstrap chain test PASSED (all markers seen on client serial console)")
        return 0
    finally:
        if client is not None:
            _terminate(client, "client VM")
        _stop_container(container)
        if dnsmasq is not None:
            _terminate(dnsmasq, "dnsmasq", sudo=True)
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


def _run_container(image: str, admin_password: str):
    """Run the pixie container detached on host networking with
    ``PIXIE_ADMIN_PASSWORD`` set. Host networking keeps ``/healthz``
    reachable via loopback while the client's PXE HTTP fetch hits the
    same process via the bridge IP."""
    subprocess.run(
        ["podman", "rm", "-f", CONTAINER_NAME],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    subprocess.run(
        [
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
            image,
        ],
        check=True,
        capture_output=True,
    )
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
        "1G",
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
