"""pixie-lab CLI entry.

Subcommands mirror bty-lab's shape to keep operator muscle memory:

    pixie-lab init [dest]     write compose.yml + envvars + README
    pixie-lab deploy [dest]   init + auto-fill envvars + podman compose up + wait /healthz
    pixie-lab purge [dest]    stop the stack; --data/--images/--all remove
                              state (confirms first; --yes to skip)

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


def _compose_down(dest: Path, envvars: Path) -> None:
    cmd = _compose_cmd()
    if cmd is None:
        raise RuntimeError(
            "no compose runner on PATH (need one of: podman-compose, podman, docker)"
        )
    subprocess.run(
        [*cmd, "--env-file", str(envvars), "down", "--remove-orphans"],
        cwd=str(dest),
        check=False,
        capture_output=True,
    )


def _confirm(prompt: str, *, assume_yes: bool) -> bool:
    """y/N gate for destructive operations. ``assume_yes`` skips the
    prompt; on a non-TTY stdin without ``assume_yes`` it refuses rather
    than guess, so a purge can't fire unattended just because it was
    piped. Mirrors bty-lab's purge confirmation."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(
            "  refusing to proceed unattended: stdin is not a TTY. Re-run with --yes to confirm.",
            file=sys.stderr,
        )
        return False
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _compose_image(dest: Path) -> str | None:
    """The ``image:`` reference in the deploy's compose.yml, or None when
    there is no compose file / no image line to read."""
    compose = dest / "compose.yml"
    if not compose.is_file():
        return None
    for line in compose.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("image:"):
            return stripped.split(":", 1)[1].strip()
    return None


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
    """Tear a deploy back down -- the inverse of ``deploy``. Stops and
    removes the container; the destructive parts (host state, image,
    deploy directory) are opt-in flags gated behind a y/N confirmation
    (skip with ``--yes``). Modelled on bty-lab's purge."""
    dest = Path(args.dest).resolve()
    envvars = dest / "envvars"
    data_dir = dest / "data"
    remove_data = bool(args.data or args.all)
    remove_dir = bool(args.all)

    # Print the plan, then gate everything behind a confirmation. Even
    # the plain stop is impactful -- it drops every nbdkit / qemu-nbd
    # export, so any target booted nbdboot off pixie loses its root.
    print(f"About to purge the pixie deploy at {dest}:", file=sys.stderr)
    print("  - podman compose down (stop + remove the container)", file=sys.stderr)
    if args.images:
        print("  - remove the pixie container image", file=sys.stderr)
    if remove_data:
        print(
            f"  - DELETE host state: {data_dir}\n"
            "      (state.db, blobs, artifacts, overlays, live-env) -- irreversible",
            file=sys.stderr,
        )
    if remove_dir:
        print(f"  - DELETE the deploy directory: {dest}", file=sys.stderr)
    if not (remove_data or remove_dir or args.images):
        print(
            "  (nothing else -- pass --data / --images / --all to remove state)",
            file=sys.stderr,
        )
    if not _confirm("Proceed?", assume_yes=args.yes):
        print("pixie-lab purge: aborted; nothing was changed.", file=sys.stderr)
        return 1

    # 1. Stop + remove the container so nothing holds the data dir open.
    if envvars.exists():
        try:
            _compose_down(dest, envvars)
        except RuntimeError as exc:
            print(f"pixie-lab purge: {exc}", file=sys.stderr)
            return 1
    else:
        print("pixie-lab purge: no envvars; skipping compose down", file=sys.stderr)

    # 2. Remove the container image.
    if args.images:
        image = _compose_image(dest) or f"{DEFAULT_IMAGE_REPO}:{pixie.__version__}"
        subprocess.run(["podman", "rmi", image], check=False, capture_output=True)
        print(f"pixie-lab purge: removed image {image}", file=sys.stderr)

    # 3. Delete host state. ``data/`` is a bind mount, not a named
    #    volume, so podman never removes it -- we unlink it ourselves.
    if remove_data and data_dir.exists():
        shutil.rmtree(data_dir)
        print(f"pixie-lab purge: removed {data_dir}", file=sys.stderr)

    # 4. Delete the deploy directory itself.
    if remove_dir and dest.exists():
        shutil.rmtree(dest)
        print(f"pixie-lab purge: removed {dest}", file=sys.stderr)

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

    p_purge = sub.add_parser(
        "purge", help="stop the stack; --data/--images/--all remove state (confirms first)"
    )
    p_purge.add_argument(
        "dest",
        nargs="?",
        default=str(DEFAULT_DEST),
        help="deploy directory (default: cwd)",
    )
    p_purge.add_argument(
        "--data",
        action="store_true",
        help="also delete host state: data/ (state.db, blobs, artifacts, "
        "overlays, live-env). DESTRUCTIVE.",
    )
    p_purge.add_argument(
        "--images",
        action="store_true",
        help="also remove the pixie container image.",
    )
    p_purge.add_argument(
        "--all",
        action="store_true",
        help="also delete the deploy directory (compose.yml, envvars). "
        "Implies --data. DESTRUCTIVE.",
    )
    p_purge.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip the confirmation prompt (for scripted / unattended runs).",
    )
    p_purge.set_defaults(func=_cmd_purge)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover -- exercised via console-script
    sys.exit(main())
