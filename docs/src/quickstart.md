# Quickstart

Bring pixie up, point DHCP at it, and boot a first target.

## Bring pixie up

One command. It generates the config, starts the container, and waits
until pixie answers healthy:

```
uv tool run pixie-lab deploy /opt/pixie --host-addr <LAN-IP>
```

`uv tool run` fetches `pixie-lab` into a throwaway environment (nothing
to install or leave behind). `deploy` writes `compose.yml` + `envvars`
into `/opt/pixie`, brings the stack up under `network_mode: host` (so
TFTP + the NBD range + HTTP all reach the LAN), waits on `/healthz`, and
prints the admin password it generated. The only default you usually
set is `--host-addr` (otherwise auto-detected); override the rest with
`--admin-password` / `--image` as needed.

Host prerequisite: podman + a compose provider. Then log in at
`http://<LAN-IP>:8080/`.

> Prefer to review the generated files first? `uv tool run pixie-lab
> init /opt/pixie` writes the same `compose.yml` + `envvars` without
> starting anything; edit `envvars` (`PIXIE_HOST_ADDR` +
> `PIXIE_ADMIN_PASSWORD`), then `cd /opt/pixie && COMPOSE_ENV_FILES=envvars
> podman compose up -d`.

## Usage

pixie is up; the rest is day-to-day operation. Point DHCP at it once,
then drive everything from the web UI at `http://<LAN-IP>:8080/`.

The dashboard shows a **Get started** checklist that tracks the three
setup steps below (set up the live env, point DHCP at pixie, boot and
bind a target) and their live status; it hides once all three are done.
The rest of this section is those steps in detail.

### Point DHCP at pixie

Set your DHCP server to chain PXE targets through pixie's TFTP and
iPXE bootstrap. See [](deployment.md#dhcp-handoff) for BIOS + UEFI
dnsmasq recipes.

### Add a catalog entry

Open `http://<PIXIE_HOST_ADDR>:8080/ui/catalog` and log in. A fresh
deploy already seeds a curated set of nosi images (debian / ubuntu /
fedora / arch headless, freebsd-14/15, and the `pixie-live-env` image)
plus the netboot bundles -- these are pointers, so nothing is
downloaded until you Fetch. For the full upstream set (every nosi
variant incl the desktop / proxmox / rpios shapes), click **Import full
nosi catalog**; to add anything else, paste a `catalog.toml` URL in the
Import bar. Then hit **Fetch** on a row: pixie pulls the bytes to disk
(decompressing `img.gz` / `img.zst` on the way in), and the row flips to
`fetched` when the pipeline lands.

### Select the live env

The `pixie-inventory`, `pixie-tui`, and `pixie-flash-*` boot modes boot
the `pixie-live-env` disk image (the operator TUI + CLI baked in) over
nbdboot. On `/ui/catalog`, Fetch the **`pixie-live-env`** image and its
**`nosi arch-headless netboot bundle`** (both ship in the seed catalog).
Then open the **Live env** page (`/ui/live-env`) and set **Image content
sha** to the fetched image's `content_sha256` (shown on its catalog
row), or set `PIXIE_LIVE_ENV_IMAGE_SHA` in `envvars`. Until an image is
selected, those boot modes render an `unavailable` plan. See
[](deployment.md#select-the-live-env) for detail and the air-gapped
path.

### Bind a machine

Once a target has PXEd at least once, it appears on `/ui/machines`.
Click its MAC to open the machine detail page. Pick a boot mode from
the card grid, pick an image, and Save. On the target's next PXE,
pixie serves the plan you bound.

See [](boot-modes.md) for what each mode does and how bindings pin a
machine to an image and, for `nbdboot`, an overlay volume.

## Tear it down

One command, the inverse of deploy:

```
uv tool run pixie-lab purge /opt/pixie --all --yes
```

`--all` removes the container, its image, and `data/`. Drop `--all` to
stop + remove the container only, or pick with `--data` / `--images`.
Without `--yes` it prints the plan and asks first -- the `--data` /
`--all` deletions are irreversible, and even a plain stop drops every
live NBD export (any target booted `nbdboot` off pixie loses its root).
