# Changelog

Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The
format captures what actually matters to an operator running pixie (the
`pixie-lab` PyPI package + `pixie` container): behaviour the operator
perceives, defaults that survived a `pip install -U`, and gates that
landed in CI.

Per-release commit history lives in `git log`; this file is the
operator-facing summary.

## [Unreleased]

### Changed

**Integration tests now cover the flash core, and CI tracks their
coverage.** `flash.py` (the destructive disk-writing path) was only
reachable through the containerised boot chain, which coverage can't
measure, so it sat at ~16%. A new in-process integration suite
(`tests/integration/test_flash.py`) flashes real images to a loop-device
target for every supported format (raw + gz/zst/xz/bz2/qcow2), over both
the local-file and URL streaming paths, and verifies on-the-wire sha256
integrity; the integration CI job now runs under coverage too. This
lifts measured `flash.py` coverage to ~67% (the remainder is UEFI NVRAM
boot-order rewriting, which needs real firmware). Pure plan/validate
branches also gained unit tests.

## [0.4.11] - 2026-07-27

### Changed

**Live env: pick the image from a dropdown.** The Advanced "Image
content sha" field on the Live env page is now a dropdown of fetched
images (name + short sha) instead of a raw 64-hex paste; a current value
that isn't in the catalog (e.g. a `$PIXIE_LIVE_ENV_IMAGE_SHA` override)
stays selectable.

## [0.4.10] - 2026-07-27

### Fixed

**`pixie-lab` was broken on PyPI (0.4.7-0.4.9).** A too-broad sdist
`exclude` pattern (`"deploy"`, meant for a non-existent top-level build
tree) also matched `src/pixie/deploy`, and since `uv build` builds the
wheel from the sdist, the published wheel shipped without the deploy
module -- `uv tool run pixie-lab` failed with `ModuleNotFoundError: No
module named 'pixie.deploy'`. The sdist now uses an explicit `include`
whitelist that can't collide with a nested directory, and CI runs both
`pixie-lab` and `pixie` from the freshly built wheel so a missing
entry-point module fails the build instead of shipping.

## [0.4.9] - 2026-07-27

### Added

**Action feedback banners.** Mutating actions that redirect (catalog
import, overlay prune, settings + live-env saves) now show a one-shot
success/error banner instead of a silent page reload -- so "imported 12
sources", "reclaimed 3 overlays", or "import failed: 404" is visible
without digging in the event log. Catalog Import in particular was fully
silent on both success and failure.

**Friendly boot-mode labels.** The machines list + machine detail render
boot modes as their friendly name (e.g. "Flash once" for
`pixie-flash-once`) with the raw mode + description in a tooltip, instead
of a bare mode string.

**New docs: a Concepts primer** (how Catalog -> Images -> Overlays ->
boot modes relate) and a **usbboot `.iso`** page (booting via BMC virtual
media / dd / Ventoy, the `PIXIE_IMGS` partition, the >=2 GiB RAM floor),
plus a Secure Boot note (the PXE + live-env chain is unsigned; disable
it on targets).

### Fixed

**UI machine binds are now audited.** Binding a machine through the web
form emitted no event, while the JSON API did; both now emit the same
`machine.bound` / `machine.binding.changed` via a shared helper.

**Login page stops advertising the default password** once it's been
changed (the hint now honours the same flag as the warning banner).

**Renamed "Fetch latest catalog" to "Import full nosi catalog"** -- it
stages source rows, it doesn't download bytes (that's the per-row
Fetch), and the old label collided with that verb.

Also: the TUI disk picker now advertises `[Enter] rescan` when no disks
are detected instead of a dead-end `[#] pick` prompt; and corrected the
README (overlay path + default boot mode) and a stale "flash path is
unimplemented" docstring.

## [0.4.8] - 2026-07-27

### Added

**A first-run checklist on the dashboard.** A "Get started" panel tracks
the three setup steps in order -- set up the live env, point DHCP at
pixie, boot and bind a target -- with a live done/todo state for each,
and hides once all three are complete. It gives a new operator the
ordering the individual pages otherwise left implicit.

### Changed

**Bind failures are shown instead of silently dropped.** Saving a machine
binding that can't apply (most often an overlay alias already held
single-writer by another machine) now redirects back to the machine page
with the reason, rather than a silent no-op that read as "Save did
nothing."

**A target with no live env set up says so on its console.** When a
`pixie-*` boot mode has no live-env image selected/fetched, the target
used to fall through to the next boot device with nothing on screen; it
now prints the reason and a pointer to the Live env page before iPXE
exits, so an operator watching the console (or SoL / IP-KVM) sees why.

**`pixie-lab init` flags the default admin password.** `init` bakes the
well-known default into its editable template (unlike `deploy`, which
generates a random one), and now says so loudly so the hand-bringup path
doesn't ship the public password unnoticed.

### Fixed

**Trimmed the PyPI source distribution.** It no longer ships the
`.github/`, `docs/`, or local `.claude/` trees -- none are needed to
build or install `pixie-lab`.

## [0.4.7] - 2026-07-27

### Changed

**The usbboot `.iso` is now an Arch (archiso) live image, not Debian.**
The bootable USB / BMC-virtual-media ISO
(`pixie-usbboot-pc-x86_64.iso`) is now built with `mkarchiso` from an
Arch `releng` fork (`pixie-media/archiso/`) instead of Debian
live-build. The Arch kernel + full linux-firmware carry the NIC drivers
in-tree, so the r8125 DKMS module the Debian ISO built (to bring up
2.5G Realtek NICs) is gone, along with the whole live-build tree. The
image is proven to mount + boot via a real BMC's Redfish virtual media
(PiKVM / JetKVM / Redfish). The operator-facing live env is unchanged:
the same `pixie` TUI on tty1, the same flash / inventory / wizard flow,
and the same writable `PIXIE_IMGS` exFAT partition, written with `dd` /
Etcher / Rufus DD-mode / Ventoy exactly as before. The ISO grew from
~200 MiB to ~1.3 GiB (full linux-firmware), still under GitHub's 2 GiB
release-asset limit.

## [0.4.6] - 2026-07-27

### Added

**More nosi images out of the box + a "Fetch latest catalog" button.**
The seeded catalog now also carries `nosi arch-headless` (nbdboot via
the shared arch bundle) and `nosi freebsd-14/15-headless` (flash-only),
so a fresh deploy lists them without any import. A **Fetch latest
catalog** button on the Catalog page imports the full upstream nosi
catalog (every flashable variant -- all distros + desktop / proxmox /
rpios + freebsd, rolling tags) on one click, so you can pull the whole
set on demand without pixie hand-maintaining a copy that drifts. The
on-target live-env wizard's image-source screen gains a matching
`[n] nosi (full)` choice alongside `[d] default` (pixie's curated set).

## [0.4.5] - 2026-07-27

### Fixed

**`pixie-lab deploy` no longer looks like it hangs.** `deploy` captured
`podman compose`'s output, so a first deploy sat silent for a minute or
two while podman pulled the image cold. It now pulls the image with
visible progress before the compose up (and prints a "starting the
container..." line while it waits on `/healthz`), so you can see it
working instead of guessing whether it wedged.

## [0.4.4] - 2026-07-27

### Added

**One-click live-env setup.** The Live env page now has a **Set up live
env** button that fetches the `pixie-live-env` image + its arch-headless
netboot bundle and selects the image in a single action, with a live
progress bar -- no more hopping to the Catalog tab, fetching two entries
by hand, and copying a `content_sha256` onto the Live env page. The
manual "select by content sha" form remains as an advanced override.

## [0.4.3] - 2026-07-26

### Removed

**The squashfs live-env path is retired.** The live-env boot modes
(`pixie-inventory` / `pixie-tui` / `pixie-flash-once` / `pixie-flash-always`)
now boot the disk image selected by `PIXIE_LIVE_ENV_IMAGE_SHA` over
ephemeral nbdboot, full stop -- the old Debian live-boot squashfs
delivery is gone. Removed: the `boot=live fetch=<squashfs>` render
fallback (an unset image sha now degrades to the unavailable plan, so
select an image on the Live env page), the in-app "Fetch live-env"
action and `/ui/live-env/src/edit` route, the `PIXIE_LIVE_ENV_SRC` /
`PIXIE_LIVE_ENV_DIR` settings and the `/boot/pixie-live-env` mount, and
the `netboot-pc` live-build bake (with its `pixie-live-env-x86_64.tar.gz`
release asset). The `usbboot-pc` bootable `.iso` -- for ad-hoc
flash-install of images -- is unchanged.

## [0.4.2] - 2026-07-26

### Added

**The live-env boots as an ephemeral-nbdboot disk image, not a
live-boot squashfs.** The inventory / tui / flash boot modes now
nbdboot a normal disk image (nosi `arch-headless` with the pixie CLI +
`pixie-on-tty1` service injected) with a tmpfs overlay, instead of
fetching a live-boot squashfs. This fixes boxes whose NIC never came up
under the old Debian live-boot initramfs (dual-NIC Intel `igb` among
them): the image rides the same dracut ramboot path that nbdboot
already uses, which brings those NICs up. Point pixie at it by fetching
the `pixie-live-env` catalog entry and setting `PIXIE_LIVE_ENV_IMAGE_SHA`
(or the `live_env.image_sha` setting) to its content sha; the image ships
as a release asset (`pixie-live-env-x86_64.img.gz`, under 2 GB) built and
published by CI.

### Fixed

**Rootfs extraction picks the Linux root partition by GPT type.** The
fetcher extracted partition 1 as `rootfs.raw`, correct for the
ubuntu/debian cloud images (root at p1) but not for arch/fedora, whose
images place BIOS-boot + ESP first and root at p3 -- there it produced a
1 MiB stub that could not mount. It now selects by type (Linux-root GUID,
then the largest Linux filesystem, then the largest non-firmware/boot/swap
partition), so every nosi image extracts its real root.

## [0.4.1] - 2026-07-25

### Changed

**New machines default to `pixie-inventory`, one-shot.** A
freshly-discovered MAC now auto-registers with `pixie-inventory`
instead of `ipxe-exit`: non-destructive (boots the live env, collects
lshw + disks, posts them, exits to firmware) and immediately useful --
every new machine's hardware shows up and the flash modes (which
require inventory) become available without a manual pass. It is
one-shot: the first inventory POST flips the binding to `ipxe-exit`, so
a PXE-first box inventories itself exactly once and never boot-loops.
With no live env staged the plan degrades to exit, so a bare deploy
behaves like before until the operator fetches the live env. Override
per deploy with `PIXIE_DEFAULT_BOOT_MODE` (e.g. `ipxe-exit` for the old
behaviour).

### Fixed

**Live env boots on arbitrary NICs (netboot-pc initramfs is now
`MODULES=most`).** The live env fetches its squashfs over HTTP from the
initramfs before pivot-root, so the target's NIC must be driven from
inside the initrd. It was built the mkinitramfs default `MODULES=dep` --
only the build host's drivers (plus the DKMS'd r8125) -- so a target
with a different NIC came up to `initrd` and then stalled forever at the
squashfs fetch, silently breaking `pixie-inventory` / `-tui` / `-flash`
on that box (observed on an ASRock Rack board). The netboot-pc bake now
sets `MODULES=most`, baking the broad Debian-installer driver set so an
arbitrary lab machine's NIC (and storage) is alive before the fetch.
Costs some initramfs size; worth it for an appliance whose job is
booting unknown hardware.

**No more `${PIXIE_LIVE_ENV_EXTRA_CMDLINE:-}` literal on the kernel
cmdline.** On a deploy whose compose runner doesn't expand `${VAR:-}`
(podman-compose passes the unset default through verbatim), the
container env held the literal string `${PIXIE_LIVE_ENV_EXTRA_CMDLINE:-}`,
which pixie then appended to every live-env / nbdboot kernel cmdline.
Harmless (the kernel ignores the unknown token) but wrong. The
extra-cmdline resolver now treats a value that is nothing but an
unexpanded `${...}` placeholder as unset, so it can't leak -- fixed for
any deploy without regenerating compose.

## [0.4.0] - 2026-07-24

### Changed

**Machines list Image column reads like the new model.** The bound
image now shows the catalog name linked to its Images page (short sha
for an orphan blob whose entry was deleted), instead of a bare hash,
and is blank for boot modes that don't consume an image (ipxe-exit /
inventory / tui) rather than showing a stale sha.

**Catalog table is leaner.** The sources listing drops the inline
content sha + byte size (image facts that live on the linked Image
page); the fetched/error/in-flight readiness state stays.

**Default timestamp format drops the timezone suffix.** Operator-facing
timestamps now default to `%Y-%m-%d %H:%M:%S` (24-hour, no ` %Z`); the
display timezone is still an operator setting, just not repeated on
every row. Set `PIXIE_DATETIME_FORMAT` or the Settings override to add
it back.

**Overlays are now globally-named single-writer volumes.** A persistent
nbdboot overlay is no longer a per-`(machine, image, profile)` triple; it
is a globally-unique named writable volume (`alias`) over ONE base image,
and the base image is implied by the alias. At most one machine may hold
an alias at a time: attaching one already held by a different machine is
rejected in the app ("held by <mac>; detach first"), with qemu-nbd's
qcow2 image-lock as the backstop. On a machine's detail page the picker
now offers the aliases that are free (or already held here) plus a
create-new flow; attaching an existing alias binds the machine to that
alias's base image. The Overlays page keys on the alias, shows an
**Attached to** column (a MAC or "free"), and classifies each row as
serving / held / free / orphaned / missing; Prune still reclaims only the
orphaned + file-missing rows and leaves a free alias (a deliberate keep)
alone. On-disk overlays move from
`overlays/<mac>/<image_sha>/<profile>.qcow2` to a flat
`overlays/<alias>.qcow2`. Existing state.db rows migrate in place on
first start: each old overlay becomes `alias = <profile>-<mac_slug>`
holding its original MAC, its qcow2 path is kept as-is (no large-file
move), and each machine's binding is rewritten to the same derived alias.

**Dashboard speaks the new model.** The tiles now mirror the
storage/lifecycle vocabulary + the nav: **Machines**, **Catalog**
(sources you can fetch, with a not-yet-fetched count), **Images** (the
materialised entities, with on-disk footprint + how many are
reclaimable), **Overlays** (alias-keyed writable volumes, with disk-used
+ serving), and **Events** -- instead of the old catalog-images /
NBD-exports framing. The Acknowledge button was removed from the
dashboard (it's an action; the dashboard is output-only -- ack lives on
the Events page).

**Catalog is sources-only now.** With Images owning the materialised
view, the Catalog page drops the NBD-serving column and the
Stop-export / blob-delete actions (those live on Images). It keeps
source management -- add, import, Fetch, delete-entry -- and a fetched
disk-image row now links to its **Image** for footprint, live NBD /
overlay usage, and GC.

**Legacy `/ui/exports` lands on Images.** The kept-alive redirect for
the old exports URL (and the Stop-export action) now points at the
Images view, where NBD export usage is surfaced per content sha, rather
than at Catalog.

**Overlay picker shows aliases held elsewhere, disabled.** The
machine-detail overlay dropdown used to hide any alias held by another
machine, so an operator searching for it assumed it didn't exist and
tried to create a duplicate (which the single-writer bind then
rejected). Held-elsewhere aliases are now shown as a disabled option
labelled with the holder MAC ("held by `<mac>`") -- visible, but not
selectable.

**Overlays distinguish "pending" from "file missing".** A reserved
overlay alias whose qcow2 hasn't been lazy-created yet (never booted)
previously read as *file missing* -- the same alarming state as a
booted-then-lost overlay -- and Prune would reclaim it as junk. Such an
overlay is now classified **pending** (benign, awaiting its first
nbdboot) and is left alone by Prune; *missing* is reserved for an
overlay whose qcow2 vanished after a prior boot (real data loss). The
`last_boot_at` timestamp is the discriminator.

### Added

**First-contact machines emit a `machine.discovered` audit event.** The
first time an unknown MAC hits `GET /pxe/<mac>` and is auto-registered,
pixie now records one `machine.discovered` event (carrying the
auto-registered boot mode + the client IP); repeat contacts do not
re-emit. The kind was reserved but never fired before.

**Images: the materialised content behind Catalog sources.** A new
**Images** page (`/ui/images`) separates the *source* (a Catalog entry,
a URL you can fetch) from the *entity* it produces once fetched (an
image, identity = the disk content sha). Machines, NBD exports, and
overlays all key off that sha, so each image rolls up its on-disk
footprint (raw disk + rootfs + boot bundle + overlays) and every live
usage -- machines bound, the ephemeral nbdkit export, and per-machine
qemu-nbd overlays -- with counts that link into the per-usage admin
surfaces. An **image detail hub** (`/ui/images/<sha>`) lays out the
artifacts + every usage inline and offers a **guarded delete / blob GC**:
allowed only when the usage count (the refcount) is zero, it removes the
whole `blobs/<sha>/` dir (raw disk **and** the `rootfs.raw` the old
per-entry delete leaked) and clears the sha off every entry that
resolved to it. The list also surfaces **orphan blobs** -- sha dirs on
disk with no catalog entry -- which is where the un-GC'd disk pressure
actually hides; they delete straight through. Catalog is unchanged for
now.

### Fixed

**Live-env artifacts are served after a post-startup fetch, no restart
needed.** The `/boot/pixie-live-env/` mount was created at app startup
only if the directory already existed, so on a fresh deploy -- where the
operator stages the netboot-pc bake later via "Fetch live env" -- the
plan pointed targets at `/boot/pixie-live-env/vmlinuz` and friends that
then 404'd until pixie was restarted, silently breaking every
`pixie-inventory` / `-tui` / `-flash` boot. The mount is now created
unconditionally (dir ensured, `check_dir=False`), so a fetched live-env
is served immediately.

**`pixie-lab purge --all` no longer tracebacks on a root-owned parent.**
When the deploy dir lives under a directory the operator can't write
(the common `/opt/pixie` case), purge could empty the dir but not
remove the dir itself, and surfaced the failure as a raw Python
traceback. It now clears the state, prints a plain one-line caveat
("emptied `<dir>`, but could not remove the directory itself ..."), and
exits 0 -- the purge did its job; only the empty-dir removal was blocked
by the parent's permissions.

## [0.3.2] - 2026-07-24

### Added

**CI verifies the usbboot `.iso` boots via Ventoy.** A new
`verify-usbboot` job structurally checks the bake (valid ISO 9660 +
isohybrid MBR + El Torito BIOS *and* UEFI boot entries + sha256), then
installs Ventoy onto a QEMU disk, drops the `.iso` + a sentinel image
catalog onto it, and boots it -- asserting the pixie live env comes up
correctly on the Ventoy loop-boot path (`pixie-usb-grow` skips with no
`PIXIE_IMGS` partition, `pixie-images-discover` bind-mounts the
operator drop at `/var/lib/pixie/images`, and `pixie-on-tty1` renders
the CLI wizard). The job gates the publish jobs, so a broken,
non-bootable, or non-Ventoy-compatible ISO can never ship. Ported from
bty's `test-usb-ventoy`.

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
