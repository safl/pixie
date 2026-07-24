# Quickstart

This walks through a first pixie deploy: install, bring the container
up, point DHCP at it, and boot a first target.

## Install the CLI

```
pipx install pixie-lab
```

`pipx` is preferred so pixie's runtime deps stay isolated from the
system Python. `uv tool install pixie-lab` works too.

The `pixie-lab` distribution ships two console-scripts: `pixie-lab`
generates the container deployment bundle, and `pixie` is the
operator TUI baked into the pixie live env (targets run it after
booting the live env; operators don't call it directly on their
workstation).

## Generate the deploy bundle

```
mkdir /opt/pixie && cd /opt/pixie
pixie-lab init
${EDITOR:-vi} envvars     # set PIXIE_HOST_ADDR + PIXIE_ADMIN_PASSWORD
```

`pixie-lab init` writes `compose.yml`, `envvars` (with placeholder
values), and `envvars.example` into the current directory. Edit
`envvars` to point pixie at your LAN address and set a real admin
password.

## Bring pixie up

```
podman compose --env-file envvars up -d
curl http://<PIXIE_HOST_ADDR>:8080/healthz
```

The compose file runs pixie under `network_mode: host` so udp/69
(TFTP) plus the NBD port range plus tcp/8080 (HTTP) all reach the
LAN without publish gymnastics.

## Point DHCP at pixie

Set your DHCP server to chain PXE targets through pixie's TFTP and
iPXE bootstrap. See [](deployment.md#dhcp-handoff) for BIOS + UEFI
dnsmasq recipes.

## Add a first catalog entry

Open `http://<PIXIE_HOST_ADDR>:8080/ui/catalog` in a browser, log in
with your admin password, and add a catalog entry with a source URL.
Hit Fetch. Pixie pulls the bytes to disk (and for `img.gz` / `img.zst`
inputs, decompresses on the way in). The row flips to `fetched` when
the pipeline lands.

## Bind a machine

Once a target has PXEd at least once, it appears on `/ui/machines`.
Click its MAC to open the machine detail page. Pick a boot mode from
the card grid, pick an image, and Save. On the target's next PXE,
pixie serves the plan you bound.

See [](boot-modes.md) for what each mode does and how bindings pin a
machine to an image and, for `nbdboot`, an overlay volume.

## Tear it down

`pixie-lab purge` is the inverse of the deploy. Run it from the deploy
directory (or pass the path):

```
pixie-lab purge            # stop + remove the container only
pixie-lab purge --data     # also DELETE data/ (state.db, blobs, artifacts, overlays, live-env)
pixie-lab purge --images   # also remove the pixie container image
pixie-lab purge --all      # container + image + data/ + the deploy directory itself
```

Purge prints its plan and gates every variant behind a `y/N`
confirmation; pass `--yes` to skip the prompt for scripted teardown.
Even the plain stop is impactful: it drops every nbdkit / qemu-nbd
export, so any target currently booted `nbdboot` off pixie loses its
NBD root at that moment. The `--data` and `--all` deletions are
irreversible, so the confirmation is deliberate.

The single-shot `pixie-lab deploy` is the mirror image of purge: it
runs `init`, auto-fills `envvars`, brings the stack up, and waits on
`/healthz` in one command, for when you don't need to hand-edit the
generated files first.
