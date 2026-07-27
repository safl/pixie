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


def test_argparse_purge_accepts_flags() -> None:
    parser = _build_parser()
    args = parser.parse_args(["purge", "/opt/pixie", "--data", "--images", "--all", "--yes"])
    assert args.cmd == "purge"
    assert args.data and args.images and args.all and args.yes


def _fake_stdin_no_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    from pixie.deploy import _main

    monkeypatch.setattr(_main.sys, "stdin", io.StringIO(""))  # isatty() -> False


def test_purge_data_yes_removes_bind_mounted_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pixie.deploy import _main

    monkeypatch.setattr(_main, "_compose_cmd", lambda: ["true"])
    (tmp_path / "envvars").write_text("PIXIE_ADMIN_PASSWORD=x\n")
    data = tmp_path / "data"
    data.mkdir()
    (data / "state.db").write_text("x")
    args = _build_parser().parse_args(["purge", str(tmp_path), "--data", "--yes"])
    assert _main._cmd_purge(args) == 0
    assert not data.exists()  # bind-mounted data actually removed


def test_purge_yes_without_data_keeps_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pixie.deploy import _main

    monkeypatch.setattr(_main, "_compose_cmd", lambda: ["true"])
    (tmp_path / "envvars").write_text("x\n")
    data = tmp_path / "data"
    data.mkdir()
    args = _build_parser().parse_args(["purge", str(tmp_path), "--yes"])
    assert _main._cmd_purge(args) == 0
    assert data.exists()  # plain stop leaves state in place


def test_purge_without_yes_aborts_unattended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pixie.deploy import _main

    _fake_stdin_no_tty(monkeypatch)
    monkeypatch.setattr(_main, "_compose_cmd", lambda: ["true"])
    (tmp_path / "envvars").write_text("x\n")
    data = tmp_path / "data"
    data.mkdir()
    args = _build_parser().parse_args(["purge", str(tmp_path), "--data"])
    assert _main._cmd_purge(args) == 1  # refused; no TTY, no --yes
    assert data.exists()  # nothing changed


def test_purge_all_removes_deploy_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pixie.deploy import _main

    monkeypatch.setattr(_main, "_compose_cmd", lambda: ["true"])
    (tmp_path / "envvars").write_text("x\n")
    (tmp_path / "compose.yml").write_text("services: {}\n")
    (tmp_path / "data").mkdir()
    args = _build_parser().parse_args(["purge", str(tmp_path), "--all", "--yes"])
    assert _main._cmd_purge(args) == 0
    assert not tmp_path.exists()  # --all removes the deploy dir


def test_purge_all_reports_gracefully_when_dir_cannot_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the deploy dir sits under a root-owned parent, rmtree empties
    it but can't rmdir the dir itself. Purge must report that plainly and
    still exit 0 -- not dump a traceback (the state is gone regardless)."""
    from pixie.deploy import _main

    monkeypatch.setattr(_main, "_compose_cmd", lambda: ["true"])
    dest = tmp_path / "pixie"
    dest.mkdir()
    (dest / "envvars").write_text("x\n")
    (dest / "compose.yml").write_text("services: {}\n")

    real_rmtree = _main.shutil.rmtree

    def fake_rmtree(path: object) -> None:
        p = Path(str(path))
        if p == dest:
            # Mimic real rmtree under a root-owned parent: children go,
            # then the final rmdir(dest) raises.
            for child in p.iterdir():
                child.unlink() if child.is_file() else real_rmtree(child)
            raise PermissionError(13, "Permission denied")
        real_rmtree(p)

    monkeypatch.setattr(_main.shutil, "rmtree", fake_rmtree)

    args = _build_parser().parse_args(["purge", str(dest), "--all", "--yes"])
    assert _main._cmd_purge(args) == 0  # caveat, not failure
    assert dest.exists() and not list(dest.iterdir())  # emptied, dir remains
    err = capsys.readouterr().err
    assert "could not remove the directory itself" in err
    assert "Traceback" not in err


def test_pull_image_streams_when_tool_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """``deploy`` pulls the image with visible progress before compose
    up so a cold first deploy does not sit silent (reads as a hang).
    The pull must NOT capture output -- it streams to the terminal."""
    from pixie.deploy import _main

    monkeypatch.setattr(
        _main.shutil, "which", lambda t: "/usr/bin/podman" if t == "podman" else None
    )
    calls: list = []
    monkeypatch.setattr(_main.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)))
    _main._pull_image("ghcr.io/safl/pixie:0.4.4")
    assert calls, "expected a pull invocation"
    cmd, kw = calls[0]
    assert cmd == ["/usr/bin/podman", "pull", "ghcr.io/safl/pixie:0.4.4"]
    assert not kw.get("capture_output")  # streamed, not hidden


def test_pull_image_noop_without_container_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """No podman/docker on PATH -> the pull is a best-effort no-op (the
    compose up still runs and surfaces any real error)."""
    from pixie.deploy import _main

    monkeypatch.setattr(_main.shutil, "which", lambda t: None)
    called: list = []
    monkeypatch.setattr(_main.subprocess, "run", lambda *a, **k: called.append(a))
    _main._pull_image("x:1")
    assert not called


def _which_only(monkeypatch: pytest.MonkeyPatch, present: set[str]) -> None:
    from pixie.deploy import _main

    monkeypatch.setattr(_main.shutil, "which", lambda t: f"/usr/bin/{t}" if t in present else None)


def test_compose_cmd_prefers_podman_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    from pixie.deploy import _main

    _which_only(monkeypatch, {"podman-compose", "podman", "docker"})
    assert _main._compose_cmd() == ["podman-compose"]


def test_compose_cmd_podman_when_no_docker_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    from pixie.deploy import _main

    _which_only(monkeypatch, {"podman"})
    assert _main._compose_cmd() == ["podman", "compose"]


def test_compose_cmd_docker_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from pixie.deploy import _main

    # docker-compose present blocks the ``podman compose`` branch; with
    # no podman-compose and no podman, fall through to docker compose.
    _which_only(monkeypatch, {"docker", "docker-compose"})
    assert _main._compose_cmd() == ["docker", "compose"]


def test_compose_cmd_none_when_no_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    from pixie.deploy import _main

    _which_only(monkeypatch, set())
    assert _main._compose_cmd() is None


def test_compose_image_reads_image_line(tmp_path: Path) -> None:
    from pixie.deploy import _main

    (tmp_path / "compose.yml").write_text(
        "services:\n  pixie:\n    image: ghcr.io/safl/pixie:1.2.3\n", encoding="utf-8"
    )
    assert _main._compose_image(tmp_path) == "ghcr.io/safl/pixie:1.2.3"


def test_compose_image_none_when_absent(tmp_path: Path) -> None:
    from pixie.deploy import _main

    assert _main._compose_image(tmp_path) is None


def test_compose_up_no_runner_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from pixie.deploy import _main

    monkeypatch.setattr(_main, "_compose_cmd", lambda: None)
    with pytest.raises(RuntimeError, match="no compose runner"):
        _main._compose_up(Path("/x"), Path("/x/envvars"))


def test_cmd_deploy_success_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pixie.deploy import _main

    dest = tmp_path / "d"
    monkeypatch.setattr(_main, "detect_host_addr", lambda: "10.0.0.5")
    monkeypatch.setattr(_main, "_pull_image", lambda image: None)
    monkeypatch.setattr(_main, "_compose_up", lambda dest, envvars: None)
    monkeypatch.setattr(_main, "_wait_healthz", lambda h, p, d: None)
    args = _build_parser().parse_args(["deploy", str(dest), "--admin-password", "sekret"])
    assert _main._cmd_deploy(args) == 0
    envvars = (dest / "envvars").read_text(encoding="utf-8")
    assert "PIXIE_HOST_ADDR=10.0.0.5" in envvars
    assert "PIXIE_ADMIN_PASSWORD=sekret" in envvars


def test_cmd_deploy_autofills_host_and_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pixie.deploy import _main

    dest = tmp_path / "d"
    monkeypatch.setattr(_main, "detect_host_addr", lambda: "192.0.2.9")
    monkeypatch.setattr(_main, "gen_admin_password", lambda: "RANDOMTOKEN")
    monkeypatch.setattr(_main, "_pull_image", lambda image: None)
    monkeypatch.setattr(_main, "_compose_up", lambda dest, envvars: None)
    monkeypatch.setattr(_main, "_wait_healthz", lambda h, p, d: None)
    args = _build_parser().parse_args(["deploy", str(dest)])  # no flags -> auto-fill
    assert _main._cmd_deploy(args) == 0
    envvars = (dest / "envvars").read_text(encoding="utf-8")
    assert "PIXIE_HOST_ADDR=192.0.2.9" in envvars
    assert "PIXIE_ADMIN_PASSWORD=RANDOMTOKEN" in envvars


def test_cmd_deploy_compose_failure_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pixie.deploy import _main

    dest = tmp_path / "d"
    monkeypatch.setattr(_main, "detect_host_addr", lambda: "127.0.0.1")
    monkeypatch.setattr(_main, "_pull_image", lambda image: None)

    def boom(dest: Path, envvars: Path) -> None:
        raise RuntimeError("compose up failed (rc=1)")

    monkeypatch.setattr(_main, "_compose_up", boom)
    args = _build_parser().parse_args(["deploy", str(dest)])
    assert _main._cmd_deploy(args) == 1


def test_cmd_deploy_refuses_existing_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pixie.deploy import _main

    dest = tmp_path / "d"
    dest.mkdir()
    (dest / "compose.yml").write_text("x", encoding="utf-8")
    monkeypatch.setattr(_main, "detect_host_addr", lambda: "127.0.0.1")
    args = _build_parser().parse_args(["deploy", str(dest)])
    assert _main._cmd_deploy(args) == 1  # FileExistsError -> exit 1


def test_wait_healthz_returns_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    from pixie.deploy import _main

    class _Resp:
        status = 200

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

    monkeypatch.setattr(_main.urllib.request, "urlopen", lambda url, timeout=0: _Resp())
    _main._wait_healthz("127.0.0.1", 8080, _main.time.monotonic() + 5)  # no raise


def test_wait_healthz_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from pixie.deploy import _main

    # Deadline already in the past -> the poll loop never runs, so it
    # raises immediately without sleeping.
    with pytest.raises(RuntimeError, match="healthz timeout"):
        _main._wait_healthz("127.0.0.1", 8080, _main.time.monotonic() - 1)
