# Concepts: sources, images, overlays

Pixie separates four things an operator tends to conflate. Getting the
split clear makes the UI (and disk usage) obvious.

```
Catalog source        Image (materialised)          NBD serving
(a URL you fetch)  ->  (identity = content sha)  ->  ephemeral  (nbdkit, read-only + tmpfs)
                       forms: disk / rootfs / boot  persistent (qemu-nbd, an Overlay volume)
```

## Catalog = sources

A **catalog entry** is a *source*: a name, a `src` URL
(`oras://` or `https://`), a format, and (for a disk image) a
`netboot_src` pointing at its netboot bundle. It is a thing you *can*
fetch. Nothing is on disk until you hit Fetch. The Catalog page is
sources-only: add, import a `catalog.toml`, Fetch, delete the entry.

## Image = the materialised entity

The moment you fetch a disk-image source, its bytes land under a
content sha. That content **is** the image, and its identity is the
sha, not the catalog row. Two catalog entries with different names can
resolve to the same image.

An image has up to three derived forms, all shared across machines:

- **disk** - the whole raw disk (decompressed from the `img.gz`),
  `blobs/<sha>/blob`.
- **rootfs** - partition 1 extracted (`blobs/<sha>/rootfs.raw`), so
  NBD can hand out ext4 at offset 0.
- **boot** - `vmlinuz` + `initrd`, unpacked from the netboot bundle
  into `artifacts/<bundle-sha>/`. Needed for any netboot; never for a
  local flash.

The **Images** page (`/ui/images`) groups fetched content by sha and,
per image, rolls up its on-disk footprint and every live usage:
machines bound to it, its ephemeral NBD export, and its overlays. The
image detail hub (`/ui/images/<sha>`) lays those out and offers a
guarded delete.

## Overlay = a single-writer volume

An **overlay** is a named writable layer over one base image. It is
not owned by a machine. Its identity is a globally-unique `alias`, and
the base image is implied by the alias. On disk it is a flat
`overlays/<alias>.qcow2` with the image's blob as `backing_file`,
served by `qemu-nbd`.

An overlay is single-writer: at most one machine holds an alias at a
time. Attaching a machine to an alias another machine already holds is
refused ("held by `<mac>`; detach first"); qemu-nbd's qcow2 image-lock
is the backstop. Detach an alias and it can move to a different target
with its accumulated writes.

The **Overlays** page classifies each: `serving` (attached + a live
NBD port), `held` (attached, not serving), `free` (unattached, a
deliberate keep), `orphaned` (holder machine gone), `missing` (qcow2
gone).

## The refcount, GC, and orphans

An image's usage count - machines bound + running exports + overlays -
is a refcount. Delete is allowed only when it reaches zero, and it
frees the whole `blobs/<sha>/` dir (disk *and* rootfs). The Images
page also surfaces **orphan blobs**: sha-named dirs on disk with no
catalog entry at all, left when an entry was deleted or re-pointed.
That is where un-GC'd disk pressure hides, and it is where you reclaim
it.

## How a boot consumes all this

- **flash** - the live env writes the image's `disk` to the target's
  own disk. Boots the local kernel afterwards. No boot form needed.
- **nbdboot / ephemeral** - kernel + initrd (the boot form) over
  HTTP, root over NBD read-only with a tmpfs overlay. Writes vanish.
- **nbdboot / persistent** - same, but the writable layer is an
  Overlay volume, so writes survive.

See [](boot-modes.md) for the per-mode detail.
