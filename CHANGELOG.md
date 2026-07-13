# Changelog

Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The
format captures what actually matters to an operator running pixie (the
`pixie-lab` PyPI package + `pixie` container): behaviour the operator
perceives, defaults that survived a `pip install -U`, and gates that
landed in CI.

Per-release commit history lives in `git log`; this file is the
operator-facing summary.

## [Unreleased]

## [0.5.0] - TBD

### Added

**`pixie-lab` deploy generator.** The `pixie-lab` console-script
(previously a placeholder that exited 1) now emits a working compose
deployment.

- `pixie-lab init [dest]` writes `compose.yml` (one service on
  `--network=host`), `envvars.example`, `README.md`, and a `data/`
  scaffold. The image tag baked into `compose.yml` is
  `ghcr.io/safl/pixie:<pixie-version>` at generation time; the
  operator overrides via `--image`.
- `pixie-lab deploy [dest]` builds on init: auto-detects the LAN
  address via a UDP-connect probe, generates a random admin
  password (unless `--admin-password` is passed), realises
  `envvars`, runs `podman compose up -d`, and waits for
  `/healthz`.
- `pixie-lab purge [dest]` runs `podman compose down`; add
  `--wipe-data` to drop the state volume too.
- Compose runner detection: prefers `podman-compose`, falls back to
  `podman compose`, then `docker compose`. Fails loud if none of
  those are on PATH.

Kept deliberately shallower than bty-lab (one container, no Quadlet
emission, no upgrade flow yet). Additional knobs land in follow-ups
if operators ask.

### Tests

10 unit tests over the file-emitter + argparse shape. One **real
container** integration test: `pixie-lab init` builds a deploy dir,
`podman-compose up` brings the stack up, `/healthz` answers 200. The
integration test reuses the container image the shared fixture
already built, so it adds ~30s to the CI wallclock rather than a
second full build.

## [0.4.0] - TBD

### Added

**Machines + PXE plan renderer (image-native ramboot MVP).** An
operator can now bind a MAC to a fetched catalog entry and target
that machine boots into that image with its own kernel and root over
NBD.

- `machines` table on the shared `state.db`. MAC normalisation
  accepts `aa:bb:...`, `AA-BB-...` and `AABBCCDDEEFF` shapes.
  Closed set of boot modes: `ipxe-exit` (default) and `ramboot`.
- `GET /pxe-bootstrap.ipxe` -> iPXE bootstrap that chain-loads
  `/pxe/${net0/mac}`; served over HTTP (and by an external TFTP
  daemon for BIOS-PXE clients until the in-process TFTP router
  lands).
- `GET /pxe/<mac>` -> discovery upsert + per-machine plan. First
  hit creates the row (default `ipxe-exit`); subsequent hits
  refresh `last_seen_at`.
- `GET/PUT/DELETE /machines[/<mac>]` for operator-driven CRUD.
- Ramboot plan: walks `catalog[image_sha] -> netboot_src ->
  catalog[bundle]` by URL cross-reference, verifies the bundle's
  `manifest.json` is unpacked, ensures an NBD export against the
  disk-image blob is spawned, and renders the ramboot iPXE plan
  with content-addressed artifact URLs +
  `pixie.nbd=tcp://${nbd-host}:${nbd-port}`.
- Missing / corrupt / not-yet-fetched bundle -> the renderer emits
  the `unavailable.j2` template with the reason baked in the plan
  comment and `exit`. NO fallback to a bty-media-baked kernel;
  a mismatched image-vs-modules boot is a worse operator experience
  than a clean `exit`.
- Auto-export lifecycle: binding a machine to `ramboot` triggers
  an idempotent `spawn` of an NBD export named `pixie-<sha[:12]>.img`
  for the disk-image blob. Same-machine re-binds no-op; distinct
  content shas get distinct exports on distinct ports.
- Env knobs: `PIXIE_PUBLIC_HOST` (override the URL host baked into
  the plan when pixie is fronted by a proxy), `PIXIE_NBD_PUBLIC_HOST`
  (override the NBD-side host for the same reason).

### Tests

Two new integration tests build the real container, place synthetic
blobs + unpacked bundle bytes under the bind-mounted state dir, add
catalog entries via the JSON API, bind a machine to the disk-image
entry, and prove the ramboot plan:

- References `/artifacts/<bundle-sha>/vmlinuz` +
  `/artifacts/<bundle-sha>/initrd`.
- Auto-spawns an NBD export that actually speaks NBD (raw socket
  read of `NBDMAGIC` on the port the plan advertises).
- Falls back cleanly to `unavailable.j2` + `exit` when the bundle's
  artifacts aren't unpacked on disk.

Plus 12 pure-Python unit tests over MAC normalisation, boot-mode
validation, discovery upsert, and the negative paths on the
operator write route.

## [0.3.0] - TBD

### Added

**Exports + NBD supervisor.** Hard-forked from nbdmux 0.9.2's
`NbdServer` and adapted to pixie's content-addressed model:

- `POST /exports {name, content_sha256}` -> spawn nbdkit for that
  export against `<state_dir>/blobs/<sha>/blob`. Ports allocated
  from a base + scan (`10809+` by default) and persisted on the
  export row.
- `GET /exports` + `GET /exports/{name}` (open reads): live view of
  each export's port + status. `DELETE /exports/{name}` (session
  auth) kills the subprocess and removes the row.
- Filter chain per export: `--filter=cow` always; `--filter=partition`
  when the blob has an MBR/GPT sig. cow gives ramboot targets a
  writable overlay without mutating the shared backing blob.
- Subprocess supervision: idempotent spawn, safe termination
  (SIGTERM + wait + SIGKILL escalation), diff-sync `reload()`.
- Env knobs: `PIXIE_NBD_PORT_BASE`, `PIXIE_NBD_BIND`, `PIXIE_NBDKIT_BIN`.

### Changed

Requires `nbdkit` >= 1.44 on the runtime path (per audit
`docs/audit.md#nbdmux`); the base container image already pins
`ubuntu:26.04` for this reason.

### Tests

The exports surface is verified end-to-end against the REAL pixie
container running REAL nbdkit. Fake-argv shims for `subprocess.Popen`
were tried first and removed on operator feedback -- they produced
false confidence in nbdkit argv construction that didn't survive
contact with the real binary. New `tests/integration/` builds
`pixie:integration-test` from the Containerfile, starts it with
`--network=host` + a bind-mounted state dir, and drives the JSON
API over HTTP; the "is nbdkit really up?" assertion is a raw
socket NBD handshake against the returned port that reads back
`NBDMAGIC` + `IHAVEOPT`. Gated behind `-m integration` so the fast
unit loop stays fast; CI runs it as its own job after building the
container.

## [0.2.0] - TBD

### Added

**Catalog + fetch.** Operator-curated image library, forked from
withcache's `Store` + `oras.py` and nbdmux's tar.gz-unpack pipeline:

- Add / list / delete catalog entries (`POST` / `GET` / `DELETE
  /catalog/entries`, form actions under `/ui/catalog/`).
- One `fetch` verb (`POST /catalog/entries/<name>/fetch`): downloads
  the entry's `src`, streams sha256 into `<state_dir>/blobs/<sha>/`
  atomically, and for `format=tar.gz` unpacks vmlinuz + initrd +
  manifest.json into content-addressed
  `<state_dir>/artifacts/<sha>/`. Runs in a bounded thread pool so
  concurrent fetches don't block the event loop.
- Content-addressed serves: `GET /b/<content_sha256>/<name>` for
  blobs, `GET /artifacts/<content_sha256>/{vmlinuz,initrd,manifest.json}`
  for netboot bundles. Open routes: iPXE targets don't carry
  sessions.
- Nosi-shape TOML round-trip: parse `catalog.toml`, serialise back
  in the same schema. Unknown fields survive round-trip via `extra`.
- `netboot_src` (URL cross-reference) is pixie's canonical way to
  advertise a matching netboot bundle from a disk-image entry.
  Legacy `netboot_ref` (name-string) is loose-parsed with a warning
  and dropped on write.

**Design departures from the trio:**

- **Content-addressed everything.** Blobs + artifacts key on their
  own sha256, not on the URL that fetched them. Renaming an entry
  never changes its blob URL; the same content shared across
  entries lives on disk once.
- **Session-only auth.** No bearer surface (withcache + nbdmux each
  carried one). Mutating routes require the pixie session cookie.
- **No misses, no auto-fetch, no cache-through, no warmer as a
  lifecycle stage.** Presence on disk IS readiness.

### Changed

`pixie-lab` (the deploy generator entry) remains a placeholder until
PR 3; `pixie` (the TUI entry) also remains a placeholder.

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
