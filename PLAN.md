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

**PR 1 (v0.1.0) -- skeleton.** MIT-or-GPL license (see open), pyproject
using uv, ruff + mypy + pytest wired, one CI workflow (lint + typecheck +
pytest on Python 3.11-3.14, matching bty), one FastAPI app with `/healthz`
+ session-cookie login/logout + a placeholder dashboard, one Containerfile
staging the runtime toolchain (nbdkit + plugins, tftpd-hpa, curl, tar,
gzip, zstd, xz, qemu-utils, ca-certificates), .gitignore, .dockerignore,
README. No feature code beyond healthz + login. Publishes to PyPI + ghcr
on tag.

**PR 2 (v0.2.0) -- catalog + fetch.** Ports withcache's Store class, ORAS
client, catalog state, blob-serve, and download pipeline into
`src/pixie/catalog/`. Ports nbdmux's `_fetch_and_decompress` +
`_fetch_netboot_bundle` methods as the tar.gz-unpack half of `fetch`.
Drops the misses / auto-fetch / cache-through surface per the audit. Adds
`netboot_src` parsing + `netboot_ref` loose-parse-with-warning. Emits
content-addressed `/artifacts/<sha256>/` on unpack. UI: catalog page with
Fetch + Delete + Redownload buttons; no misses page, no cache-through
prose.

**PR 3 (v0.3.0) -- exports + PXE + TFTP + TUI + pixie-lab.** Ports the
nbdkit supervisor, export CRUD, machine registry, PXE plan renderer, iPXE
templates, TFTP bootstrap, and Rich TUI. `pixie-lab` init/deploy/purge
mirroring bty-lab. Templates keep their existing behaviour (console lines,
modprobe blacklists, transparency comments) since those encode hardware
lessons.

**PR 4 (v0.3.x) -- live-env media.** `pixie/media/` + `pixie/cijoe/`,
bakes pixie-netboot-pc + pixie-ramboot-diag as GitHub release assets.
Split from PR 3 because the media bake is a large, independently-testable
diff (~5-10k LOC of live-build config) that can land after the container
side is proven.

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

## Open decisions

Small calls I would otherwise silently make in PR 1. Answer any that
you'd rather steer.

- **License**. bty ships GPL-3.0-only. Pixie is also GPL-3.0-only unless
  you'd rather MIT / Apache-2.0.
- **PyPI package name.** `pixie` may be taken; falling back to `pixie-lab`
  as the distribution + two console-scripts (`pixie` + `pixie-lab`)
  matches how bty-lab / bty are shaped today.
- **Config file.** `pixie.toml`, env-var prefix `PIXIE_*` (e.g.
  `PIXIE_ADMIN_PASSWORD`).
- **Admin password default in `pixie-lab`.** `pixie-lab` (mirrors bty-lab's
  `bty-lab` default).
- **Data mount root.** `/var/lib/pixie/` with subdirs `blobs/`,
  `artifacts/`, `images/`, `state.db`, `session-secret`.
- **Docs shape.** Slim README-first for v0.1-v0.3, add operator docs (PDF
  + HTML via CI) later? Or match bty's docs tree from day one?

Higher-impact calls to answer explicitly:

- **Nosi coordination.** Option (b) is the current plan: pixie loose-parses
  `netboot_ref`, tight-emits `netboot_src`, nosi migrates when convenient.
  Alternatives are (a) coordinated nosi release before pixie v0.2 or (c)
  nosi ships a schema v2 with `netboot_src` alongside a v1 with
  `netboot_ref`.

## What lives elsewhere

The pre-port audit (fable-produced, `docs/audit.md`) is the concrete port
inventory: which files come across verbatim, which drop, what runtime
deps the Dockerfile has to carry, and a section of items flagged for
operator decision. Read it alongside this plan; the two are consistent.

Memory notes on the trio's history (bty motivation, withcache-owns-catalog
milestone, ramboot architecture, netboot UEFI milestone, ...) live in the
per-project memory tree, not in this repo. They inform pixie's design
but aren't authoritative for it.
