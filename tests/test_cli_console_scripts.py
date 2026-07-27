"""End-to-end console-script tests: build the wheel, install it into a
throwaway venv, and drive the ``pixie-lab`` / ``pixie`` entry points with
all flags.

This is the layer the unit tests can't cover. ``test_deploy_unit.py``
imports functions from the *source* tree, so it passes even when the
built *wheel* is broken -- which is exactly how 0.4.7-0.4.9 shipped a
wheel with no ``pixie.deploy`` module (a too-broad sdist exclude) and
``pixie-lab`` died with ``ModuleNotFoundError``. These tests exercise the
SHIPPED artifact: a packaging break fails here, mirroring
``uv tool run pixie-lab``.

Slow: builds a wheel + venv once per session. Skipped when ``uv`` isn't
on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("uv") is None, reason="uv is required to build + install the wheel"
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


CliRunner = Callable[..., subprocess.CompletedProcess[str]]


@pytest.fixture(scope="session")
def cli(tmp_path_factory: pytest.TempPathFactory) -> CliRunner:
    """Build the wheel, install it into a fresh venv, and return a runner
    that invokes one of the venv's console scripts by name."""
    work = tmp_path_factory.mktemp("cli-e2e")
    dist = work / "dist"
    subprocess.run(
        ["uv", "build", "--out-dir", str(dist)],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(dist.glob("*.whl"))
    assert wheels, "uv build produced no wheel"

    venv = work / "venv"
    subprocess.run(["uv", "venv", str(venv)], check=True, capture_output=True, text=True)
    bindir = venv / ("Scripts" if sys.platform == "win32" else "bin")
    subprocess.run(
        ["uv", "pip", "install", "--python", str(bindir / "python"), str(wheels[0])],
        check=True,
        capture_output=True,
        text=True,
    )

    def run(script: str, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(bindir / script), *args],
            capture_output=True,
            text=True,
            timeout=60,
            **kwargs,  # type: ignore[arg-type]
        )

    return run


# ---- entry-point / --help: the packaging guard ------------------------


def test_pixie_lab_help(cli: CliRunner) -> None:
    r = cli("pixie-lab", "--help")
    assert r.returncode == 0, r.stderr
    assert "{init,deploy,purge}" in r.stdout


def test_pixie_help(cli: CliRunner) -> None:
    r = cli("pixie", "--help")
    assert r.returncode == 0, r.stderr
    assert "usage: pixie" in r.stdout


@pytest.mark.parametrize("sub", ["init", "deploy", "purge"])
def test_subcommand_help(cli: CliRunner, sub: str) -> None:
    r = cli("pixie-lab", sub, "--help")
    assert r.returncode == 0, r.stderr
    assert "usage: pixie-lab" in r.stdout


def test_deploy_help_lists_all_flags(cli: CliRunner) -> None:
    r = cli("pixie-lab", "deploy", "--help")
    assert r.returncode == 0
    for flag in ("--image", "--admin-password", "--host-addr", "--force"):
        assert flag in r.stdout


def test_purge_help_lists_all_flags(cli: CliRunner) -> None:
    r = cli("pixie-lab", "purge", "--help")
    assert r.returncode == 0
    for flag in ("--data", "--images", "--all", "--yes"):
        assert flag in r.stdout


# ---- init: real run, all flags (safe -- only writes files) ------------


def test_init_writes_files(cli: CliRunner, tmp_path: Path) -> None:
    dest = tmp_path / "d"
    r = cli("pixie-lab", "init", str(dest))
    assert r.returncode == 0, r.stderr
    assert (dest / "compose.yml").is_file()
    assert (dest / "envvars.example").is_file()
    assert (dest / "README.md").is_file()
    assert (dest / "data").is_dir()


def test_init_all_flags(cli: CliRunner, tmp_path: Path) -> None:
    dest = tmp_path / "d"
    r = cli(
        "pixie-lab",
        "init",
        str(dest),
        "--image",
        "ghcr.io/x/pixie:e2e",
        "--admin-password",
        "hunter2",
    )
    assert r.returncode == 0, r.stderr
    assert "ghcr.io/x/pixie:e2e" in (dest / "compose.yml").read_text()
    assert "hunter2" in (dest / "envvars.example").read_text()


def test_init_refuses_clobber_then_force(cli: CliRunner, tmp_path: Path) -> None:
    dest = tmp_path / "d"
    assert cli("pixie-lab", "init", str(dest)).returncode == 0
    assert cli("pixie-lab", "init", str(dest)).returncode == 1  # no --force
    assert cli("pixie-lab", "init", str(dest), "--force").returncode == 0


# ---- purge: real runs on a dir with no compose runner needed ----------
# (no ``envvars`` -> compose down is skipped, so no podman/docker needed)


def test_purge_all_yes_removes_dir(cli: CliRunner, tmp_path: Path) -> None:
    dest = tmp_path / "d"
    cli("pixie-lab", "init", str(dest))
    (dest / "data" / "state.db").write_text("x")
    r = cli("pixie-lab", "purge", str(dest), "--all", "--yes")
    assert r.returncode == 0, r.stderr
    assert not dest.exists()  # --all removed the deploy dir


def test_purge_data_yes_removes_state_keeps_dir(cli: CliRunner, tmp_path: Path) -> None:
    dest = tmp_path / "d"
    cli("pixie-lab", "init", str(dest))
    (dest / "data" / "state.db").write_text("x")
    r = cli("pixie-lab", "purge", str(dest), "--data", "--yes")
    assert r.returncode == 0, r.stderr
    assert dest.exists()
    assert not (dest / "data").exists()  # state gone, deploy dir kept


def test_purge_plain_yes_keeps_state(cli: CliRunner, tmp_path: Path) -> None:
    dest = tmp_path / "d"
    cli("pixie-lab", "init", str(dest))
    (dest / "data").mkdir(exist_ok=True)
    r = cli("pixie-lab", "purge", str(dest), "--yes")
    assert r.returncode == 0, r.stderr
    assert (dest / "data").exists()  # plain stop leaves state in place


def test_purge_without_yes_refuses_unattended(cli: CliRunner, tmp_path: Path) -> None:
    dest = tmp_path / "d"
    cli("pixie-lab", "init", str(dest))
    (dest / "data").mkdir(exist_ok=True)
    # Non-TTY stdin (pipe) + no --yes -> refuse rather than fire.
    r = cli("pixie-lab", "purge", str(dest), "--data", stdin=subprocess.DEVNULL)
    assert r.returncode == 1
    assert (dest / "data").exists()  # nothing changed


def test_no_subcommand_is_an_error(cli: CliRunner) -> None:
    r = cli("pixie-lab")
    assert r.returncode != 0  # argparse: subcommand is required
