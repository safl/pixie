# pixie plan

Pixie is a bare-metal netboot appliance: catalog + fetch + NBD + PXE + TFTP
+ operator TUI in one container. It consolidates into one process what
bty, nbdmux, and a hard-fork of withcache implement today as three
separate FastAPI services on the same lab appliance -- the goal being one
wire contract, one state.db, one admin surface, in exchange for the
compositional flexibility of running them independently.

bty, nbdmux, and withcache all continue as their own projects. Pixie
does not replace them, deprecate them, or plan for their archival; it
is a new sibling appliance that starts from a merged design.

This document is the source of truth for what pixie is, what has been
decided, what is open, and what the ordered work looks like. Rehashed
during the design phase; kept up to date as PRs land.

## Locked decisions

### Identity + shape

One repo `safl/pixie`. One Python package `pixie`. One container
`ghcr.io/safl/pixie`. Two CLIs: `pixie` (Rich-based operator TUI, same
shape and strengths as the bty-tui it replaces; runs inside the
pixie-media live env on target hardware for interactive flashing +
inventory) and `pixie-lab` (deploy generator, ports bty-lab). One FastAPI
app on one uvicorn.

The container runs with `--network=host` and serves udp/69 (TFTP), tcp/8080
(HTTP), plus an NBD port range (10809 + N, one per registered export).

### Auth

Session-cookie only. One admin password. The bearer-token surface that
withcache + nbdmux both carried today is dropped entirely; every write-
capable route requires a valid session.

### Fetch model

One verb: `fetch`. Downloads bytes from an ORAS or HTTPS src, streams
sha256 while writing, atomically renames into place; for `.tar.gz` netboot
bundles, additionally unpacks vmlinuz + initrd + manifest.json into a
content-addressed artifacts directory. No misses page, no auto-fetch, no
cache-through, no warmer as a lifecycle stage, no ready/pending dance.
Presence on disk is readiness.

### Catalog schema

Two peer `[[images]]` entries in TOML, as today's nosi ships. The
disk-image entry carries `netboot_src = "<url>"` pointing at the bundle
entry's `src` (URL cross-reference, not name-string). The netboot bundle
remains its own catalog entity, self-contained and fetchable standalone,
so a ramboot-only workflow with no matching disk image stays coherent.

The `netboot_ref` name-string field that today's nosi 2026.W28 catalog
still carries gets loose-parsed (accepted with a warning) so pixie reads
existing nosi tags out of the box; tight-emits `netboot_src` when
rewriting. Nosi's `gen_catalog.py` migrates on its own schedule.

### Artifact URLs

Content-addressed: `/artifacts/<sha256>/{vmlinuz,initrd,manifest.json}`,
where `<sha256>` is the tar.gz bundle blob's own sha (recorded during the
fetch). Immutable per content, cache-friendly, no character-escaping
fights, no rename fragility.

### Ramboot

One path only: image-native (image's own kernel + initrd from the
extracted netboot bundle). No fallback to a bty-media-baked kernel; that
was the source of userspace-vs-modules mismatch pain the trio design
carried.

When the ramboot bundle is missing, corrupt, or the tar.gz didn't unpack
cleanly at runtime, target lands on a pixie-media **diagnostic** boot: a
tiny live-env variant whose only job is to display "ramboot bundle for
image X is broken on this pixie; fetch it on the operator UI and reboot
the target" in large friendly text on tty1/serial. Baked from the same
media pipeline as the operator TUI live-env, no drift.

At the API layer, `PUT /machines/<mac> {ramboot, ref=X}` with X's bundle
not yet fetched returns 422 with a message pointing at the catalog page.
The diagnostic boot is the runtime safety net for the "bundle looked
fetched but was corrupt at boot time" case, not the normal bind path.

### Media inside the repo

`pixie/media/` (live-build configs, hooks, includes.chroot) +
`pixie/cijoe/` (build-time tooling only, no runtime surface) live in this
repo. One repo, one CI, one release cadence for both the container and
the live-env artifacts. Two live-env variants baked from one tree:
`pixie-netboot-pc` (the operator TUI live env) and `pixie-ramboot-diag`
(the diagnostic screen).

### Repo layout

```
pixie/
  src/pixie/
    catalog/    fetch + blob store (forked from withcache._store + oras)
    exports/    NBD export lifecycle
    nbd/        nbdkit subprocess supervisor (from nbdmux)
    artifacts/  content-addressed serve
    machines/   registry
    pxe/        plan renderer + iPXE templates
    tftp/       bootstrap (in-process, folded from bty-tftp)
    web/        FastAPI wiring + templates + static (base: bty)
    tui/        Rich-based operator TUI (from bty.tui)
    deploy/     pixie-lab deploy generator
  media/        live-build configs + hooks for TUI + ramboot-diag bakes
  cijoe/        build-time tooling (no runtime surface)
  deploy/       one Containerfile + one Quadlet + one compose service
  docs/
  tests/
```

### Roadmap

The four PR bands below all landed. Current focus is operator-UX polish
+ end-to-end hardware validation on the lab appliance (matx-bmc).

**PR 1 (shipped) -- skeleton.** pyproject on uv, ruff + mypy + pytest,
CI on Python 3.11-3.14 mirroring bty, one FastAPI app with `/healthz`,
session-cookie login, placeholder dashboard, one Containerfile staging
the runtime toolchain (nbdkit + plugins, tftpd-hpa, curl, tar, gzip,
zstd, xz, qemu-utils, ca-certificates). GPL-3.0-only. Publishes to PyPI
(`pixie-lab`) + ghcr (`ghcr.io/safl/pixie`) on tag.

**PR 2 (shipped) -- catalog + fetch.** Store class + ORAS client +
catalog state + blob-serve + download pipeline live under
`src/pixie/catalog/`. tar.gz netboot-bundle unpack pipeline folded in.
`netboot_src` parsing + `netboot_ref` loose-parse-with-warning. No
misses / auto-fetch / cache-through / warmer stages: presence on disk
IS readiness. Content-addressed `/artifacts/<sha>/{vmlinuz,initrd,
manifest.json}` + `/b/<sha>/<name>`.

**PR 3 (shipped) -- exports + PXE + TFTP + TUI + pixie-lab.** nbdkit
supervisor, export CRUD, machine registry, PXE plan renderer, iPXE
templates, in-process TFTP bootstrap, Rich TUI wholesale from bty.
`pixie-lab init/deploy/purge` mirrors bty-lab shape. Templates keep
their behaviour (console lines, modprobe blacklists, transparency
comments) so hardware lessons survive.

**PR 4 (shipped) -- live-env media.** `pixie-media/` + `cijoe/` bake
`pixie-netboot-pc` (operator TUI live-env) and `pixie-ramboot-diag`
(diagnostic screen) as GitHub release assets. Reference deploy stages
the netboot-pc bake to `<data-dir>/live-env/`; the PXE renderer
degrades to `unavailable.j2` when it is absent.

**Ongoing -- operator UX + hardware validation.** Settings pane
(timezone + strftime), machine record extensions (labels,
sanboot_drive, target_disk_serial), inventory-derived flash-target
picker, live-refresh on catalog + machines + dashboard, event log with
kind + subject_kind filters, delete confirmations on destructive
actions. End-to-end validation on matx-bmc pending target-side r8125
autoload in the live-boot initrd.

### Relationship to bty / nbdmux / withcache

bty, nbdmux, and withcache stay alive as their own projects with their
own release cadences. Pixie borrows patterns and, for its initial port,
lifts code from all three (withcache's Store class + oras client,
nbdmux's NbdServer + fetch mechanics, bty's operator UI + machine
registry + PXE renderer + TUI + iPXE templates + live-env media
recipes). Those lifts start as forks, not dependencies; each project
evolves separately afterwards.

No migration script from an existing bty state.db. Fresh pixie install;
operator re-adds catalog entries and re-fetches the images they want.

## Decisions locked during PR 1-4

License: GPL-3.0-only. Distribution: `pixie-lab` on PyPI with two
console-scripts (`pixie` + `pixie-lab`), matching bty-lab. Env-var
prefix `PIXIE_*` (e.g. `PIXIE_ADMIN_PASSWORD`). Data mount root
`/var/lib/pixie/` with `blobs/`, `artifacts/`, `live-env/`, `state.db`.
Admin password default in `pixie-lab`: `pixie`. Docs shape: slim
README-first, operator narrative lives on `/ui/settings` and inline
form help; PDF/HTML operator docs deferred until v1.0.

Nosi coordination: option (b) as originally planned. Pixie
loose-parses `netboot_ref` (accepted with a warning) and tight-emits
`netboot_src`; nosi migrates on its own schedule.

## Open

- **End-to-end validation on matx-bmc.** The netboot-pc initrd bakes
  r8125 + r8169 + igb + e1000e drivers (PR safl/pixie#32) but the
  target still stalls before Debian live-boot fetches its squashfs.
  Blocks memory notes [[project_pixie_transition]] +
  [[project_ramboot_architecture]] from graduating out of "in
  progress".
- **Backup / export / import for state.db.** Bty-web has this; pixie
  does not yet. A tarball of `state.db` + selective `blobs/` +
  `artifacts/` would let an operator migrate between hosts without
  re-fetching every image. Deferred until an operator has enough
  fetched to feel the migration pain.

## What lives elsewhere

The pre-port audit (fable-produced, `docs/audit.md`) is the concrete port
inventory: which files come across verbatim, which drop, what runtime
deps the Dockerfile has to carry, and a section of items flagged for
operator decision. Read it alongside this plan; the two are consistent.

Memory notes on the trio's history (bty motivation, withcache-owns-catalog
milestone, ramboot architecture, netboot UEFI milestone, ...) live in the
per-project memory tree, not in this repo. They inform pixie's design
but aren't authoritative for it.
