# Changelog

Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The
format captures what actually matters to an operator running pixie (the
`pixie-lab` PyPI package + `pixie` container): behaviour the operator
perceives, defaults that survived a `pip install -U`, and gates that
landed in CI.

Per-release commit history lives in `git log`; this file is the
operator-facing summary. Nothing between the 0.1.0 skeleton and the
next real release has been tagged: the intermediate work all lands
under `[Unreleased]` until an operator can drive the full flow end-
to-end on real hardware.

## [Unreleased]

### Added

**Operator TUI ported from bty wholesale.** The Rich-based five-stage
wizard (source pick, catalog pick, image pick, disk pick, flash) that
was the successful part of bty ships on pixie under the `pixie`
console-script. Same UX, same in-live-env behaviour, same server-driven
mode (`pixie --mac X` fetches `/pxe/<mac>/plan` and dispatches). No
Textual, no event loop, no alt-screen; Rich Panels + `Prompt.ask` per
screen. Namespace-sed'd from bty (`bty` -> `pixie`, `BTY_` -> `PIXIE_`,
`bty-server` -> `pixie`, `bty-lab` -> `pixie-lab`). `rich>=13` is now
a hard runtime dependency; no `[tui]` extra dance.

**Inventory server surface.** The live env's TUI POSTs an lshw + lsblk
blob after PXE-done; pixie stores it on the machine row so operators
can inspect discovered hardware from the UI. `machines` gained
`inventory_json` + `inventory_at` columns via an idempotent additive
migration. `POST /pxe/{mac}/inventory` accepts a JSON object body
(`{"disks": [...], "lshw": ...}`), upserts the row on first contact,
and emits a `machine.inventory.updated` event with `disks_count` +
`has_lshw` details. `GET /machines/{mac}/inventory` returns the blob
or 404.

**Events log.** Every write path in pixie emits a row into the shared
`state.db`'s `events` table. Operators grep the timeline from the
operator UI (`/ui/events`) or the JSON API (`GET /events`). Emit sites
include `catalog.entry.added`, `catalog.entry.deleted`, `catalog.fetch.
started`, `catalog.fetch.done`, `catalog.fetch.failed`, `machine.bound`,
`machine.deleted`, `machine.inventory.updated`, `export.registered`,
`export.deleted`. `GET /events` is an open read; the events carry only
already-visible fields, no secrets.

**Operator UI: exports + machines pages.** The dashboard is no longer
catalog-only; a nav strip at the top surfaces four tabs (Catalog /
Exports / Machines / Events) and a sign-out button. `/ui/exports`
tables the registered NBD exports with content sha, port, status
pill, and per-row Delete. `/ui/machines` tables every MAC pixie has
seen or bound, plus a form for binding a MAC to a boot mode +
optional image content sha.

**TFTP subprocess supervision.** The FastAPI lifespan manages an
`in.tftpd` (from `tftpd-hpa`) that serves iPXE NBPs so a target's
BIOS-PXE / UEFI-PXE first hop can chain into pixie's HTTP bootstrap
without an external TFTP daemon on the LAN. The Containerfile
installs the `ipxe` package and copies `undionly.kpxe` (BIOS),
`ipxe.efi` (UEFI), and `snponly.efi` (SNP-only UEFI) into
`/usr/share/pixie/tftp/`. `PIXIE_TFTP_ENABLED=1` is set in the image
env so a fresh compose bring-up serves TFTP by default. Non-root
callers must set `PIXIE_TFTP_PORT` to a non-privileged port.

**`pixie-lab` deploy generator.** `pixie-lab init [dest]` writes
`compose.yml` (one service on `--network=host`), `envvars.example`,
`README.md`, and a `data/` scaffold. `pixie-lab deploy [dest]` builds
on init: auto-detects the LAN address, generates a random admin
password (unless `--admin-password` is passed), realises `envvars`,
runs `podman compose up -d`, and waits for `/healthz`. `pixie-lab
purge [dest]` runs `podman compose down`; add `--wipe-data` to drop
the state volume too. Compose runner detection prefers
`podman-compose`, falls back to `podman compose`, then `docker
compose`. Deliberately shallower than bty-lab (one container, no
Quadlet emission, no upgrade flow yet).

**Machines + PXE plan renderer (image-native ramboot MVP).** An
operator can bind a MAC to a fetched catalog entry and target that
machine boots into that image with its own kernel and root over NBD.
MAC normalisation accepts `aa:bb:...`, `AA-BB-...` and `AABBCCDDEEFF`.
Closed set of boot modes: `ipxe-exit` (default) and `ramboot`.
`GET /pxe-bootstrap.ipxe` chain-loads `/pxe/${net0/mac}`; served
over HTTP (and by TFTP for BIOS-PXE clients). `GET /pxe/<mac>`
performs discovery upsert + per-machine plan. Ramboot plan walks
`catalog[image_sha] -> netboot_src -> catalog[bundle]` by URL cross-
reference, verifies the bundle's `manifest.json` is unpacked, ensures
an NBD export against the disk-image blob is spawned, and renders
the ramboot iPXE plan with content-addressed artifact URLs +
`pixie.nbd=tcp://${nbd-host}:${nbd-port}`. Missing / corrupt / not-
yet-fetched bundle emits `unavailable.j2` with the reason baked in
and `exit`. No fallback to a bty-media-baked kernel; a mismatched
image-vs-modules boot is a worse operator experience than a clean
`exit`. Binding a machine to `ramboot` triggers an idempotent spawn
of an NBD export named `pixie-<sha[:12]>.img` for the disk-image
blob. Env knobs: `PIXIE_PUBLIC_HOST` and `PIXIE_NBD_PUBLIC_HOST`.

**Exports + NBD supervisor.** Hard-forked from nbdmux 0.9.2's
`NbdServer` and adapted to pixie's content-addressed model.
`POST /exports {name, content_sha256}` spawns nbdkit for that export
against `<state_dir>/blobs/<sha>/blob`. Ports allocated from a base +
scan (`10809+` by default) and persisted on the export row.
`GET /exports` + `GET /exports/{name}` are open reads: live view of
each export's port + status. `DELETE /exports/{name}` (session auth)
kills the subprocess and removes the row. Filter chain per export:
`--filter=cow` always; `--filter=partition` when the blob has an
MBR/GPT sig. cow gives ramboot targets a writable overlay without
mutating the shared backing blob. Requires `nbdkit >= 1.44` on the
runtime path; the base container image pins `ubuntu:26.04` for this.
Env knobs: `PIXIE_NBD_PORT_BASE`, `PIXIE_NBD_BIND`, `PIXIE_NBDKIT_BIN`.

**Catalog + fetch.** Operator-curated image library, forked from
withcache's `Store` + `oras.py` and nbdmux's tar.gz-unpack pipeline.
Add / list / delete catalog entries (`POST` / `GET` / `DELETE
/catalog/entries`, form actions under `/ui/catalog/`). One fetch verb
(`POST /catalog/entries/<name>/fetch`) downloads the entry's `src`,
streams sha256 into `<state_dir>/blobs/<sha>/` atomically, and for
`format=tar.gz` unpacks vmlinuz + initrd + manifest.json into
content-addressed `<state_dir>/artifacts/<sha>/`. Runs in a bounded
thread pool so concurrent fetches don't block the event loop.
Content-addressed serves: `GET /b/<content_sha256>/<name>` for blobs,
`GET /artifacts/<content_sha256>/{vmlinuz,initrd,manifest.json}` for
netboot bundles. Nosi-shape TOML round-trip: parse `catalog.toml`,
serialise back in the same schema, unknown fields survive round-trip
via `extra`. `netboot_src` (URL cross-reference) is pixie's canonical
way to advertise a matching netboot bundle from a disk-image entry.
No misses, no auto-fetch, no cache-through, no warmer as a lifecycle
stage: presence on disk IS readiness. Session-only auth on mutating
routes; no bearer surface (withcache + nbdmux each carried one).

### Tests

Unit + real-container-integration coverage across every surface
listed above. Integration tests build `pixie:integration-test` from
the Containerfile, start it with `--network=host` + a bind-mounted
state dir, and drive the JSON API over HTTP; NBD assertions read
`NBDMAGIC` off a raw socket, TFTP assertions run `curl tftp://` over
UDP against the container's real `in.tftpd`, inventory assertions
POST via the same helper the live-env TUI calls. Gated behind
`-m integration` so the fast unit loop stays fast; CI runs it as its
own job after building the container. Current baseline: 72 unit +
19 integration, all green locally.

## [0.1.0] - TBD

### Added

First release. Skeleton only: a FastAPI app with `/healthz`,
session-cookie login/logout, and a placeholder dashboard. No catalog,
no fetch, no exports, no PXE plan renderer, no TFTP, no TUI, no
deploy generator. Publishes to PyPI (`pixie-lab`) and ghcr.io
(`ghcr.io/safl/pixie`).

The intent of a 0.1.0 with nothing operator-usable in it is to lock
the package name, the container image name, the release pipeline, and
the shape of the FastAPI app before the port PRs land. See `PLAN.md`.
