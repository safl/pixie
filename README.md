# pixie

Bare-metal netboot appliance in one container. Catalog + fetch + NBD
serving + PXE plan renderer + TFTP + operator UI, on one FastAPI
process with one state.db and one admin password.

Pixie folds into a single service what bty (operator UI + machine
registry + PXE plan renderer + Rich TUI), nbdmux (NBD-export
multiplexer + netboot artifact serve), and a hard-fork of withcache
(catalog + fetch + blob store) implement as three separate FastAPI
services. bty, nbdmux, and withcache continue as their own projects;
pixie is a sibling appliance that starts from a merged design. See
`PLAN.md` for the design rationale, locked decisions, and roadmap.

## Run

The reference deploy is a single podman-compose stack under
`--network=host`. The `pixie-lab` CLI (installed with the package)
generates the compose file, the envvars template, and a per-deploy
README:

    pixie-lab init /opt/pixie
    "$EDITOR" /opt/pixie/envvars.example    # set PIXIE_HOST_ADDR + PIXIE_ADMIN_PASSWORD
    mv /opt/pixie/envvars.example /opt/pixie/envvars
    cd /opt/pixie
    podman compose --env-file envvars up -d

`pixie-lab deploy` fills in an admin password + host address + waits
for `/healthz` in one shot, useful for a fresh lab machine.

Log in at http://<host>:8080/. First landing after login is a
dashboard summarising machines, catalog, and NBD-serving state; the
top nav has Machines, Catalog, Events, and Settings.

## Operator workflow

1. Point catalog import at a `catalog.toml` URL on `/ui/catalog`.
   Entries land staged (rows only, no bytes yet).
2. Click Fetch on a row to download it. The status pill ticks
   through downloading -> decompressing -> unpacking with a
   `bytes / total` counter so you see progress live.
3. Power-cycle a target with pixie in its DHCP next-server chain.
   A first contact shows up on `/ui/machines` with the default
   `ipxe-exit` boot mode. Open the row to bind a mode + image.
4. `pixie-inventory` prompts the live env to POST an inventory
   blob; the target's disks + NICs + memory show up on the
   machine detail page.
5. Flash modes (`pixie-flash-once`, `pixie-flash-always`) require
   a target disk serial that came in on the inventory. The
   binding form disables Save until that prerequisite is met.

## State

Everything writable lives under `PIXIE_DATA_DIR` (default
`/var/lib/pixie`). `state.db` holds catalog rows, machine rows,
event log, settings, NBD-export records, and persistent-overlay
records. `blobs/<sha>/blob` holds fetched disk images.
`artifacts/<sha>/{vmlinuz,initrd,manifest.json}` holds unpacked
netboot bundles. `overlays/<mac>/<image_sha>/<profile>.qcow2` holds
per-machine writable overlays for the `nbdboot` boot mode.
`live-env/` holds the pixie live env kernel + initrd + squashfs the
netboot-pc bake produced.

Both admin password and display timezone / strftime pattern have
env-var overrides plus DB overrides via `/ui/settings`; env wins
so a compose deploy pins behaviour deterministically.

## Development

    uv sync
    uv run pytest
    uv run ruff check src tests
    uv run mypy src

The integration tests spin real QEMU VMs behind a real pixie
container to exercise the PXE bootstrap + ramboot chains. They are
gated behind a marker so plain `pytest` skips them.

## License

pixie is licensed under GPL-3.0-only. See `LICENSE`.
