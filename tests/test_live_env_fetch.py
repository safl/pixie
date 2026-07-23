"""Live-env fetch: the tarball unpack/stage, the settings source
resolution, and the ``POST /ui/live-env/fetch`` route (success + failure
paths, run on the fetch pool with the download stubbed out).
"""

from __future__ import annotations

import io
import tarfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pixie.catalog._fetcher import FetchError
from pixie.catalog._live_env import (
    LIVE_ENV_FILES,
    LiveEnvResult,
    _unpack_live_env_tar,
    stage_live_env,
)
from pixie.web._settings_store import (
    DEFAULT_LIVE_ENV_SRC,
    KEY_LIVE_ENV_SRC,
    SettingsStore,
)
from tests.conftest import authed


def _make_tar(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(str(path), mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


# ---------- unpack ---------------------------------------------------


def test_unpack_stages_the_trio(tmp_path: Path) -> None:
    tar = tmp_path / "live.tar.gz"
    _make_tar(
        tar,
        {
            "vmlinuz": b"KERNEL",
            "initrd": b"INITRD-BYTES",
            "live.squashfs": b"SQUASHFS" * 100,
        },
    )
    dest = tmp_path / "live-env"
    sizes = _unpack_live_env_tar(tar, dest)
    assert {p.name for p in dest.iterdir()} == set(LIVE_ENV_FILES)
    assert (dest / "vmlinuz").read_bytes() == b"KERNEL"
    assert sizes["live.squashfs"] == len(b"SQUASHFS" * 100)
    # No staging directory left behind.
    assert not any(p.name.startswith(".staging-") for p in dest.iterdir())


def test_unpack_rejects_missing_file(tmp_path: Path) -> None:
    tar = tmp_path / "bad.tar.gz"
    _make_tar(tar, {"vmlinuz": b"K", "initrd": b"I"})  # no squashfs
    with pytest.raises(FetchError, match="missing required file"):
        _unpack_live_env_tar(tar, tmp_path / "live-env")


def test_unpack_ignores_nested_and_stray_members(tmp_path: Path) -> None:
    tar = tmp_path / "sneaky.tar.gz"
    _make_tar(
        tar,
        {
            "vmlinuz": b"K",
            "initrd": b"I",
            "live.squashfs": b"S",
            "sub/evil": b"NOPE",  # nested -> ignored
            "README": b"noise",  # not a required name -> ignored
        },
    )
    dest = tmp_path / "live-env"
    _unpack_live_env_tar(tar, dest)
    assert {p.name for p in dest.iterdir()} == set(LIVE_ENV_FILES)


def test_stage_rejects_empty_src(tmp_path: Path) -> None:
    with pytest.raises(FetchError, match="no live-env src"):
        stage_live_env("", tmp_path / "live-env")


def test_stage_rejects_bad_scheme(tmp_path: Path) -> None:
    with pytest.raises(FetchError, match="unsupported src scheme"):
        stage_live_env("ftp://nope/x.tar.gz", tmp_path / "live-env")


# ---------- settings resolution --------------------------------------


def test_resolve_live_env_src_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SettingsStore(tmp_path / "state.db")
    # default
    monkeypatch.delenv("PIXIE_LIVE_ENV_SRC", raising=False)
    assert store.resolve_live_env_src() == DEFAULT_LIVE_ENV_SRC
    # env beats default
    monkeypatch.setenv("PIXIE_LIVE_ENV_SRC", "https://mirror/x.tar.gz")
    assert store.resolve_live_env_src() == "https://mirror/x.tar.gz"
    # DB override beats env
    store.set_value(KEY_LIVE_ENV_SRC, "https://override/y.tar.gz")
    assert store.resolve_live_env_src() == "https://override/y.tar.gz"


# ---------- routes ---------------------------------------------------


def _wait_fetch_done(client: TestClient, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fs = dict(client.app.state.live_env_fetch_state)
        if fs.get("state") in {"done", "error"}:
            return fs
        time.sleep(0.02)
    raise AssertionError(f"fetch never settled: {dict(client.app.state.live_env_fetch_state)}")


def test_ui_live_env_fetch_requires_auth(client: TestClient) -> None:
    r = client.post("/ui/live-env/fetch", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def test_ui_live_env_fetch_success(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    c = authed(client)

    def _fake_stage(src: str, live_env_dir: Path, *, progress=None) -> LiveEnvResult:
        live_env_dir.mkdir(parents=True, exist_ok=True)
        for name in LIVE_ENV_FILES:
            (live_env_dir / name).write_bytes(b"x" * 16)
        return LiveEnvResult(src=src, sha256="a" * 64, size=48, files={})

    monkeypatch.setattr("pixie.catalog._live_env.stage_live_env", _fake_stage)

    r = c.post("/ui/live-env/fetch", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/"
    fs = _wait_fetch_done(c)
    assert fs["state"] == "done"
    assert fs["sha256"] == "a" * 64
    # files actually landed in the live-env dir
    assert (Path(c.app.state.live_env_dir) / "live.squashfs").is_file()
    # a done event is on the log
    body = c.get("/events").json()
    assert any(e["kind"] == "live_env.fetch.done" for e in body["events"])


def test_ui_live_env_fetch_failure_records_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = authed(client)

    def _boom(src: str, live_env_dir: Path, *, progress=None) -> LiveEnvResult:
        raise FetchError("download exploded")

    monkeypatch.setattr("pixie.catalog._live_env.stage_live_env", _boom)

    c.post("/ui/live-env/fetch", follow_redirects=False)
    fs = _wait_fetch_done(c)
    assert fs["state"] == "error"
    assert "download exploded" in fs["error"]
    body = c.get("/events").json()
    assert any(e["kind"] == "live_env.fetch.failed" for e in body["events"])


def test_ui_settings_live_env_src_override_roundtrip(client: TestClient) -> None:
    c = authed(client)
    r = c.post(
        "/ui/settings/live-env-src/edit",
        data={"live_env_src": "https://mirror.local/pixie-live-env-x86_64.tar.gz"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    store = c.app.state.settings_store
    assert store.resolve_live_env_src() == "https://mirror.local/pixie-live-env-x86_64.tar.gz"
    # blank clears back to default
    c.post("/ui/settings/live-env-src/edit", data={"live_env_src": ""}, follow_redirects=False)
    assert store.resolve_live_env_src() == DEFAULT_LIVE_ENV_SRC


def test_dashboard_shows_fetch_button_and_source(client: TestClient) -> None:
    c = authed(client)
    body = c.get("/ui/").text
    assert "Fetch live-env" in body
    assert "/ui/live-env/fetch" in body
    assert "pixie-live-env-x86_64.tar.gz" in body  # the default source
