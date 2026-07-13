"""Unit tests for the ``pixie-lab`` CLI's file emitter + argparse
shape. Anything that shells out to podman lives in the integration
suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pixie.deploy._main import (
    _build_parser,
    _emit_files,
    detect_host_addr,
    gen_admin_password,
)
from pixie.deploy._templates import (
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_IMAGE_REPO,
    compose_yaml,
    envvars_example,
)


def test_emit_files_writes_the_three_canonical_files(tmp_path: Path) -> None:
    _emit_files(
        tmp_path,
        image=f"{DEFAULT_IMAGE_REPO}:0.4.0",
        admin_password=DEFAULT_ADMIN_PASSWORD,
        force=False,
    )
    assert (tmp_path / "compose.yml").is_file()
    assert (tmp_path / "envvars.example").is_file()
    assert (tmp_path / "README.md").is_file()
    assert (tmp_path / "data").is_dir()


def test_emit_files_refuses_to_clobber_without_force(tmp_path: Path) -> None:
    _emit_files(
        tmp_path,
        image="anything:test",
        admin_password="x",
        force=False,
    )
    with pytest.raises(FileExistsError):
        _emit_files(
            tmp_path,
            image="anything:test",
            admin_password="x",
            force=False,
        )


def test_emit_files_force_overwrites(tmp_path: Path) -> None:
    _emit_files(tmp_path, image="a:1", admin_password="p1", force=False)
    _emit_files(tmp_path, image="b:2", admin_password="p2", force=True)
    body = (tmp_path / "compose.yml").read_text(encoding="utf-8")
    assert "image: b:2" in body


def test_compose_yaml_bakes_image_tag() -> None:
    body = compose_yaml(image="ghcr.io/safl/pixie:0.4.0", admin_password="pw")
    assert "image: ghcr.io/safl/pixie:0.4.0" in body
    assert "network_mode: host" in body
    # PIXIE_ADMIN_PASSWORD gets a compose ``${VAR:-default}`` fallback
    # so an operator who forgets to fill envvars still lands on the
    # baked default; the envvars.example gives them the string to
    # copy-paste.
    assert "PIXIE_ADMIN_PASSWORD: ${PIXIE_ADMIN_PASSWORD:-pw}" in body


def test_envvars_example_lists_required_fields() -> None:
    body = envvars_example(admin_password="a-secret")
    assert "PIXIE_HOST_ADDR=" in body
    assert "PIXIE_ADMIN_PASSWORD=a-secret" in body


def test_detect_host_addr_returns_a_valid_ip_shape() -> None:
    addr = detect_host_addr()
    # Very loose: the LAN-probe trick may fall back to 127.0.0.1 on
    # a runner without any outbound route.
    parts = addr.split(".")
    assert len(parts) == 4
    for p in parts:
        assert p.isdigit() and 0 <= int(p) <= 255


def test_gen_admin_password_is_nontrivial() -> None:
    pw = gen_admin_password()
    assert len(pw) >= 32
    # url-safe base64 output; sanity-check the charset.
    assert all(c.isalnum() or c in "-_" for c in pw)


def test_argparse_has_three_subcommands() -> None:
    parser = _build_parser()
    # argparse subparsers surface via the ``dest`` attribute on the
    # subparsers action; grep them off the help text since that's
    # the operator-visible shape.
    help_text = parser.format_help()
    for cmd in ("init", "deploy", "purge"):
        assert cmd in help_text


def test_argparse_init_defaults_dest_to_cwd() -> None:
    parser = _build_parser()
    args = parser.parse_args(["init"])
    assert args.cmd == "init"
    assert args.dest  # non-empty string
    assert args.force is False


def test_argparse_deploy_accepts_flags() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "deploy",
            "/tmp/pixie-deploy",
            "--image",
            "ghcr.io/x/pixie:dev",
            "--admin-password",
            "hunter2",
            "--host-addr",
            "10.20.30.40",
            "--force",
        ]
    )
    assert args.dest == "/tmp/pixie-deploy"
    assert args.image == "ghcr.io/x/pixie:dev"
    assert args.admin_password == "hunter2"
    assert args.host_addr == "10.20.30.40"
    assert args.force is True
