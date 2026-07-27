# Concepts

A quick orientation to the nouns pixie uses. Each one has its own page
in the UI; this is how they relate.

## Catalog: the sources

A **catalog entry** is a *pointer* to an image, not the image itself: a
name plus a `src` URL (`oras://` for a ghcr artifact, `https://` for a
release asset) and a format (`img.gz`, `img.zst`, `tar.gz`). A fresh
deploy seeds a curated catalog; you can also **Import** a `catalog.toml`
URL or **Import full nosi catalog** for the whole upstream set. Nothing
is downloaded until you hit **Fetch** on a row.

Some entries are **disk images** (bindable — a machine can boot them);
others are **netboot bundles** (`vmlinuz` + `initrd`) that a disk image
points at via its `netboot_src` so pixie can netboot it over NBD. You
bind the disk image; pixie resolves the sibling bundle for you.

## Images: the materialised content

Once you Fetch a catalog entry, its bytes land on disk and it becomes an
**image**, identified by its `content_sha256` (the sha of the raw disk
image). Everything downstream keys off that sha — machine bindings,
NBD exports, and overlays — so the Images page is a group-by-sha rollup:
per image, its on-disk footprint, and every live use of it. (The old
`/ui/exports` page folded into here; NBD exports are a column now.)

## Machines + boot modes

A **machine** is a target, keyed by MAC. It appears the first time it
PXEs into pixie. You **bind** it to a **boot mode** + an image; on its
next PXE, pixie serves that plan. The modes are covered in detail on
[](boot-modes.md); in short: `ipxe-exit` (fall through to firmware),
`pixie-inventory` (post disk/NIC info, then exit), `pixie-tui` (operator
wizard on the target console), `pixie-flash-once` / `pixie-flash-always`
(write the image to a target disk), and `nbdboot` (stream the image over
NBD, root is overlay-on-tmpfs).

## Overlays: per-machine writable layers

`nbdboot` serves a read-only base image; each machine's writes land in an
**overlay** — a qcow2 keyed by a globally-unique **alias** (not a
per-machine file path), single-writer so two machines can't corrupt the
same overlay. Reset an overlay to discard its writes and boot the clean
base again. See [](boot-modes.md#overlays-are-volumes-not-per-machine-files).

## The live env

The `pixie-inventory` / `pixie-tui` / `pixie-flash-*` modes all boot the
same **live env** — the `pixie-live-env` disk image (the nosi
`arch-headless` base with the pixie CLI baked in), netbooted ephemerally
over NBD. Set it up once on the Live env page (one click fetches +
selects it); until then those modes render an `unavailable` plan. The
usbboot `.iso` (see [](usb-boot.md)) is the same live env on removable
media instead of over the network.

## How it fits together

```
Catalog entry  --Fetch-->  Image (content_sha)  --bind-->  Machine + boot mode
   (a source)                (bytes on disk)                  (served next PXE)
                                   |
                                   +-- nbdboot --> Overlay (per-machine writes)
```
