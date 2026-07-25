# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`pixie` is a bare-metal netboot appliance in one container: catalog + fetch
+ NBD serving + PXE plan renderer + TFTP + operator UI, on one FastAPI
process with one `state.db` and one admin password. It folds into a single
service what three sibling projects implement separately: `bty` (operator UI
+ machine registry + PXE renderer + Rich TUI), `nbdmux` (NBD-export
multiplexer + netboot serve), and `withcache` (catalog + fetch + blob
store). Those three continue as their own projects; pixie is a fourth sibling
that starts from a merged design. `PLAN.md` is the source of truth for
decisions, rationale, and roadmap.

The distribution is `pixie-lab` (PyPI); the container is
`ghcr.io/safl/pixie`. Two console-scripts ship from it: `pixie` (the
Rich-based operator TUI, runs inside the live env on target hardware) and
`pixie-lab` (deploy generator: `init` / `deploy` / `purge`). Requires
Python >= 3.11; the codebase is `mypy --strict`.

## Commands

Everything CI does is a `make` target; run `make help` for the full list.

```sh
make ci             # lint + format-check + typecheck + test (the full gate)
make test           # uv run pytest -q  (SKIPS the integration marker)
make lint           # uv run ruff check .
make format         # uv run ruff format .   (format-check = --check)
make typecheck      # uv run mypy src
make deps           # uv sync --group dev
```

Run a single unit test:
```sh
uv run pytest tests/test_pxe_plan_json.py -q
uv run pytest tests/test_machines_unit.py::test_upsert_binding -q
```

Unit tests never need env-vars or network: `_resolve_state_dir()` falls back
to a tempdir when `/var/lib/pixie` is unwritable, and `conftest.py` pins the
admin password + forces the fetch transport to fail fast.

### Integration + media builds (opt-in, need real hardware emulation)

The `integration` pytest marker and the `test-pxe*` make targets spin up a
real pixie container + QEMU guest + bridge/tap/dnsmasq to exercise the PXE
bootstrap chain end to end. They are gated so a plain `pytest`/`make test`
stays fast and offline.

```sh
uv run pytest -m integration          # the container-backed suite
make test-pxe                         # end-to-end PXE bootstrap chain (needs podman + QEMU + KVM + dnsmasq)
make test-pxe-nbdboot                 # nbdboot chain (needs a prior VARIANT=netboot-pc bake)
make test-pxe-inventory / -flash / -flash-always / -tui   # per-boot-mode chains
make build VARIANT=usbboot-pc         # live-build a media image (needs live-build + passwordless sudo)
make build VARIANT=netboot-pc         # kernel + initrd + squashfs trio for the live env
make ipxe                             # build pixie's custom iPXE binary with the chain-loader baked in
```

The media/CI pipelines live under `cijoe/` (build-time only, no runtime
surface): `cijoe/tasks/*.yaml` are the workflows, `cijoe/configs/*.toml` the
per-variant + per-chain-test configs. `matx-bmc` (10.20.30.61) is the
hardware-validation target referenced throughout.

## Architecture

### One app, state on `app.state`

`pixie.web.main.create_app()` is a factory (fresh app per test fixture, no
module globals). It constructs every store against a single sqlite file
(`CatalogStore` owns `state.db`; `ExportsStore`, `OverlaysStore`,
`MachinesStore`, `EventsLog`, `SettingsStore` all open the same `db_path`)
and attaches them to `app.state`, plus the `NbdServer` supervisor, the
`PlanRenderer`, the TFTP supervisor, and the fetch thread pool. Route
handlers reach everything via `request.app.state.<name>`. Read the top of
this file for the full env-var surface (`PIXIE_*`); the Settings >
Deployment card is generated from `_deployment_envvar_docs()`, so keep that
list and the actual resolver constants in sync.

Subsystems are packages under `src/pixie/`: `catalog/` (fetch + blob store,
forked from withcache), `exports/` (NBD lifecycle + nbdkit/qemu-nbd
supervision, from nbdmux), `machines/` (registry + boot-mode table), `pxe/`
(plan renderer + iPXE templates), `tftp/` (in-process bootstrap), `events/`
(audit log), `web/` (FastAPI wiring + Jinja templates + static, base from
bty), `tui/` (Rich operator wizard), `pivot/` (nbdboot initramfs overlay),
`deploy/` (the `pixie-lab` generator). Each package keeps its routes,
store, and schema in leading-underscore modules (`_routes.py`, `_store.py`,
`_schema.py`).

### Fetch model: presence on disk IS readiness

One verb, `fetch`. It downloads bytes from an ORAS or HTTPS src (shelling
out to `curl`), streams sha256 while writing, atomically renames into place;
for `.tar.gz` netboot bundles it additionally unpacks
vmlinuz + initrd + manifest.json into a content-addressed artifacts dir.
There is no misses page, no auto-fetch, no cache-through, no
ready/pending state machine. A blob or manifest existing on disk is the only
readiness signal (`_fetch_would_be_noop` in `web/main.py` encodes this).
Fetches run on `app.state.fetch_pool`, not in the request handler.

### Catalog schema: two peer entries with a URL cross-reference

Catalog TOML carries two peer `[[images]]` kinds. A disk-image entry
(`bindable=True`, has a `content_sha256`) carries `netboot_src = "<url>"`
pointing at a netboot-bundle entry's `src` — a URL cross-reference, not a
name string. The bundle is its own fetchable entity so a ramboot-only
workflow with no disk image stays coherent. The legacy `netboot_ref`
name-string field is loose-parsed (accepted with a warning) so existing nosi
catalogs read out of the box; pixie tight-emits `netboot_src` when rewriting.

### On-disk state layout (`PIXIE_DATA_DIR`, default `/var/lib/pixie`)

`state.db` (all rows), `blobs/<sha>/blob` (fetched disk images),
`artifacts/<sha>/{vmlinuz,initrd,manifest.json}` (unpacked netboot bundles,
served over HTTP at content-addressed `/artifacts/<sha>/...`),
`overlays/<mac>/<image_sha>/<profile>.qcow2` (per-machine writable overlays
for nbdboot), `live-env/` (the netboot-pc bake: kernel + initrd + squashfs
the pixie live env boots from).

### Boot modes + the plan renderer (the core dispatch)

`machines/_store.py` defines the boot-mode frontier: `BOOT_MODES =
{ipxe-exit, nbdboot} | LIVE_ENV_MODES`, where `LIVE_ENV_MODES =
{pixie-flash-once, pixie-flash-always, pixie-inventory, pixie-tui}`.
`BOOT_MODE_META` carries per-mode operator docs and is asserted at import to
have exactly one row per mode, so adding a mode without metadata fails loudly
rather than silently rendering "unknown boot_mode".

`pxe/_renderer.py` (`PlanRenderer`, built once, called per `GET /pxe/<mac>`)
is the dispatch table. `ipxe-exit` -> `exit.j2`. `nbdboot` walks
`catalog[image_sha] -> netboot_src -> catalog[netboot bundle]` to find the
artifacts key, ensures an NBD export exists for the disk-image blob, and
renders `nbdboot.j2`. The live-env modes render `pixie-live-env.j2` against
the staged `live-env/` bake. Any failure (no bound image, bundle not fetched,
NBD spawn refused, live-env dir missing) degrades to `unavailable.j2` with
the reason baked into the plan comment. The renderer is pure apart from the
NBD spawn, which is idempotent per (name, blob_path).

### PXE wire contract (`pxe/_routes.py`), open by design

PXE targets hold no session cookie, so these routes are unauthenticated:
`GET /pxe-bootstrap.ipxe` (chain-loader), `GET /pxe/<mac>` (per-machine iPXE
plan; first contact auto-registers the MAC with `ipxe-exit`),
`GET /pxe/<mac>/plan` (JSON the live-env `pixie` CLI reads to decide
auto-flash / interactive / no-op), `POST /pxe/<mac>/status`,
`POST /pxe/<mac>/inventory` (the live env posts an lshw blob back). The
catalog read routes (`GET /catalog`, `/catalog.toml`, `/b/<sha>/<name>`,
`/artifacts/<sha>/<file>`) are open for the same reason. Everything under
`/ui/*` and the write routes require a valid session
(`_require_ui_auth` / `require_auth`).

### The live-env `pixie` CLI (target-side)

`pixie.tui` is the console-script entry (module name is historical). It
imports nothing from Rich at module level so `import pixie.tui` works without
the TUI extra; the real wizard is in `tui/_app.py`, loaded on invocation.
Invoked as `pixie --mac <MAC> --server <host>`, it fetches
`GET /pxe/<mac>/plan` and acts on the mode: auto-flash (flash-once /
-always), interactive wizard (pixie-tui), inventory POST, or no-op. The
per-boot-mode `make test-pxe-*` targets each assert one of these chains end
to end.

### Auth + supervision lifecycle

Session-cookie only (`SessionMiddleware` signs `pixie-token`, 7-day sliding
TTL, LAN-only so `https_only=False`); the bearer-token surface bty/nbdmux/
withcache carried is dropped. Admin password and display timezone/strftime
both have env-var overrides plus live DB overrides via `/ui/settings`; env
wins so a compose deploy pins behaviour. On startup the lifespan re-spawns
nbdkit for every stored export whose blob still exists and qemu-nbd for every
overlay whose qcow2 still exists (`_respawn_exports_at_startup` /
`_respawn_overlays_at_startup`), so a container recreate doesn't leave a wall
of `status=error` rows; children are torn down on graceful shutdown.

## Conventions + gotchas

- **`bty.*` kernel-cmdline tokens are intentional, not stale.**
  `ipxe/nbdboot.j2` and `ipxe/pixie-live-env.j2` emit BOTH `pixie.*` and
  `bty.*` prefixes on the kernel cmdline. nosi netboot bundles published
  before the pixie rename ship initrds that grep for `bty.server` /
  `bty.mac` / `bty.nbd` / `bty.image`; last-token-wins keeps both readable.
  Do not "clean these up" without confirming the target-side initrd no
  longer needs them.
- Sibling-project names (`bty`, `withcache`, `nbdmux`) also appear in
  comments/docstrings as deliberate lineage narration ("ported from bty's
  `_table_macros.html`", palette-anchor notes in `layout.html`). Those are
  provenance, not TODOs. `PLAN.md` and `docs/audit.md` narrate the lineage on
  purpose. Distinguish these from genuinely stale references before editing.
- Ruff: line-length 100, target py311, `select` includes E/F/W/I/B/UP/RUF/
  SIM/PERF/RET/PIE/C4 with RET504/RET505 ignored. `mypy` is `strict`.
- Runtime deps are kept intentionally lean (FastAPI + uvicorn + Jinja +
  itsdangerous + python-multipart + tomlkit + Rich + httpx). Every pipeline
  otherwise uses stdlib: `urllib`/`curl` for downloads, `tarfile`/`lzma`/
  `gzip`/subprocess-`zstd` for decompression, `sqlite3` for state. Settings
  edits round-trip through `tomlkit` so operator comments/ordering survive.
- Templates are server-side rendered Jinja; admin POSTs 303-redirect. The
  UI nav is Dashboard / Machines / Catalog (exports merged in) / Events /
  Settings. Live-refresh polling endpoints back the dashboard + table pages.
- Commit style: Conventional Commits, motivation-focused, GPL-3.0-only.
