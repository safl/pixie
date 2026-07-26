"""
Build + publish the pixie-live-env disk image
=============================================

Produces the ``pixie-live-env`` image that pixie nbdboots ephemerally
for the live-env boot modes (``pixie-flash-once`` / ``-always`` /
``pixie-inventory`` / ``pixie-tui``). The renderer selects this image
via the ``PIXIE_LIVE_ENV_IMAGE_SHA`` setting; the value an operator
sets there is the ``content_sha256`` this build computes and publishes
(see step 5 below and the ``.content-sha256`` sidecar).

Pipeline:

1. ``uv build --wheel`` the current pixie-lab, then assemble a vendored
   CLI tree: ``pip install --target`` the pixie package (``--no-deps``)
   plus only the pure-Python CLI runtime deps (``rich`` + ``tomlkit``).
   The CLI reaches the server over stdlib ``urllib`` -- NOT httpx -- so
   the heavy fastapi / uvicorn / pydantic base deps are deliberately
   left out. The image's stock ``python3`` runs this tree.

2. Pull the nosi ``arch-headless`` base disk image via ORAS, reusing
   pixie's own resolver (``python -m pixie.oras_pull``) so the bytes +
   digest match a live catalog Fetch. Decompress the ``img.gz`` to a
   raw disk image.

3. Run ``live_env_transform.sh`` (privileged: qemu-nbd + mount) to
   inject the vendored CLI + the ``pixie-on-tty1`` service trio and
   slim conservatively, emitting a raw disk image.

4. sha256 the RAW image -> ``content_sha256`` (== PIXIE_LIVE_ENV_IMAGE_SHA);
   gzip it to ``pixie-live-env-x86_64.img.gz`` (the release asset);
   assert the gzipped asset is < 2 GiB (GitHub's per-file release
   limit) and fail otherwise.

5. Emit alongside the asset: its ``.sha256`` checksum, a
   ``.content-sha256`` sidecar carrying the raw-image sha (the value
   for ``PIXIE_LIVE_ENV_IMAGE_SHA``), and ``pixie-live-env-catalog.toml``
   -- a catalog fragment (the bindable disk image with its
   ``netboot_src`` + the arch-headless netboot bundle it points at) the
   release job merges into the shipped ``catalog.toml`` so the image is
   fetchable via the Catalog tab like any other.

The cwd at run time is ``cijoe/`` (the Makefile cd's there), so the
repo root is ``Path.cwd().parent``. Requires: passwordless sudo,
qemu-utils (qemu-nbd + qemu-img), the ``nbd`` module, rsync, gzip, and
``uv`` on PATH. Skipped for any variant other than ``live-env-image``.

Retargetable: False
"""

from __future__ import annotations

import errno
import logging as log
import shutil
from argparse import ArgumentParser
from pathlib import Path

# Reuse the pyproject version reader so the published asset + catalog
# fragment carry the same stamped version as the other bakes.
from usb_iso_build import _read_pixie_version

# GitHub's per-file release-asset hard limit. The gzipped image must
# land under this or the release upload is rejected downstream, so we
# gate here (loud, early) instead.
RELEASE_ASSET_LIMIT = 2 * 1024 * 1024 * 1024

# Stable, unversioned asset basename so the catalog + settings default
# can reference ``.../releases/latest/download/<name>`` at a fixed URL
# (mirrors how the live-env tarball + catalog.toml are published).
ASSET_BASENAME = "pixie-live-env-x86_64.img.gz"
CATALOG_FRAGMENT_BASENAME = "pixie-live-env-catalog.toml"
CONTENT_SHA_BASENAME = "pixie-live-env-x86_64.content-sha256"

# Defaults; overridable via the ``[live-env-image]`` config table.
DEFAULT_BASE_SRC = "oras://ghcr.io/safl/nosi/arch-headless:latest"
DEFAULT_NETBOOT_SRC = "oras://ghcr.io/safl/nosi/arch-headless-netboot:latest"
DEFAULT_CATALOG_SRC = (
    "https://github.com/safl/pixie/releases/latest/download/pixie-live-env-x86_64.img.gz"
)
DEFAULT_IMAGE_NAME = "pixie-live-env"


def add_args(parser: ArgumentParser):
    del parser  # no flags; signature kept for cijoe consistency


def _sha256_of(cijoe, path: Path) -> str | None:
    err, state = cijoe.run_local(f"sha256sum {path}")
    if err:
        return None
    out = state.output() if hasattr(state, "output") else str(state)
    fields = out.strip().split()
    return fields[0] if fields else None


def _build_vendored_cli(cijoe, repo_root: Path, build_dir: Path) -> Path | None:
    """Build the wheel and assemble the vendored CLI tree. Returns the
    vendor dir on success, ``None`` on failure."""
    wheel_out = build_dir / "wheel"
    if wheel_out.exists():
        shutil.rmtree(wheel_out)
    wheel_out.mkdir(parents=True)

    err, _ = cijoe.run_local(f"sh -c 'cd {repo_root} && uv build --wheel --out-dir {wheel_out}'")
    if err:
        log.error("uv build --wheel failed")
        return None
    wheels = sorted(wheel_out.glob("pixie_lab-*-py3-none-any.whl"))
    if not wheels:
        log.error(f"no pixie_lab wheel produced in {wheel_out}")
        return None
    wheel = wheels[-1]

    vendor = build_dir / "vendor"
    if vendor.exists():
        shutil.rmtree(vendor)
    vendor.mkdir(parents=True)

    # The pixie package only (no deps -- installing the full wheel would
    # drag fastapi / uvicorn / httpx / pydantic, which are compiled,
    # version-specific, and unused by the CLI).
    err, _ = cijoe.run_local(f"uv pip install --target {vendor} --no-compile --no-deps {wheel}")
    if err:
        log.error("uv pip install (pixie package, --no-deps) failed")
        return None
    # The pure-Python CLI runtime deps. rich powers the wizard; tomlkit
    # backs the catalog round-trip the CLI can reach. Both ship as
    # ``py3-none-any`` wheels, so they import under the image's own
    # python3 regardless of its minor version.
    err, _ = cijoe.run_local(f"uv pip install --target {vendor} --no-compile rich tomlkit")
    if err:
        log.error("uv pip install (rich + tomlkit) failed")
        return None
    return vendor


def main(args, cijoe):
    del args
    cijoe_dir = Path.cwd()
    repo_root = cijoe_dir.parent
    pixie_media = repo_root / "pixie-media"

    variant = cijoe.getconf("pixie", {}).get("variant", "")
    if variant != "live-env-image":
        log.info(f"Skipping live_env_image (variant={variant!r}; only 'live-env-image' runs)")
        return 0

    conf = cijoe.getconf("live-env-image", {})
    base_src = conf.get("base_src", DEFAULT_BASE_SRC)
    netboot_src = conf.get("netboot_src", DEFAULT_NETBOOT_SRC)
    catalog_src = conf.get("catalog_src", DEFAULT_CATALOG_SRC)
    image_name = conf.get("image_name", DEFAULT_IMAGE_NAME)
    publish_dir_str = conf.get("publish", {}).get("dir")
    if not publish_dir_str:
        log.error("live-env-image.publish.dir is unset in the config")
        return errno.EINVAL
    publish_dir = Path(publish_dir_str)
    publish_dir.mkdir(parents=True, exist_ok=True)

    if not pixie_media.exists():
        log.error(f"pixie-media tree missing: {pixie_media}")
        return errno.ENOENT

    build_dir = cijoe_dir / "_build" / "live-env-image"
    if build_dir.exists():
        # The transform's scratch is torn down internally, but the raw
        # base image is owned by the invoking user; a plain rm is fine.
        err, _ = cijoe.run_local(f"sudo rm -rf {build_dir}")
        if err:
            log.error(f"failed to remove stale build dir {build_dir}")
            return err
    build_dir.mkdir(parents=True)

    pixie_version = _read_pixie_version(cijoe_dir)
    log.info(f"Building {image_name} v{pixie_version} from base {base_src}")

    # 1. Vendored CLI tree.
    vendor = _build_vendored_cli(cijoe, repo_root, build_dir)
    if vendor is None:
        return errno.EIO

    # 2. Pull + decompress the base image, reusing pixie's ORAS resolver.
    base_gz = build_dir / "base.img.gz"
    err, _ = cijoe.run_local(
        f"sh -c 'cd {repo_root} && uv run python -m pixie.oras_pull {base_src} {base_gz}'"
    )
    if err:
        log.error(f"failed to pull base image {base_src}")
        return err
    base_raw = build_dir / "base.img"
    err, _ = cijoe.run_local(f"sh -c 'gzip -dc {base_gz} > {base_raw}'")
    if err:
        log.error("failed to decompress the base image")
        return err

    # 3. Transform: inject CLI + service trio, slim, fstrim.
    out_raw = build_dir / "pixie-live-env.img"
    transform = cijoe_dir / "scripts" / "live_env_transform.sh"
    err, _ = cijoe.run_local(
        f"sudo bash {transform} "
        f"--base {base_raw} --out {out_raw} --vendor {vendor} "
        f"--media {pixie_media} --force"
    )
    if err:
        log.error("live_env_transform.sh failed")
        return err
    # The transform chown's the output back to us, but be defensive.
    cijoe.run_local(f"sudo chown $(id -u):$(id -g) {out_raw}")

    # 4. Content-address on the RAW image (matches pixie's img.gz fetch:
    #    decompress -> sha of the raw disk image), then gzip the asset.
    content_sha = _sha256_of(cijoe, out_raw)
    if not content_sha:
        log.error(f"failed computing content sha256 of {out_raw}")
        return errno.EIO
    log.info(f"content_sha256 (PIXIE_LIVE_ENV_IMAGE_SHA) = {content_sha}")

    asset_path = publish_dir / ASSET_BASENAME
    err, _ = cijoe.run_local(f"sh -c 'gzip -c {out_raw} > {asset_path}'")
    if err:
        log.error("failed gzipping the live-env image asset")
        return err

    asset_bytes = asset_path.stat().st_size
    log.info(f"asset {asset_path.name}: {asset_bytes} bytes ({asset_bytes / 2**30:.2f} GiB)")
    if asset_bytes >= RELEASE_ASSET_LIMIT:
        log.error(
            f"{asset_path.name} is {asset_bytes / 2**30:.2f} GiB, "
            f">= the 2 GiB GitHub release-asset limit; tighten the transform slim"
        )
        return errno.EFBIG

    # 5. Sidecars + catalog fragment.
    asset_sha = _sha256_of(cijoe, asset_path)
    if not asset_sha:
        log.error("failed computing sha256 of the gzipped asset")
        return errno.EIO
    (publish_dir / f"{ASSET_BASENAME}.sha256").write_text(
        f"{asset_sha}  {ASSET_BASENAME}\n", encoding="utf-8"
    )
    (publish_dir / CONTENT_SHA_BASENAME).write_text(f"{content_sha}\n", encoding="utf-8")

    fragment = _catalog_fragment(
        image_name=image_name,
        catalog_src=catalog_src,
        netboot_src=netboot_src,
        content_sha=content_sha,
        version=pixie_version,
    )
    (publish_dir / CATALOG_FRAGMENT_BASENAME).write_text(fragment, encoding="utf-8")

    cijoe.run_local(f"ls -la {publish_dir}")
    log.info(
        f"published {ASSET_BASENAME} (+ .sha256, {CONTENT_SHA_BASENAME}, "
        f"{CATALOG_FRAGMENT_BASENAME}) to {publish_dir}"
    )
    return 0


def _catalog_fragment(
    *,
    image_name: str,
    catalog_src: str,
    netboot_src: str,
    content_sha: str,
    version: str,
) -> str:
    """Render the catalog fragment (no ``version =`` header; the release
    job appends these ``[[images]]`` blocks to the shipped catalog).

    Two peer entries: the bindable disk image (with a filled-in
    ``content_sha256`` == PIXIE_LIVE_ENV_IMAGE_SHA, and a
    ``netboot_src`` URL cross-reference) and the arch-headless netboot
    bundle it points at, so the nbdboot renderer resolves the pair.
    """
    disk_desc = (
        "pixie live-env (nosi arch-headless + the pixie CLI + pixie-on-tty1 service). "
        "nbdbooted ephemerally for the live-env boot modes."
    )
    bundle_desc = (
        "Netboot bundle for arch-headless: vmlinuz + initrd so pixie can nbdboot the "
        "pixie-live-env image over NBD."
    )
    return f"""\
# pixie-live-env {version} -- the image the live-env boot modes
# (pixie-flash-once / -always / pixie-inventory / pixie-tui) nbdboot
# ephemerally. Set PIXIE_LIVE_ENV_IMAGE_SHA to the content_sha256 below
# to select it (that sha is the sha256 of the raw disk image, which is
# exactly what pixie content-addresses this img.gz to after fetching).

[[images]]
name = "{image_name}"
src = "{catalog_src}"
format = "img.gz"
arch = "x86_64"
description = "{disk_desc}"
netboot_src = "{netboot_src}"
content_sha256 = "{content_sha}"

[[images]]
name = "nosi arch-headless netboot bundle"
src = "{netboot_src}"
format = "tar.gz"
arch = "x86_64"
description = "{bundle_desc}"
"""
