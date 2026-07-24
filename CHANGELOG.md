# Changelog

Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The
format captures what actually matters to an operator running pixie (the
`pixie-lab` PyPI package + `pixie` container): behaviour the operator
perceives, defaults that survived a `pip install -U`, and gates that
landed in CI.

Per-release commit history lives in `git log`; this file is the
operator-facing summary.

## [Unreleased]

## [0.3.1] - 2026-07-24

### Fixed

**The usbboot `.iso` is attached to the GitHub Release.** v0.3.0's
release carried only the usbboot `.iso.sha256`, not the image itself:
the bake publishes an uncompressed `pixie-usbboot-pc-x86_64-v<version>.iso`
but the CI upload glob still matched `.iso.gz`, so only the checksum
sidecar reached the release. The glob now matches `.iso`, so the
bootable USB / ISO media auto-attaches like every other release asset.

## [0.3.0] - 2026-07-23

Operator-facing surface + release plumbing. A fleet overlay-management
page, a tightened inventory view, and a dedicated live-env pane; the
release now ships the container image + boot media + a curated catalog,
and pixie can fetch its own live-env.

### Added

**Fleet-wide overlay management page.** A new **Overlays** page
(`/ui/overlays`) lists every persistent nbdboot qcow2 across the fleet
with disk-used, last-modified, serving port, and a state
(active / idle / orphaned / missing); per-row Reset and a Prune that
reclaims only the orphaned + missing ones. The machine-detail Inventory
view was tightened from stacked stat-cards into one dense summary.

**Pixie ships a curated catalog and defaults to it, not nosi's.** A
fresh (empty) catalog is seeded on first start from a `catalog.toml`
bundled in the package: a strict subset of the upstream nosi catalog
restricted to the four netboot-capable images pixie's nbdboot + live-env
chains actually test and support (debian-13-headless, ubuntu-2404 /
2604-headless, fedora-44-headless, each with its netboot bundle). The
desktop / proxmox / rpios / freebsd variants that have no netboot bundle
are omitted. The "Import catalog" field and the live-env TUI now default
to the pixie release copy of this curated catalog rather than the full
nosi catalog; both remain overridable by URL. Seeding is one-shot,
never clobbers an operator-populated catalog, and is disabled with
`PIXIE_SEED_CATALOG=0`.

**Pixie can fetch its own live-env, from a dedicated Live-env pane.** A
new **Live env** page (`/ui/live-env`) is the one place the live env is
managed: staged-media readiness, a **Fetch live env** action, the fetch
source, and the extra-kernel-cmdline override. Fetch downloads the
netboot-pc bake as a single tarball (`PIXIE_LIVE_ENV_SRC`, defaulting to
the latest GitHub release's `pixie-live-env-x86_64.tar.gz`) and stages
`vmlinuz` + `initrd` + `live.squashfs` under `PIXIE_LIVE_ENV_DIR`,
reusing the catalog fetch's curl transport. This replaces the only
artifact an operator previously had to bake locally
(`make build VARIANT=netboot-pc`) or hand-copy. The source is overridable
per deploy (point at a mirror for air-gapped installs). The dashboard
Live-env card is now status-only and links to the pane; the live-env
knobs moved off the Settings page. The `publish-release` job assembles
that tarball so the default source resolves.

**Releases now ship the container image and the boot media, not just
the PyPI package.** Tagging `v*` publishes the appliance image to
`ghcr.io/safl/pixie` (`:<version>` + `:latest`) and creates a GitHub
Release with the boot media attached: the netboot-pc live-env bake
(`vmlinuz` + `initrd` + `squashfs`) that the `pixie-flash-once` /
`pixie-flash-always` / `pixie-inventory` / `pixie-tui` modes chain
into, and the `usbboot-pc` bootable `.iso.gz`, each with a `.sha256`.
Previously a tag published only to PyPI, so the ghcr image that
`pixie-lab deploy` pins and the live-env media both existed only as
ephemeral CI artifacts or a local `make build` -- a fresh
`pixie-lab deploy` pulled an image that was never pushed.

## [0.2.0] - 2026-07-22

First real release after the 0.1.0 skeleton: nbdboot (ephemeral +
persistent per-machine overlays) validated end-to-end on real
hardware, plus a bty-lab-shaped deploy CLI and an operator-managed
event log.

### Added

**Event-log Acknowledge + Clear actions.** The `/ui/events` page grows
two bulk actions: **Acknowledge** advances the ack cursor so the
dashboard's unacknowledged-error count zeros without touching the log,
and **Clear** wipes the whole log (behind a confirm) and drops one
`events.cleared` marker so the reset itself stays on the record.
Mirrors the ack/clear affordances bty's event log carried.

**`pixie-lab purge` reworked to match `bty-lab`.** `purge` now prints
a plan and gates the destructive parts behind a `y/N` confirmation
(`-y`/`--yes` to skip; a non-TTY refuses without `--yes`). Flags:
`--data` deletes the on-disk state (previously `--wipe-data`, which
never actually removed the bind-mounted `data/`), `--images` removes
the container image, and `--all` also removes the deploy directory
(implies `--data`).

**Per-machine persistent qcow2 overlays under `nbdboot`.** A new
`overlay_profile` field on the machine binding flips one target from
the default ephemeral-tmpfs behaviour to a per-machine writable
overlay without changing anything else about the bind. A non-blank
profile maps to a `data/overlays/<mac>/<image_sha>/<profile>.qcow2`
file with the image's base blob as `backing_file`, served over NBD by
`qemu-nbd`; the target mounts it read-write and system changes (apt
installs, kernel modules, hardware-specific config) survive reboots.
Overlays are keyed by `(mac, image_sha, profile)`, so different
machines have fully independent files under the same profile name and
rebinding to a different image leaves the old image's overlays on
disk for a later resume. A Reset button on the machine detail page
tears down `qemu-nbd`, unlinks the qcow2, and lets the next boot
lazy-create a fresh overlay from the base. New `overlays` table on
state.db (idempotent additive migration), new `overlay.created`,
`overlay.reset`, `overlay.booted` events. Concurrency is by
construction (a MAC boots one target at a time), so there is no
holder tracking or force-reclaim.

**Slick Inventory card viz.** The machine detail Inventory card was
rewritten to consume a normalised view of the stored `lshw -json`
blob. CPU renders one stat-block per socket with the model as
headline, an architecture badge, and Bootstrap `display-6` big-type
numbers for cores over threads plus max clock. Memory shows a total
headline plus a per-DIMM slot-fill row (filled blocks for populated
SMBIOS type-17 bank records, outlined blocks for empty slots,
hover-title tooltip per slot showing size / speed / type), with a
total-only fallback for firmwares that skimp on bank records. The
extractor lives in `pixie.web._inventory.normalise_inventory` and
runs at render time, so a wire-format change or a new lshw quirk
touches one function. Two new Jinja filters: `humanize_bytes` and
`humanize_hz`.

### Fixed

**Truncated `.img.gz` fetches now fail at the download stage.**
Operators saw "decompress img.gz failed: Compressed file ended before
the end-of-stream marker was reached" on the catalog page when a
ghcr download was interrupted mid-transfer. Root cause: the fetch
pipeline's byte-copy loop treated `resp.read()` returning an empty
chunk as "done" but urllib does not raise when a peer closes the
connection early, so a short body was accepted and the gzip trailer
check surfaced the truncation several minutes downstream of the
actual failure. `_stream_to_tmpfile` now cross-checks bytes-written
against `Content-Length` and raises a clear `download truncated for
<url>: got X of Y bytes` `FetchError` at the point of cause. A
related cleanup leak (the `finally` block only unlinked `.inflight`
files for `tar.gz`, so failing `img.gz` fetches left multi-GB
orphans in `data/tmp/` forever) is fixed as part of the same change.

**Settings pane with per-operator display picks.** New top-nav
pill `/ui/settings` with two knobs: display timezone (IANA zone
name) and datetime format (strftime pattern). Both resolve override
-> env (`PIXIE_DISPLAY_TZ`, `PIXIE_DATETIME_FORMAT`) -> built-in
default (UTC + `%Y-%m-%d %H:%M:%S %Z`). A `settings` table lives on
state.db via an idempotent additive migration. Every visible
timestamp cell across dashboard, events, machines, machine-detail,
catalog, and catalog-detail is threaded through a new `fmt_ts`
Jinja filter, so a Settings change flips the whole UI in one place
without a data step.

**Live status pill for fetch phases.** The catalog page's status
column now ticks through `downloading` (with `bytes / total` when
Content-Length is present) -> `decompressing` -> `unpacking` while
a Fetch is in flight, without a full page reload. Powered by a new
`ProgressReporter` callback on `catalog._fetcher.fetch()`,
`GET /ui/fetch-states.json` for the JSON echo, and a tiny in-page
poller that starts on server-render if any row is in flight and
stops when nothing is anymore.

**Live refresh across machines list + detail + dashboard.**
`GET /ui/machines-live.json` echoes the operator-visible per-machine
fields keyed by MAC; the machines table + detail page poll it every
5 s and rewrite cells in place. `GET /ui/dashboard-live.json` echoes
the same stat block the dashboard cards render, and
`GET /ui/events-live.json` the last N events with pre-formatted
timestamps. Dashboard counters + recent-events feed refresh from
those without a page reload.

**Machine record extensions (labels, sanboot_drive,
target_disk_serial).** Three additive columns on the machines table
so an operator can tag a row for grouping / search, calibrate iPXE's
BIOS drive slug (`0x80`, `0x81`, ...) for ipxe-exit, and pick a
target disk serial from the reported inventory for the
pixie-flash-* modes. Parsed via a shared validator so the JSON PUT
and the form POST reject the same set. Labels render as light
badges under the MAC on the list.

**Flash-mode guard by inventory.** `pixie-flash-once` and
`pixie-flash-always` bindings now require a target_disk_serial that
matches the machine's stored inventory. Server-side raises 422 with
three distinct failure lanes (no inventory yet -> bind
pixie-inventory + power-cycle first; inventory present but no
target picked; target serial not in the current inventory), and the
machine detail form's Save button is JS-disabled until the
constraint is met so the operator sees the prerequisite before
clicking.

**Image picker gated by boot_mode.** When a boot_mode does not
consume an image, the picker (and its accompanying sanboot /
target-disk fields) render truly `disabled` and take a
`(not used by <mode>)` inline hint rather than a plain grey. The
stored values still survive a mode flip via a submit-time re-enable,
so a sanboot calibrated under ipxe-exit is not silently cleared when
the operator swaps to ramboot. Ramboot additionally hides options
whose blob is not fetched.

**Events log page with kind + subject filters.** The events subnav
grew two strict-equality dropdowns (kind + subject_kind) on the
right slot. Both are allowlisted against the closed
`KNOWN_EVENT_KINDS` registry + the observed subject_kind values, so
a stale bookmark with a bogus value is silently dropped instead of
rendering an empty page. Filters compose with the freeform q search
+ pagination.

**Table shape ported wholesale from bty.** Catalog, machines, and
events pages now render with bty's card-header contract: the title
label on the left, an inline freeform filter beside it, and a
Bootstrap `pagination-sm` list with per-page selector on the right,
all on one row inside the card-header. Column headers are sortable
via a shared `sort_header` macro whose URL grows a
`?sort=<col>&dir=asc|desc` pair guarded by a per-page allowlist.
Subnav strip trimmed to the promised contract: relative anchor
links on the LEFT + inline forms on the RIGHT, nothing else.

**Delete confirmations on destructive actions.** Machines list,
catalog list, and catalog detail's Delete buttons now spawn a
JS confirm dialog spelling out what gets deleted vs what stays (row
vs blob vs both). The already-warned "Delete anyway" chains on
catalog_detail keep their existing banner.

**Richer hardware inventory rendering.** Machine detail's inventory
pane now surfaces System / CPU / Memory / Network sections when the
live env's pixie CLI reports them, falling back to the existing
disks table + raw lshw JSON when a section is absent. Each section
guards its own presence so a partial payload still displays the
parts that did come through.

**Closed-set event kinds with strict enforcement.**
`pixie.events._kinds.KNOWN_EVENT_KINDS` names every kind pixie will
emit; `EventsLog.emit()` raises `UnknownEventKind` on anything not in
the frozenset. Missing emit sites (catalog blob deleted, catalog
entry updated, catalog import ok / failed, export nbdkit spawned,
TFTP started / stopped) all landed in this release cycle.

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
