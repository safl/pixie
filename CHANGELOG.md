# Changelog

Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The
format captures what actually matters to an operator running pixie (the
`pixie-lab` PyPI package + `pixie` container): behaviour the operator
perceives, defaults that survived a `pip install -U`, and gates that
landed in CI.

Per-release commit history lives in `git log`; this file is the
operator-facing summary.

## [Unreleased]

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
