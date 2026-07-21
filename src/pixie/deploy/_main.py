"""pixie-lab CLI entry.

Subcommands mirror bty-lab's shape to keep operator muscle memory:

    pixie-lab init [dest]     write compose.yml + envvars + README
    pixie-lab deploy [dest]   init + auto-fill envvars + podman compose up + wait /healthz
    pixie-lab purge [dest]    podman compose down -v; optionally --wipe-data

Every subcommand takes an optional ``dest`` argument (defaults to the
current directory). The ``deploy`` and ``purge`` subcommands shell out
to ``podman compose`` (or ``docker compose``, or ``podman-compose``,
whichever is on PATH) so pixie-lab does not carry its own container
runtime.
"""

from __future__ import annotations

import argparse
import http.client
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pixie
from pixie.deploy._templates import (
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_IMAGE_REPO,
    compose_yaml,
    envvars_example,
    readme_md,
)

DEFAULT_DEST = Path.cwd()
_HEALTHZ_TIMEOUT = 60.0


# ---------- filesystem writes ------------------------------------------


def _write(path: Path, body: str, *, force: bool) -> Path:
    """Write ``body`` to ``path``, refusing to overwrite unless
    ``force`` is set. Creates parent dirs on the way. Returns the
    absolute path so the caller can print a friendly summary."""
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; pass --force to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _emit_files(dest: Path, *, image: str, admin_password: str, force: bool) -> list[Path]:
    """Lay down the three canonical files + create the ``data/``
    scaffold. Returns the list of written paths."""
    compose = _write(
        dest / "compose.yml", compose_yaml(image=image, admin_password=admin_password), force=force
    )
    envvars = _write(
        dest / "envvars.example", envvars_example(admin_password=admin_password), force=force
    )
    readme = _write(dest / "README.md", readme_md(), force=force)
    (dest / "data").mkdir(parents=True, exist_ok=True)
    return [compose, envvars, readme]


# ---------- host address + password detection --------------------------


def detect_host_addr() -> str:
    """LAN-facing IP by UDP-connect probe (no packet is sent; the
    kernel just chooses an outbound interface). Falls back to
    127.0.0.1 when no route is available. Same trick bty uses."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2)
    try:
        s.connect(("198.51.100.1", 80))
        addr: str = s.getsockname()[0]
        return addr
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def gen_admin_password(nbytes: int = 24) -> str:
    """URL-safe random token to hand the operator on first deploy."""
    return secrets.token_urlsafe(nbytes)


# ---------- compose runner detection -----------------------------------


def _compose_cmd() -> list[str] | None:
    """Return the compose invocation to shell out to, or ``None`` when
    no supported runner is on PATH."""
    if shutil.which("podman-compose"):
        return ["podman-compose"]
    if shutil.which("podman") and shutil.which("docker-compose") is None:
        # ``podman compose`` shells out to a provider; keep it simple.
        return ["podman", "compose"]
    if shutil.which("docker"):
        return ["docker", "compose"]
    return None


def _compose_up(dest: Path, envvars: Path) -> None:
    cmd = _compose_cmd()
    if cmd is None:
        raise RuntimeError(
            "no compose runner on PATH (need one of: podman-compose, podman, docker)"
        )
    try:
        subprocess.run(
            [*cmd, "--env-file", str(envvars), "up", "-d"],
            cwd=str(dest),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"compose up failed (rc={exc.returncode}):\n"
            f"--- stdout ---\n{exc.stdout}\n--- stderr ---\n{exc.stderr}"
        ) from exc


def _compose_down(dest: Path, envvars: Path, *, wipe_data: bool) -> None:
    cmd = _compose_cmd()
    if cmd is None:
        raise RuntimeError(
            "no compose runner on PATH (need one of: podman-compose, podman, docker)"
        )
    args = [*cmd, "--env-file", str(envvars), "down"]
    if wipe_data:
        args.append("-v")
    subprocess.run(args, cwd=str(dest), check=False, capture_output=True)


def _wait_healthz(host: str, port: int, deadline: float) -> None:
    url = f"http://{host}:{port}/healthz"
    last_err = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if resp.status == 200:
                    return
                last_err = f"HTTP {resp.status}"
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            last_err = str(exc)
        time.sleep(1.0)
    raise RuntimeError(f"healthz timeout on {url}: {last_err}")


# ---------- subcommands ------------------------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    dest = Path(args.dest).resolve()
    image = args.image or f"{DEFAULT_IMAGE_REPO}:{pixie.__version__}"
    admin_pw = args.admin_password or DEFAULT_ADMIN_PASSWORD
    try:
        written = _emit_files(dest, image=image, admin_password=admin_pw, force=args.force)
    except FileExistsError as exc:
        print(f"pixie-lab init: {exc}", file=sys.stderr)
        return 1
    for p in written:
        print(f"  {p.relative_to(dest.parent) if dest.parent != Path() else p}", file=sys.stderr)
    print(
        f"\nNext:\n"
        f"  cd {dest}\n"
        f'  cp envvars.example envvars && "${{EDITOR:-vi}}" envvars\n'
        f"  COMPOSE_ENV_FILES=envvars podman compose up -d\n",
        file=sys.stderr,
    )
    return 0


def _cmd_deploy(args: argparse.Namespace) -> int:
    dest = Path(args.dest).resolve()
    image = args.image or f"{DEFAULT_IMAGE_REPO}:{pixie.__version__}"
    admin_pw = args.admin_password or gen_admin_password()
    host_addr = args.host_addr or detect_host_addr()

    try:
        _emit_files(dest, image=image, admin_password=admin_pw, force=args.force)
    except FileExistsError as exc:
        print(f"pixie-lab deploy: {exc}", file=sys.stderr)
        return 1

    # Realise envvars from envvars.example + operator-detected
    # host_addr + admin password.
    envvars = dest / "envvars"
    envvars.write_text(
        f"PIXIE_HOST_ADDR={host_addr}\nPIXIE_ADMIN_PASSWORD={admin_pw}\n",
        encoding="utf-8",
    )

    print(
        f"pixie-lab deploy:\n"
        f"  dest        : {dest}\n"
        f"  image       : {image}\n"
        f"  host_addr   : {host_addr}\n"
        f"  admin_pass  : {admin_pw}\n",
        file=sys.stderr,
    )

    try:
        _compose_up(dest, envvars)
        _wait_healthz("127.0.0.1", 8080, time.monotonic() + _HEALTHZ_TIMEOUT)
    except RuntimeError as exc:
        print(f"pixie-lab deploy: {exc}", file=sys.stderr)
        return 1
    print(f"pixie is up at http://{host_addr}:8080/ui/", file=sys.stderr)
    return 0


def _cmd_purge(args: argparse.Namespace) -> int:
    dest = Path(args.dest).resolve()
    envvars = dest / "envvars"
    if not envvars.exists():
        # No envvars means no compose to pull down. Still let the
        # operator wipe data if they asked for it.
        if args.wipe_data:
            data_dir = dest / "data"
            if data_dir.exists():
                shutil.rmtree(data_dir)
                print(f"pixie-lab purge: removed {data_dir}", file=sys.stderr)
        return 0
    try:
        _compose_down(dest, envvars, wipe_data=args.wipe_data)
    except RuntimeError as exc:
        print(f"pixie-lab purge: {exc}", file=sys.stderr)
        return 1
    return 0


# ---------- CLI wiring -------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pixie-lab",
        description=(
            "pixie deploy generator + convenience CLI. See PLAN.md at "
            "github.com/safl/pixie for the design rationale."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def _add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "dest",
            nargs="?",
            default=str(DEFAULT_DEST),
            help="deploy directory (default: cwd)",
        )
        sp.add_argument(
            "--image",
            default=None,
            help=f"container image (default: {DEFAULT_IMAGE_REPO}:<pixie-version>)",
        )
        sp.add_argument(
            "--admin-password",
            default=None,
            help="operator UI + write-route password (init: default pixie; deploy: random)",
        )
        sp.add_argument(
            "--force",
            action="store_true",
            help="overwrite existing files under dest",
        )

    p_init = sub.add_parser("init", help="write compose.yml + envvars.example + README + data/")
    _add_common(p_init)
    p_init.set_defaults(func=_cmd_init)

    p_deploy = sub.add_parser(
        "deploy",
        help="init + fill envvars + podman compose up -d + wait /healthz",
    )
    _add_common(p_deploy)
    p_deploy.add_argument(
        "--host-addr",
        default=None,
        help="LAN address pixie advertises (default: auto-detected)",
    )
    p_deploy.set_defaults(func=_cmd_deploy)

    p_purge = sub.add_parser("purge", help="podman compose down [--wipe-data drops the volume]")
    p_purge.add_argument(
        "dest",
        nargs="?",
        default=str(DEFAULT_DEST),
        help="deploy directory (default: cwd)",
    )
    p_purge.add_argument(
        "--wipe-data",
        action="store_true",
        help="also delete the ``data/`` subdirectory",
    )
    p_purge.set_defaults(func=_cmd_purge)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover -- exercised via console-script
    sys.exit(main())
