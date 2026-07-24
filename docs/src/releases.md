# Releases

Pixie releases on a `v*` git tag. The tag triggers the full CI gate,
and only if every job passes do the publish jobs run. There are three
publish targets plus a boot-media verification gate.

## What a tag publishes

Pushing `vX.Y.Z` (on `main`, after the version bump lands) publishes:

- **PyPI** - `pixie-lab X.Y.Z` (trusted publishing). Ships the two
  console scripts `pixie` + `pixie-lab`.
- **ghcr** - `ghcr.io/safl/pixie:X.Y.Z` and `:latest`. This is the
  image a `pixie-lab deploy` compose file pins, so it must exist for a
  fresh deploy to pull.
- **GitHub Release** - a release for the tag with the boot media
  attached: the `netboot-pc` live-env bake (`vmlinuz` + `initrd` +
  `squashfs`), `pixie-live-env-x86_64.tar.gz` (the tarball the in-app
  Fetch live-env pulls), the `usbboot-pc` `.iso`, `catalog.toml` (the
  curated catalog), and `.sha256` checksums.

## The Ventoy verify gate

Before any publish job runs, `verify-usbboot` proves the USB ISO is
actually bootable. It structurally checks the hybrid ISO (ISO 9660 +
isohybrid MBR + El Torito BIOS and UEFI boot entries + sha256), then
installs Ventoy onto a QEMU disk, drops the `.iso` on it, and boots it
under KVM - asserting the pixie live env comes up on the Ventoy
loop-boot path. Every publish job depends on this, so a broken,
non-bootable, or non-Ventoy-compatible ISO can never ship.

## Cutting a release

1. Land the work on `main`.
2. Open a `chore(release): pixie X.Y.Z` PR that bumps `version` in
   `pyproject.toml` and rolls `CHANGELOG.md`'s `[Unreleased]` into
   `[X.Y.Z]`. Merge it.
3. Tag and push:

```
git tag -a vX.Y.Z origin/main -m "pixie X.Y.Z"
git push origin vX.Y.Z
```

The tag run does the full gate + the Ventoy verify, then publishes.
Verify afterwards with `gh release view vX.Y.Z --json assets` and a
PyPI/ghcr check.
