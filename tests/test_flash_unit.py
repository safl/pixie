"""Unit tests for pixie.flash pure helpers.

flash.py is the destructive disk-writing core; its streaming path needs
real block devices (integration-only), but the digest-normalise /
integrity-verify / compressed-size-parse helpers are pure and were
previously untested. These guard the size-fits check (which decides
whether an image is allowed to flash at all) and the post-write
integrity verify against silent breakage.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import pixie.flash as flash
from pixie.flash import (
    FlashIntegrityError,
    ImageInfo,
    TargetInfo,
    _normalize_digest,
    _parse_compressed_listing,
    _parse_gzip_listing,
    _sha256_file,
    _verify_digest,
    make_plan,
    probe_image,
    probe_target,
    validate_plan,
)


def _block_target(size: int | None, mountpoints: list[str] | None = None) -> TargetInfo:
    """A plausible block-device target, built directly so the pure
    validate/plan branches are testable without root or a real device."""
    return TargetInfo(
        path=Path("/dev/fake"),
        exists=True,
        is_block_device=True,
        size_bytes=size,
        mountpoints=mountpoints or [],
    )


def _img(path: str, fmt: str | None, vsize: int | None) -> ImageInfo:
    return ImageInfo(path=Path(path), format=fmt, size_bytes=vsize or 1, virtual_size_bytes=vsize)


def test_normalize_digest_none() -> None:
    assert _normalize_digest(None) is None


def test_normalize_digest_bare_hex_lowercased_and_prefixed() -> None:
    assert _normalize_digest("  ABCDEF0123  ") == "sha256:abcdef0123"


def test_normalize_digest_passthrough_when_prefixed() -> None:
    assert _normalize_digest("sha256:deadbeef") == "sha256:deadbeef"


def test_verify_digest_match_no_raise() -> None:
    _verify_digest("sha256:x", "sha256:x", "http://u")  # no raise


def test_verify_digest_observed_none_is_noop() -> None:
    # A source that couldn't compute a digest (no oras layer, no declared
    # sha) verifies as a no-op rather than failing the flash.
    _verify_digest("sha256:x", None, "http://u")  # no raise


def test_verify_digest_mismatch_raises() -> None:
    with pytest.raises(FlashIntegrityError):
        _verify_digest("sha256:aaa", "sha256:bbb", "http://u")


def test_parse_gzip_listing_normal() -> None:
    out = (
        "         compressed        uncompressed  ratio uncompressed_name\n"
        "               1000                5000 -80.0% img\n"
    )
    assert _parse_gzip_listing(out) == 5000


def test_parse_gzip_listing_wrap_returns_none() -> None:
    # gzip stores uncompressed size mod 4 GiB; when the reported value is
    # smaller than the compressed bytes it wrapped and must be refused so
    # validate_plan doesn't fit a too-big image onto a too-small disk.
    out = "compressed uncompressed ratio name\n 5000 1000 -1% img\n"
    assert _parse_gzip_listing(out) is None


def test_parse_gzip_listing_garbage_returns_none() -> None:
    assert _parse_gzip_listing("") is None
    assert _parse_gzip_listing("not a table\n") is None


def test_parse_compressed_listing_zstd() -> None:
    listing = (
        "Frames  Skips  Compressed  Uncompressed  Ratio  Check  Filename\n"
        "     1      0    12.5 KiB     900.00 MiB  72.00  XXH64  img.zst\n"
    )
    assert _parse_compressed_listing(listing, header_prefix="Frames") == 900 * 1024**2


def test_parse_compressed_listing_xz() -> None:
    listing = (
        "Strms  Blocks   Compressed Uncompressed  Ratio  Check   Filename\n"
        "    1       1     10.0 MiB      2.0 GiB  0.005  CRC64   img.xz\n"
    )
    assert _parse_compressed_listing(listing, header_prefix="Strms") == 2 * 1024**3


def test_parse_compressed_listing_no_data_row_returns_none() -> None:
    assert _parse_compressed_listing("Frames Skips ...\n", header_prefix="Frames") is None


def test_sha256_file_matches_hashlib(tmp_path) -> None:
    data = b"pixie flash test bytes"
    p = tmp_path / "blob"
    p.write_bytes(data)
    assert _sha256_file(p) == "sha256:" + hashlib.sha256(data).hexdigest()


# ---- probe + plan + validate (pure; no root or real block device) -----


def test_probe_image_raw_img_reports_format_and_size(tmp_path) -> None:
    p = tmp_path / "disk.img"
    p.write_bytes(b"\0" * 4096)
    info = probe_image(p)
    assert info.format == "img"
    assert info.size_bytes == 4096
    assert info.virtual_size_bytes == 4096  # raw: on-disk size == bytes written


def test_probe_image_missing_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        probe_image(tmp_path / "nope.img")


def test_probe_image_unrecognized_extension_has_no_format(tmp_path) -> None:
    p = tmp_path / "mystery.bin"
    p.write_bytes(b"x")
    assert probe_image(p).format is None


def test_probe_target_missing_path(tmp_path) -> None:
    t = probe_target(tmp_path / "absent")
    assert not t.exists and not t.is_block_device and t.size_bytes is None


def test_probe_target_regular_file_is_not_a_block_device(tmp_path) -> None:
    p = tmp_path / "regular"
    p.write_bytes(b"x")
    t = probe_target(p)
    assert t.exists and not t.is_block_device


def test_validate_ok_when_image_fits_block_target() -> None:
    plan = make_plan(_img("d.img", "img", 100), _block_target(1000))
    assert validate_plan(plan) == []


def test_validate_rejects_unrecognized_format() -> None:
    errs = validate_plan(make_plan(_img("mystery.bin", None, None), _block_target(1000)))
    assert any("format not recognised" in e for e in errs)


def test_validate_rejects_tarball_with_extract_hint() -> None:
    errs = validate_plan(make_plan(_img("bundle.tar.gz", None, None), _block_target(1000)))
    assert any("tarball" in e and "Extract first" in e for e in errs)


def test_validate_rejects_missing_target() -> None:
    tgt = TargetInfo(
        path=Path("/dev/gone"),
        exists=False,
        is_block_device=False,
        size_bytes=None,
        mountpoints=[],
    )
    errs = validate_plan(make_plan(_img("d.img", "img", 1), tgt))
    assert any("does not exist" in e for e in errs)


def test_validate_rejects_non_block_target() -> None:
    tgt = TargetInfo(
        path=Path("/some/file"),
        exists=True,
        is_block_device=False,
        size_bytes=None,
        mountpoints=[],
    )
    errs = validate_plan(make_plan(_img("d.img", "img", 1), tgt))
    assert any("not a block device" in e for e in errs)


def test_validate_rejects_mounted_target() -> None:
    tgt = _block_target(1000, mountpoints=["/mnt/data"])
    errs = validate_plan(make_plan(_img("d.img", "img", 1), tgt))
    assert any("mounted partitions" in e for e in errs)


def test_validate_rejects_image_larger_than_target() -> None:
    errs = validate_plan(make_plan(_img("big.img", "img", 2000), _block_target(1000)))
    assert any("larger than target" in e for e in errs)


def test_make_plan_notes_unknown_virtual_size() -> None:
    img = ImageInfo(
        path=None,
        url="http://x/i.img.gz",
        format="img.gz",
        size_bytes=10,
        virtual_size_bytes=None,
    )
    plan = make_plan(img, _block_target(1000))
    assert any("size-fits-target check skipped" in n for n in plan.notes)


# ---- UEFI boot-entry registration -------------------------------------
#
# register_uefi_boot_entry makes a freshly-flashed disk bootable by
# pointing the firmware at the ESP fallback loader. It shells to
# efibootmgr and reads lsblk; both are simulated here (a state-backed
# stub on PATH + monkeypatched ESP discovery) so the full orchestration
# -- idempotent delete of prior pixie entries, create-only, one-shot
# BootNext with BootOrder left untouched -- runs without real firmware or
# the udev that populates lsblk PARTTYPE.

_FAKE_EFIBOOTMGR = r"""#!/usr/bin/env python3
import json, os, sys

state_path = os.environ["FAKE_EFIBOOTMGR_STATE"]
with open(state_path) as fh:
    state = json.load(fh)
argv = sys.argv[1:]
state["calls"].append(argv)
if "--create-only" in argv:
    num = "%04X" % state["next"]
    state["next"] += 1
    state["entries"][num] = argv[argv.index("--label") + 1]
elif "-B" in argv and "-b" in argv:
    state["entries"].pop(argv[argv.index("-b") + 1], None)
elif "-n" in argv:
    state["bootnext"] = argv[argv.index("-n") + 1]
lines = []
if state["bootnext"] is not None:
    lines.append("BootNext: " + state["bootnext"])
for num, label in state["entries"].items():
    lines.append("Boot%s* %s\tHD(1,GPT)" % (num, label))
sys.stdout.write("\n".join(lines) + "\n")
with open(state_path, "w") as fh:
    json.dump(state, fh)
"""


@pytest.fixture
def fake_efibootmgr(tmp_path, monkeypatch) -> Path:
    """Put a state-backed ``efibootmgr`` stub on PATH, seeded with a
    firmware entry (0000) + a stale pixie entry (0005) so the idempotent
    'delete our own prior entries' path runs. Returns the NVRAM state
    file for assertions."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "efibootmgr"
    script.write_text(_FAKE_EFIBOOTMGR)
    script.chmod(0o755)
    state = tmp_path / "nvram.json"
    state.write_text(
        json.dumps(
            {
                "entries": {"0000": "netboot BOOTIF", "0005": "pixie flashed"},
                "next": 6,
                "bootnext": None,
                "calls": [],
            }
        )
    )
    monkeypatch.setenv("FAKE_EFIBOOTMGR_STATE", str(state))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return state


def _force_uefi(monkeypatch) -> None:
    """Make the ``/sys/firmware/efi/efivars`` UEFI-mode gate report true
    without touching /sys (every other path delegates to the real check)."""
    orig = Path.is_dir
    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda self: True if str(self) == "/sys/firmware/efi/efivars" else orig(self),
    )


def test_register_uefi_boot_entry_success(fake_efibootmgr, monkeypatch) -> None:
    _force_uefi(monkeypatch)
    monkeypatch.setattr(flash, "_find_esp_partition_number", lambda disk: 1)
    status = flash.register_uefi_boot_entry(Path("/dev/fake"), label="pixie flashed")
    assert "registered UEFI boot entry" in status
    assert "ESP partition 1" in status

    nvram = json.loads(fake_efibootmgr.read_text())
    assert "0000" in nvram["entries"]  # firmware's own entry survives
    assert "0005" not in nvram["entries"]  # stale pixie entry deleted
    assert nvram["bootnext"] in nvram["entries"]  # BootNext points at a live entry
    assert nvram["entries"][nvram["bootnext"]] == "pixie flashed"
    flags = [tok for call in nvram["calls"] for tok in call]
    assert "--create-only" in flags and "-n" in flags


def test_register_uefi_skips_when_not_uefi(monkeypatch) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda self: False)
    status = flash.register_uefi_boot_entry(Path("/dev/x"))
    assert "not booted in UEFI mode" in status


def test_register_uefi_skips_when_efibootmgr_absent(monkeypatch) -> None:
    _force_uefi(monkeypatch)
    monkeypatch.setattr(flash.shutil, "which", lambda _name: None)
    status = flash.register_uefi_boot_entry(Path("/dev/x"))
    assert "efibootmgr not installed" in status


def test_register_uefi_skips_when_no_esp(fake_efibootmgr, monkeypatch) -> None:
    _force_uefi(monkeypatch)
    monkeypatch.setattr(flash, "_find_esp_partition_number", lambda disk: None)
    status = flash.register_uefi_boot_entry(Path("/dev/x"))
    assert "no EFI System Partition" in status


# ---- ESP discovery + boot-entry line parsing --------------------------

_ESP_GUID = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
_LINUX_GUID = "0fc63daf-8483-4772-8e79-3d69d8477de4"


def _fake_lsblk(json_out: str, monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json_out, stderr="")

    monkeypatch.setattr(flash.subprocess, "run", fake_run)


def test_find_esp_partition_number_from_partn(monkeypatch) -> None:
    out = json.dumps(
        {
            "blockdevices": [
                {
                    "path": "/dev/sda",
                    "parttype": None,
                    "partn": None,
                    "children": [
                        {"path": "/dev/sda1", "parttype": _ESP_GUID, "partn": 1},
                        {"path": "/dev/sda2", "parttype": _LINUX_GUID, "partn": 2},
                    ],
                }
            ]
        }
    )
    _fake_lsblk(out, monkeypatch)
    assert flash._find_esp_partition_number(Path("/dev/sda")) == 1


def test_find_esp_partition_number_path_digit_fallback(monkeypatch) -> None:
    # Older lsblk without PARTN: derive the number from PATH's trailing digits.
    out = json.dumps(
        {
            "blockdevices": [
                {
                    "path": "/dev/nvme0n1",
                    "children": [{"path": "/dev/nvme0n1p1", "parttype": _ESP_GUID}],
                }
            ]
        }
    )
    _fake_lsblk(out, monkeypatch)
    assert flash._find_esp_partition_number(Path("/dev/nvme0n1")) == 1


def test_find_esp_partition_number_none_when_no_esp(monkeypatch) -> None:
    out = json.dumps(
        {
            "blockdevices": [
                {
                    "path": "/dev/sdb",
                    "children": [{"path": "/dev/sdb1", "parttype": _LINUX_GUID, "partn": 1}],
                }
            ]
        }
    )
    _fake_lsblk(out, monkeypatch)
    assert flash._find_esp_partition_number(Path("/dev/sdb")) is None


def test_find_esp_partition_number_survives_lsblk_error(monkeypatch) -> None:
    def boom(cmd, **kwargs):
        raise FileNotFoundError("lsblk")

    monkeypatch.setattr(flash.subprocess, "run", boom)
    assert flash._find_esp_partition_number(Path("/dev/sdc")) is None


def test_boot_entries_with_label_matches_exact_label_only() -> None:
    out = (
        "BootNext: 0006\n"
        "BootOrder: 0000,0006\n"
        "Boot0000* netboot BOOTIF\tPXE\n"
        "Boot0006* pixie flashed\tHD(1,GPT)\n"
        "Boot0007* pixie flashed extra\tHD\n"  # different label: must NOT match
    )
    assert flash._boot_entries_with_label(out, "pixie flashed") == ["0006"]


# ---- probe_image_url (HEAD via urllib, mocked) ------------------------


class _FakeResp:
    def __init__(self, content_length: str | None) -> None:
        self.headers = {} if content_length is None else {"Content-Length": content_length}

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _mock_urlopen(monkeypatch, resp_or_exc: object) -> None:
    def fake(req: object, timeout: float = 30) -> object:
        if isinstance(resp_or_exc, Exception):
            raise resp_or_exc
        return resp_or_exc

    monkeypatch.setattr(urllib.request, "urlopen", fake)


def test_probe_image_url_raw_img_sets_virtual_size(monkeypatch) -> None:
    _mock_urlopen(monkeypatch, _FakeResp("2048"))
    info = flash.probe_image_url("http://h/disk.img")
    assert info.format == "img"
    assert info.size_bytes == 2048
    assert info.virtual_size_bytes == 2048  # raw: source size == virtual size


def test_probe_image_url_compressed_leaves_virtual_size_unknown(monkeypatch) -> None:
    _mock_urlopen(monkeypatch, _FakeResp("500"))
    info = flash.probe_image_url("http://h/disk.img.gz")
    assert info.format == "img.gz"
    assert info.size_bytes == 500
    assert info.virtual_size_bytes is None  # can't know without pulling the body


def test_probe_image_url_no_extension_uses_format_hint(monkeypatch) -> None:
    # pixie's /images/<sha>/<display-name> route has no file extension.
    _mock_urlopen(monkeypatch, _FakeResp("100"))
    info = flash.probe_image_url("http://h/images/abc123/My Disk", format_hint="img.zst")
    assert info.format == "img.zst"


def test_probe_image_url_bad_content_length_folds_to_unknown(monkeypatch) -> None:
    _mock_urlopen(monkeypatch, _FakeResp("not-a-number"))
    info = flash.probe_image_url("http://h/disk.img")
    assert info.size_bytes == 0
    assert info.virtual_size_bytes is None


def test_probe_image_url_normalises_expected_sha(monkeypatch) -> None:
    _mock_urlopen(monkeypatch, _FakeResp("10"))
    info = flash.probe_image_url("http://h/disk.img", expected_sha="ABCDEF")
    assert info.expected_sha == "sha256:abcdef"


def test_probe_image_url_unreachable_raises_filenotfound(monkeypatch) -> None:
    _mock_urlopen(monkeypatch, urllib.error.URLError("connection refused"))
    with pytest.raises(FileNotFoundError):
        flash.probe_image_url("http://h/disk.img")


def test_probe_image_url_rejects_non_http_scheme() -> None:
    with pytest.raises(ValueError, match="http://, https://, or oras://"):
        flash.probe_image_url("ftp://h/disk.img")


# ---- curl arg building + secret redaction -----------------------------


def test_curl_args_for_plain_http_passes_through() -> None:
    argv, size, digest = flash._curl_args_for_source("http://h/disk.img")
    assert argv[-1] == "http://h/disk.img"
    assert size is None and digest is None


def test_redact_secrets_masks_bearer_token() -> None:
    line = "curl -H 'Authorization: Bearer sk-secret-123.abc' http://h"
    out = flash._redact_secrets(line)
    assert "sk-secret-123.abc" not in out
    assert "<redacted>" in out


# ---- _image_virtual_size per-format branches (probe helpers mocked) ---


def _mock_probe_run(monkeypatch, stdout: str | None, returncode: int = 0) -> None:
    def fake(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str] | None:
        if stdout is None:
            return None  # simulates a probe timeout
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(flash, "_probe_run", fake)


def test_image_virtual_size_qcow2_reads_virtual_size(monkeypatch) -> None:
    _mock_probe_run(monkeypatch, json.dumps({"virtual-size": 4096}))
    assert flash._image_virtual_size(Path("x.qcow2"), "qcow2") == 4096


def test_image_virtual_size_qcow2_bad_json_is_none(monkeypatch) -> None:
    _mock_probe_run(monkeypatch, "not json")
    assert flash._image_virtual_size(Path("x.qcow2"), "qcow2") is None


def test_image_virtual_size_qcow2_probe_timeout_is_none(monkeypatch) -> None:
    _mock_probe_run(monkeypatch, None)
    assert flash._image_virtual_size(Path("x.qcow2"), "qcow2") is None


def test_image_virtual_size_zst_probe_failure_is_none(monkeypatch) -> None:
    _mock_probe_run(monkeypatch, "", returncode=1)
    assert flash._image_virtual_size(Path("x.img.zst"), "img.zst") is None


def test_image_virtual_size_xz_parses_listing(monkeypatch) -> None:
    listing = (
        "Strms  Blocks   Compressed Uncompressed  Ratio  Check   Filename\n"
        "    1       1     10.0 MiB      2.0 GiB  0.005  CRC64   x.xz\n"
    )
    _mock_probe_run(monkeypatch, listing)
    assert flash._image_virtual_size(Path("x.img.xz"), "img.xz") == 2 * 1024**3


def test_image_virtual_size_gz_parses_listing(monkeypatch) -> None:
    listing = "compressed uncompressed ratio name\n            1000 5000 -80% x\n"
    _mock_probe_run(monkeypatch, listing)
    assert flash._image_virtual_size(Path("x.img.gz"), "img.gz") == 5000


def test_image_virtual_size_bz2_is_none() -> None:
    # bzip2 carries no uncompressed-size header.
    assert flash._image_virtual_size(Path("x.img.bz2"), "img.bz2") is None


def test_image_virtual_size_unknown_format_is_none() -> None:
    assert flash._image_virtual_size(Path("mystery"), None) is None


def test_image_virtual_size_xz_probe_failure_is_none(monkeypatch) -> None:
    _mock_probe_run(monkeypatch, "", returncode=1)
    assert flash._image_virtual_size(Path("x.img.xz"), "img.xz") is None


def test_image_virtual_size_gz_probe_timeout_is_none(monkeypatch) -> None:
    _mock_probe_run(monkeypatch, None)
    assert flash._image_virtual_size(Path("x.img.gz"), "img.gz") is None


def test_probe_run_returns_none_on_timeout(monkeypatch) -> None:
    def boom(*a: object, **k: object) -> object:
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    monkeypatch.setattr(flash.subprocess, "run", boom)
    assert flash._probe_run(["sleep", "5"]) is None


# ---- dd progress + subprocess-log pumps (fed synthetic streams) -------


def test_pump_dd_progress_emits_latest_byte_count() -> None:
    events: list[flash.FlashProgress] = []
    stream = io.StringIO("1000 bytes copied\r2000 bytes copied\r4000 bytes (4 kB) copied, 0.1 s\n")
    flash._pump_dd_progress(stream, events.append, total_bytes=8000)
    assert events and events[-1].event == "writing_progress"
    assert events[-1].bytes_written == 4000
    assert events[-1].total_bytes == 8000


def test_pump_dd_progress_drains_trailing_partial_line() -> None:
    # A long stream ending in a newline-less partial line exercises the
    # partial-buffer carry + the EOF drain path.
    events: list[flash.FlashProgress] = []
    data = "100 bytes copied\r" * 30 + "999 bytes copied"  # no trailing newline
    flash._pump_dd_progress(io.StringIO(data), events.append, None)
    assert any(e.bytes_written == 999 for e in events)


class _FakeProc:
    def __init__(self, stderr_lines: list[bytes]) -> None:
        self.stderr = iter(stderr_lines)


def test_subprocess_log_pump_emits_lines_and_redacts_bearer() -> None:
    events: list[flash.FlashProgress] = []
    proc = _FakeProc([b"fetching disk.img\n", b"Authorization: Bearer sekret-token\n", b""])
    thread = flash._start_subprocess_log_pump(proc, events.append, "curl")  # type: ignore[arg-type]
    assert thread is not None
    thread.join(timeout=2)
    notes = [e.note for e in events]
    assert any("fetching disk.img" in (n or "") for n in notes)
    assert all("sekret-token" not in (n or "") for n in notes)  # redacted


def test_subprocess_log_pump_returns_none_without_callback() -> None:
    proc = _FakeProc([b"x\n"])
    assert flash._start_subprocess_log_pump(proc, None, "curl") is None  # type: ignore[arg-type]


def test_register_uefi_reports_unconfirmed_bootnext(fake_efibootmgr, monkeypatch) -> None:
    # If the freshly-created entry can't be found back in the listing, the
    # function still reports success but flags BootNext as unconfirmed.
    _force_uefi(monkeypatch)
    monkeypatch.setattr(flash, "_find_esp_partition_number", lambda disk: 1)
    monkeypatch.setattr(flash, "_boot_entries_with_label", lambda out, label: [])
    status = flash.register_uefi_boot_entry(Path("/dev/x"))
    assert "could not confirm BootNext" in status


# ---- more listing-parse + lsblk-probe edge branches -------------------


def test_parse_compressed_listing_bad_value_is_none() -> None:
    # A malformed numeric cell (multiple dots) fails float() -> None.
    listing = "Frames Skips Compressed Uncompressed Ratio Check Filename\n 12.5 KiB 1.2.3 MiB\n"
    assert _parse_compressed_listing(listing, header_prefix="Frames") is None


def test_parse_gzip_listing_skips_non_integer_row() -> None:
    out = "compressed uncompressed ratio name\n foo bar baz\n 1000 5000 -80% x\n"
    assert _parse_gzip_listing(out) == 5000


def test_lsblk_target_size_none_on_probe_failure(monkeypatch) -> None:
    monkeypatch.setattr(flash, "_probe_run", lambda cmd, **k: None)
    assert flash._lsblk_target_size(Path("/dev/x")) is None


def test_lsblk_target_size_none_on_garbage(monkeypatch) -> None:
    monkeypatch.setattr(
        flash,
        "_probe_run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout="not-a-number\n", stderr=""),
    )
    assert flash._lsblk_target_size(Path("/dev/x")) is None


def test_lsblk_target_mountpoints_empty_on_probe_failure(monkeypatch) -> None:
    monkeypatch.setattr(flash, "_probe_run", lambda cmd, **k: None)
    assert flash._lsblk_target_mountpoints(Path("/dev/x")) == []


def test_lsblk_target_mountpoints_parses_lines(monkeypatch) -> None:
    monkeypatch.setattr(
        flash,
        "_probe_run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 0, stdout="\n/boot/efi\n\n/mnt/data\n", stderr=""
        ),
    )
    assert flash._lsblk_target_mountpoints(Path("/dev/x")) == ["/boot/efi", "/mnt/data"]


# ---- oras:// resolution (registry client mocked) ----------------------

from pixie import oras  # noqa: E402


def _fake_resolved(**overrides: object):
    fields = {
        "blob_url": "https://reg.example/v2/img/blobs/sha256:" + "a" * 64,
        "headers": {"Authorization": "Bearer tok123"},
        "digest": "sha256:" + "a" * 64,
        "size": 1234,
        "title": "disk.img.gz",
    }
    fields.update(overrides)
    return oras.ResolvedBlob(**fields)  # type: ignore[arg-type]


def _mock_oras(monkeypatch, resolver) -> None:
    monkeypatch.setattr(flash.oras, "is_oras_url", lambda _u: True)
    monkeypatch.setattr(flash.oras, "resolve_ref", resolver)


def test_probe_image_url_oras_resolves_layer(monkeypatch) -> None:
    _mock_oras(monkeypatch, lambda _u: _fake_resolved())
    info = flash.probe_image_url("oras://reg.example/img:tag")
    assert info.format == "img.gz"  # from the layer title annotation
    assert info.size_bytes == 1234
    assert info.virtual_size_bytes is None  # compressed: unknown without a pull


def test_probe_image_url_oras_defaults_format_without_title(monkeypatch) -> None:
    _mock_oras(monkeypatch, lambda _u: _fake_resolved(title=None))
    assert flash.probe_image_url("oras://reg.example/img:tag").format == "img.gz"


def test_probe_image_url_oras_error_becomes_filenotfound(monkeypatch) -> None:
    def boom(_u: str) -> object:
        raise oras.OrasError("registry down")

    _mock_oras(monkeypatch, boom)
    with pytest.raises(FileNotFoundError):
        flash.probe_image_url("oras://reg.example/img:tag")


def test_curl_args_for_oras_injects_bearer_and_returns_digest(monkeypatch) -> None:
    _mock_oras(monkeypatch, lambda _u: _fake_resolved())
    argv, size, digest = flash._curl_args_for_source("oras://reg.example/img:tag")
    assert "-H" in argv
    assert any("Authorization: Bearer tok123" in a for a in argv)
    assert argv[-1].endswith("/blobs/sha256:" + "a" * 64)
    assert size == 1234
    assert digest == "sha256:" + "a" * 64
