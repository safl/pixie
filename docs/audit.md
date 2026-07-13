<!--
Pre-port audit produced by a fable-model subagent on 2026-07-13, before
any pixie code was written. Purpose: itemise what to port verbatim from
bty / withcache / nbdmux, what to drop per the pixie design decisions
(see ../PLAN.md), what non-obvious runtime deps the pixie Dockerfile
has to carry, and what ambiguous items still need operator decision.

Line numbers reference the source trees at their state on 2026-07-13
(bty commit e3a45dd, withcache + nbdmux at their respective mains).
Kept in the repo for historical reference; not updated as PRs land.
-->

# pixie port audit

Read-only audit of the three source repos (bty, withcache, nbdmux) against the
locked pixie design: one container, one FastAPI app, one `state.db`, one admin
password, one `fetch` verb (download + sha256 + tar.gz unpack), session-only
auth, `netboot_src` URL cross-reference, content-addressed
`/artifacts/<sha256>/` bundle serving.

Line ranges from a snapshot at bty commit `e3a45dd`, withcache and nbdmux at
their HEAD trees.

## Cross-cutting picks (base-of-truth for duplicated modules)

Three modules exist as near-copies in every repo. When pixie folds them, adopt
bty's copies as the base:

- `_layout.html`: bty's is 788 lines; withcache/nbdmux are 246/244 line
  cut-downs of it (sub-nav, dashboard chrome, SSE wiring, brand-pill-home all
  live in bty's). Kill the trio-gradient brand strip
  (`withcache/_layout.html:32-39`) since there is one service now.
- `_events_log.py`: bty's is 418 lines and the most complete;
  withcache is 277, nbdmux is 243. Bty's `KNOWN_EVENT_KINDS` needs pruning
  (`blob.miss.*`, `export.warm.*`), and its docstring should be widened from
  "bty" to cover fetch + export + PXE.
- `_settings_store.py`: bty's 364 lines is the richest; withcache 100 and
  nbdmux 158 are subsets. Drop bty's cross-service URL keys
  (`KEY_WITHCACHE_URL`, `KEY_NBDMUX_URL`) since there are no sidecars to
  configure.
- `_table_state.py`: identical shape in all three (bty 234, others 152); bty's
  version is the base.
- `_auth.py`: bty's session-cookie form (87 lines) is exactly the pixie auth
  decision. Withcache and nbdmux both carry `check_bearer` alongside; keep
  bty's, drop the bearer half in both other repos.

There is ONE event log in pixie, not three. Emit `fetch.*`, `export.*`,
`machine.*`, `pxe.*`, `auth.*` from the same bus.

# bty

## 1. Port verbatim

**Machine registry + PXE plan renderer (the core)**

- `src/bty/web/_app.py:487-1000` `GET /pxe/{mac}`: race-safe discovery upsert,
  boot-mode decision tree, saw_flasher_boot loop-break. Port whole; the ramboot
  branch (695-822) needs its nbdmux-client lookup replaced by an in-process
  export query.
- `src/bty/web/_app.py:1095-1332` `GET /pxe/{mac}/plan` JSON plan. The
  withcache `blob_url` rewrite (1218-1266) becomes "pixie's own blob route".
- `src/bty/web/_app.py:1002-1093` `POST /pxe/{mac}/status`, `1334-1437`
  `POST /pxe/{mac}/inventory` (lshw cap), `1460-1561` `_arm_flasher_boot` +
  `GET/HEAD /boot/{name}` with `?mac=` arming, `1640-1951` machines CRUD +
  lshw/disks downloads, `475-486` `/pxe-bootstrap.ipxe`.
- `src/bty/web/_db.py:88-214` schema (machines, machine_labels, events,
  settings, version marker) + `254-351` init_db auto-rotate + open_db. Rename
  `bty_version` table + `bty_image_ref` column to pixie names.
- `src/bty/web/_models.py` (365 lines, all Pydantic wire models), `_reqctx.py`
  (client_ip, normalise_mac), `_labels.py`, `_security.py`, `_helpers.py`
  (`request_host` 79-100, `safe_path`/`serve_safe_file` 141-192, `seed_boot_dir`
  102-140, `boot_state` 244-308, `stream_upload` 309-361).

**Event log + SSE**

- `src/bty/web/_events_log.py` (418 lines: append-only audit log, search,
  acknowledge tripwire) and `_events.py` (181 lines: thread-safe SSE fan-out
  bus). Port whole as `pixie._events` / `pixie._events_log`; adopt as the sole
  event log for the merged process.
- `src/bty/web/_app.py:1589-1636` SSE endpoints `/events/machines`,
  `/events/workers`.

**iPXE templates** (all 7 under `src/bty/web/_templates/*.j2`, 285 lines total)

- `pxe_bootstrap.j2`, `ipxe_flash.j2`, `ipxe_sanboot.j2`, `ipxe_tui.j2`,
  `ipxe.j2`, `ipxe_unknown.j2` port verbatim. The `console=` and blacklist
  comments encode hardware lessons; keep them.
- `ipxe_ramboot.j2` ports with URL rework: `nbdmux_base + /artifacts/<export>/`
  becomes pixie's `/artifacts/<sha256>/{vmlinuz,initrd}`.

**Session auth** (matches the pixie decision exactly)

- `src/bty/web/_auth.py` (87 lines, one admin password, constant-time compare,
  session cookie). Port as `pixie.web._auth`.
- `src/bty/web/_app.py:366-376` SessionMiddleware wiring, 7-day sliding TTL.
- `src/bty/web/_ui.py:121-215` login/logout + NotAuthenticated redirect
  middleware.

**Operator UI**

- `src/bty/web/_ui.py` (2100 lines) + `_templates/ui/*.html` (18 files) +
  `_static/` (vendored Bootstrap/HTMX/sse.js). Machines/dashboard/events/
  machine_detail port near-verbatim; settings pages need pruning (cross-service
  URL rows go, see Drop).

**Config + settings**

- `src/bty/web/_config.py` (631 lines: TOML + env-override + tomlkit
  round-trip `save_value`). Rename `BTY_*` env prefix to `PIXIE_*`.
- `_settings_store.py` minus the withcache/nbdmux URL keys.

**Worker plumbing**

- `src/bty/web/_jobs.py` (286 lines, `_BaseAsyncManager`: queue/cancel/
  state-listener). Pixie's `fetch` verb wants exactly this base class.

**Netboot release fetch (live-env kernel/initrd/squashfs)**

- `src/bty/web/_releases.py` (387 lines: GitHub release-asset download +
  sha256-manifest verify, atomic rename), `_release_mgr.py`,
  `_routes_releases.py`, and the `/ui/netboot` page (`_ui.py:967-1040,1846-1904`).

**TUI**

- `src/bty/tui/_app.py` (2209 lines) + `tui/__init__.py`. Port whole as the
  `pixie` CLI. Baseline endorsed twice in memory; additive changes only.
  `_fetch_and_dispatch_plan` (721-804), auto mode (805-964), wizard screens
  (985-1523).

**Flash engine + live-env helpers**

- `src/bty/flash.py` (1973 lines: probe/plan/validate/execute, curl|decomp|dd
  pipelines with `oflag=direct conv=fsync`, sha256 tee-verify, efibootmgr
  registration).
- `src/bty/disks.py`, `src/bty/catalog.py` (657 lines: TOML catalog load, src
  canonicalise, `image_ref_for_src`; grow a `netboot_src` field parser here).
- `src/bty/images.py` (formats, arch detect, UnifiedImage).

**Deploy generator + TFTP**

- `src/bty/deploy.py` structure: steps UX (863-1143), prereqs (889-968),
  init/deploy/upgrade/purge/show-config mains (1144-2141). Port as
  `pixie-lab`. All four quadlet/compose emitters collapse into ONE
  container unit.
- `deploy/tftp/Containerfile` (tftpd-hpa + ipxe NBPs + custom-seed overlay)
  folds into the pixie Dockerfile.
- `src/bty/web/_sysconfig.py:169-265` raw-UDP RFC-1350 TFTP probe + `266-355`
  interface listing port verbatim.

## 2. Drop

- `src/bty/web/_withcache.py` (38 lines): cross-service blob-URL glue.
- `src/bty/web/_withcache_catalog.py` (226 lines): HTTP-polled catalog snapshot
  of a remote withcache. Consumers: `_app.py:215-228` (startup prime),
  `1953-1992` (`POST /admin/withcache/refresh`), the `Sync from withcache`
  buttons in `ui/settings.html` + `machine_detail.html`.
- `src/bty/web/_ramboot.py` (64 lines): nbdmux HTTP-client glue
  (`exports_by_src`). Also `_app.py:1766-1814` bind-time gate via
  `nbdmux.client.list_exports` and `1994-2018`
  `POST /admin/nbdmux/refresh`.
- `src/bty/web/_settings_store.py:51-60,185-243`: `KEY_WITHCACHE_URL`,
  `KEY_NBDMUX_URL` + resolvers. Also the Settings-page rows in `_ui.py:1595-1648`
  upstream and parts of `1776-1845` ramboot.
- `src/bty/deploy.py:89-207` (`_compose_yaml` 3-service topology), `446-484`
  (`_quadlet_withcache`), `485-544` (`_quadlet_nbdmux`), `545-570`
  (`_quadlet_bty_tftp`): single-container deploy replaces all of it. Same for
  `deploy/quadlet/{withcache,nbdmux,bty-tftp}.container` and
  `deploy/compose.yml`.
- `src/bty/web/_sysconfig.py:95-133` `tftp_status` via
  `systemctl is-active dnsmasq` / pgrep + `71-93` `running_in_container`
  sidecar-excuse logic. TFTP is in pixie's container; replace with an
  in-process liveness check.
- `pyproject.toml:19-42` deps `withcache>=0.13.1` and `nbdmux>=0.7.0`.
- `_app.py:2020-2059` `GET /images` `cached: False` field and the v0.60
  proxy-removal comments (`1579-1587`). Dead weight from withcache era.

## 3. Non-obvious runtime deps

**Server-side (pixie container):**

- `qemu-img` (qemu-utils): `images.py:448` `inspect_image` detail view;
  `flash.py:1836` size probe. Image-detail pages degrade without it
  (`docker/Dockerfile:31` already carries it).
- `zstd`, `xz`, `gzip` CLIs: `images.py:462-468` compressed-size listings for
  the UI detail modal.
- TFTP daemon + iPXE NBPs (`deploy/tftp/Containerfile`): `tftpd-hpa` + `ipxe`
  package (`undionly.kpxe`, `ipxe.efi`, `snponly.efi`), plus CI-built custom
  `ipxe.efi` with embedded chain script (built by
  `cijoe/scripts/bty_ipxe_build.py`, staged into `docker/seed/`,
  `BTY_BOOT_SEED_DIR`, copied by `_helpers.py:102-140`). udp/69 is
  privileged: bty-web container runs uid 1000 (`Dockerfile:60-63`); folding
  TFTP in means root, `cap_net_bind_service`, or an unprivileged-port trick.
  `--network=host` required.
- Writable `/var/lib/bty` (state.db, boot/, backups/): `_db.py:50`, Dockerfile
  VOLUME.
- `itsdangerous`, `python-multipart`, `tomlkit`, `uvicorn[standard]`
  (`pyproject.toml:72-88`). Session cookies silently break on a slim install
  without itsdangerous.

**Live env (bty-media image, NOT the container) - `pixie` TUI/flash needs:**

- `dd` (with `oflag=direct`, `conv=fsync`), `curl` (`--http1.1`), `zstd`,
  `xz`, `gzip`, `bzip2`, `qemu-img convert`, `sha256sum`, `tee` writing to
  `/dev/fd/N` (`flash.py:1341-1364`, needs a real `/dev/fd`), `sync`,
  `partprobe`, `udevadm settle`, `lsblk -J`, `efibootmgr` (optional, UEFI
  boot-entry registration `flash.py:800-932`), `lshw -json`
  (`tui/_app.py:300-372`), `systemctl reboot` (`tui/_app.py:2064-2079`). Root
  + raw block-device write access throughout.

# withcache

## 1. Port verbatim

**Blob store + fetch pipeline (the core keeper)**

- `Store` class, `src/withcache/server.py:462-756` -> `pixie.catalog._store`.
  Blob-on-disk keyed by sha256(normalised url), SQLite metadata (`cache.db`),
  `normalize`/`key_of`/`blob_path` (514-526), `get_blob`/`list_blobs`
  (529-539), `delete_blob` (593-598). Pixie merges this into the single
  `state.db`; schema at 487-511 ports minus the `misses` table + `hits`/
  `misses` counters.
- `Store.store_from_origin`, `server.py:600-756`: resume-on-truncation
  Range-retry download loop with sha256-while-streaming, cancel polling,
  atomic tmp -> blob rename, per-attempt `fetch_resolver` re-resolution (the
  ghcr.io 10-min-SAS lesson at 59-68). This IS pixie's `fetch` verb core;
  extend it with tar.gz unpack post-step.
- `DownloadManager` + `Job`, `server.py:762-957` -> `pixie.catalog._fetcher`.
  Bounded worker pool, enqueue-dedup (830-839), cancel (841-852),
  `.started/.completed/.cancelled/.failed` event emits (876-946), `close()`
  drain (817-828).
- `_oras_tag_moved`, `server.py:989-1029`: mutable-tag invalidation. Keep;
  this is revalidation of already-fetched content, not auto-fetch (only
  deletes; re-fetch stays operator-triggered).
- Helpers: `now_iso`/`human_size`/`parse_size`/`parse_headers`, `server.py:72-104`;
  `resolve_secret`, `server.py:118-135` (rename env to `PIXIE_SESSION_SECRET`).

**ORAS client**

- `src/withcache/oras.py` (whole file, 543 lines) -> `pixie.oras` verbatim.
  `parse_ref` (209-245), anonymous-token flow with WWW-Authenticate discovery
  fallback (283-365), `fetch_manifest` (368-396), `pick_image_layer` with the
  non-image-mediaType data-loss guard (407-471), `resolve_ref` (493-538), retry
  wrapper (248-280). Stdlib-only, no framework deps. Bty's `flash.py:38`
  already imports this, so pixie's TUI reuses the same module in-process.

**Catalog state**

- `src/withcache/server.py:138-405` -> `pixie.catalog._state`.
  `CatalogState` dataclass: persisted `catalog.toml` load/fetch/atomic-persist
  (223-304, 398-405), operator URL override with env-pin-wins (281-304),
  `add_oras_entry` (306-377), `delete_entry` (379-396).
- `_serialise_catalog`, `server.py:141-185`: hand-rolled TOML emitter. Locked
  change lands here: scalar-key allowlist at 164-174 emits `netboot_ref`; swap
  to `netboot_src`. The other two `netboot_ref` sites: `_api.py:302-312`
  (add-entry allowlist) and `client.py:224` (docstring). Withcache has NO
  resolution logic for `netboot_ref` today (round-trips the string opaquely,
  nbdmux does the pairing), so the rename is mechanical here.

**Catalog JSON API**

- Blob-serve: `_decode_blob_origin` (`_api.py:46-61`), `_serve_blob`
  (64-145, minus the miss-recording branch 93-119), route registration for
  `/b/<b64>/<name>` GET+HEAD (169-182). Streams 64 KiB chunks,
  `X-Withcache-Sha256` header (rename to `X-Pixie-Sha256`), Content-Length. No
  Range support on serve today; see Flag section.
- `GET /catalog` (215-268) including the sha256/size blob-row enrichment
  (240-259) that bty's ramboot bind-gate depends on.
- `POST /catalog/entries` (270-337), `POST /catalog/entries/{name}/download`
  (339-384, incl. `?force=1`), `DELETE /catalog/entries` (386-408),
  `_persist_catalog` (411-423). Strip the Bearer half of
  `_bearer_or_session_authed` (193-213); keep the session-cookie half.

**App factory + UI**

- `src/withcache/_app.py:57-72,75-205,240-267,271-275,279-318`: factory shape
  (`_build_jinja`, `create_app` incl. SessionMiddleware + app.state injection,
  `render`/`require_ui_auth`/`NotAuthenticated` handler, `/healthz`,
  login/logout).
- `/ui/catalog` with per-row download-state fold-in (552-638) +
  `_latest_job_by_url` (491-504).
- `/ui/events` + ack (433-489); `/ui/settings` log-level card (640-724).
- Admin forms: `catalog_refresh` (856-883), `catalog_set_url` (885-905),
  `catalog_add_oras` (907-924), `catalog_add_entry` (926-957) +
  `_promote_url_to_catalog` (734-773, rename the `misses-<sha12>` fallback
  name at 758-759), `catalog_delete_entry` (959-977),
  `catalog_download_entry` (979-1016), `cancel_entry` (829-854).
- `Auth` class, `server.py:408-456`, minus `check_bearer` (445-456).

Note: adopt bty's `_layout.html`, `_events_log.py`, `_settings_store.py`,
`_table_state.py`, `_static/` (identical across the trio) as the base. Only
withcache-specific templates (`catalog.html`, parts of `settings.html`,
`events.html` shape) port over.

## 2. Drop

- `_templates/ui/misses.html` (84 lines): misses page, killed by decision.
- `_app.py:506-550` `GET /ui/misses`: same.
- `_app.py:775-809` `POST /admin/fetch`: miss-promote, killed by decision.
- `_app.py:811-827` `POST /admin/dismiss`: killed by decision.
- `server.py:497-503` `misses` table + `record_miss` (561-582), `record_hit`
  (584-587), `dismiss` (589-591), `counts` miss half (545-549),
  `list_misses` (541-543), prior-miss carry-over in `store_from_origin`
  (736-755): whole miss/hit accounting apparatus, killed by decision.
- `_api.py:93-119` miss-record + `blob.miss.recorded` event on 404:
  cache-through vestige; pixie 404s plainly.
- `_api.py:156-167` `/blob?url=` legacy GET+HEAD routes: legacy pre-`/b/`
  shape; pixie starts clean.
- `server.py:445-456` `Auth.check_bearer` + `_api.py:193-213` Bearer branch:
  bearer surface killed by decision.
- `src/withcache/client.py` (287 lines): cross-service client for
  bty -> withcache HTTP; obsolete when both sides are one process. Bty's
  `_withcache_catalog.py:39` + `_ui.py:1224` imports die with the merge.
- `src/withcache/_shim.py`, `curlwithcache.py`, `wgetwithcache.py`, `shim/`
  (Zig source), `hatch_build.py`: the "ccache for HTTP artifacts" transparent
  curl/wget shim family is cache-through tooling for general lab downloads,
  unrelated to pixie's appliance mission.
- `examples/pull_oras_blob.py`: demo script.
- `deploy/Containerfile`, `compose*.yml`, `envvars.example`: superseded by
  pixie's single container + `pixie-lab`.
- Dashboard miss card + "Recorded misses" sanity pill,
  `_app.py:385-397` + `dashboard.html` miss references.
- `KNOWN_EVENT_KINDS` `blob.miss.recorded` / `blob.miss.dismissed`,
  `_events_log.py:80-81`.

## 3. Non-obvious runtime deps

Withcache is deliberately thin here:

- No subprocess calls at all in `src/withcache/`. The only `subprocess` use is
  in the curl/wget shims being dropped. ORAS is pure stdlib `urllib`
  (`oras.py`); no `oras` binary needed.
- Python deps (`pyproject.toml:22-37`): fastapi, uvicorn[standard], jinja2,
  python-multipart, itsdangerous. All shared with the other two services.
- Outbound HTTPS at runtime: catalog fetch (`server.py:250-279`, default
  `https://github.com/safl/nosi/releases/latest/download/catalog.toml` at 138)
  and origin/ORAS blob fetches. Container needs CA certificates
  (`python:3.12-slim` carries them; distroless/alpine base might not).
- Path/state assumptions under `--data-dir` (container `/data`,
  `Containerfile:24-30`): `blobs/`, `tmp/` (same filesystem as blobs;
  `os.replace` at `server.py:730` requires no cross-device rename), `cache.db`,
  `catalog.toml`, `catalog_url`, `session-secret` (0600, `server.py:132`).
  Runs as non-root uid 10001; nothing needs root.
- Env vars to rename: `WITHCACHE_ADMIN_PASSWORD`, `WITHCACHE_SESSION_SECRET`,
  `WITHCACHE_CATALOG_URL`, `WITHCACHE_LOG_LEVEL`.
- Disk sizing: `--max-bytes` cap refuses new fills, never evicts
  (`server.py:555-558`); pixie's volume must be sized for full images
  (multi-GiB each) plus `tmp/` holding one in-flight partial per worker.
- `curl` in the image only for the container HEALTHCHECK
  (`Containerfile:20-22, 36-37`).

# nbdmux

## 1. Port verbatim

**NBD supervisor (the crown jewel)**

- `server.py:1273-1456` `NbdServer` -> `pixie.nbd.NbdServer`. One `nbdkit`
  child per export, port allocation from `port_base` scanning 256 ports
  (`_allocate_port_locked`, 1432-1443), diff-sync `reload()` (1327-1341),
  terminate with escalation (1445-1456), and the exact nbdkit argv
  construction (1377-1407):
  `nbdkit -p <port> --ipaddr <bind> -f --newstyle -e <name> --filter=cow [--filter=partition] file file=<path> [partition=1]`.
  Filter-ordering and plugin-args-last comments encode hard-won nbdkit
  arg-parser knowledge; keep them.
- `server.py:1227-1248` `_file_looks_partitioned` -> `pixie.nbd`. MBR/GPT
  `0x55AA` sniff that decides the `partition=1` filter; safe-default-to-loud-
  failure rationale is correct.
- `server.py:1251-1270` `_port_available` -> `pixie.nbd`. Bind-probe port
  check.
- `server.py:84-143` `_derive_export_name` + `server.py:66-81` export-name
  validator/regex -> `pixie.nbd`. URL-basename -> sanitised `.img` export
  name; regex rationale (INI-section + filesystem safety) still applies to
  nbdkit `-e` names.

**Fetch pipeline mechanics** (extract from Warmer; do not inherit the
lifecycle stage)

- `server.py:1041-1167` `Warmer._fetch_and_decompress` -> the body of pixie's
  `fetch` verb. `curl | gunzip/zstd/xz > dest.inflight` kernel-pipe streaming,
  `.inflight` + `os.replace` atomicity, stat()-based decompressed-bytes
  progress watcher (1122-1137). Lift the mechanics; detach from the Warmer
  class.
- `server.py:1206-1221` `_decompressor_cmd` + `server.py:1170-1203`
  `WARMABLE_FORMATS` / `is_warmable_format` -> `pixie.fetch`. Format ->
  decompressor mapping. Pixie adds `tar.gz` as a first-class fetchable (unpack
  step) rather than the indirect netboot_ref path.
- `server.py:900-1039` `Warmer._fetch_netboot_bundle` -> `pixie.fetch` unpack
  step. `curl | tar -xzf - -C staging` streaming extract,
  `manifest.json`-as-truth-marker check (1016), staging-dir + `os.rename`
  atomic swap (1028-1031). Port the extract mechanics; the sibling-lookup-
  by-name plumbing around it goes (pixie fetches the tar.gz blob directly by
  `netboot_src` URL and extracts under `/artifacts/<blob-sha256>/`).

**Artifact serving**

- `_api.py:161-214` artifact path validation + the three `FileResponse` routes
  -> `pixie.artifacts`. Realpath-containment check (170-173) is the right
  shape; swap the `{name}` path segment for `{sha256}` and the validator for
  a hex-digest check per the locked design.

**Export routes and store**

- `_api.py:233-345` `POST /exports` + `_api.py:349-389`
  `DELETE /exports/{name}` -> `pixie.nbd` routes. Two-shape body validation
  (`file` xor `src_url`, 275-278), delete-unlinks-warm-created-but-not-
  operator-placed distinction (376-387). Strip the withcache-lookup block
  (326-331) and the bearer dep.
- `_api.py:131-151` + `216-229` `GET /exports`, `GET /export/{name}` with the
  `netboot_ready` filesystem-derived bool. In pixie this becomes an internal
  query (feeds the PXE plan renderer and TUI), not a cross-service API.
- `server.py:238-533` `Store`: schema, `upsert_export` conflict-clause,
  `set_status`, `_row_to_export`, schema-version rotate-on-mismatch (331-356)
  -> merge the `exports` table into pixie's single state.db. Pre-1.0
  rotate-don't-migrate policy matches pixie's stance.
- `server.py:1459-1517` `_ensure_probe_export` -> `pixie.nbd`, probably (see
  Flag). Guarantees nbdkit always serves something, making "STOPPED" a real
  signal; `nbdinfo nbd://host:10809/probe.img` is the one-command smoke test.
- `_app.py:167-199` lifespan with `run_lifecycle` flag: the start/stop +
  resume-pending-on-boot pattern is right for pixie's single app;
  resume-pending becomes resume-unfinished-fetches.
- `deploy/Containerfile`: the Ubuntu 26.04 pin rationale (nbdkit >= 1.44 for
  cow-safe multi-export; trixie's 1.42 silently corrupts) must survive into
  pixie's Dockerfile comments verbatim.

## 2. Drop

- `server.py:221-232` `Auth.check_bearer` + `_api.py:102-127` `control_authed`
  bearer branch: bearer surface, killed by decision.
- `src/nbdmux/client.py` (whole file): stdlib HTTP client that existed only so
  bty could drive nbdmux over HTTP; merge makes it a function call.
  `warm_export`'s Bearer plumbing (16-24, 72-81, 98-100) is doubly dead.
- `server.py:539-624` `_fetch_withcache_catalog`,
  `_lookup_withcache_entry_for_src`, `_lookup_withcache_entry_by_name`,
  `_lookup_withcache_format` + `_app.py:49-94` UI-side duplicate: cross-
  service catalog HTTP lookups; pixie's catalog is in-process.
- `server.py:665-687` `_resolve_withcache_url` + the `NBDMUX_WITHCACHE_URL`
  gate in `_api.py:294-302` and `_app.py:544-548`: the
  `/b/<b64(src)>/` route-through-withcache contract dissolves; pixie's fetch
  reads its own blob store.
- Warmer as a lifecycle stage: `server.py:719-898` (queue/condvar/thread,
  queued -> fetching -> decompressing -> ready state machine,
  `export.warm.*` event kinds), `Store.list_pending_exports` (486-490):
  "warming" concept, killed by decision. Pipeline mechanics survive inside
  `fetch` (section 1); staged-status vocabulary and re-enqueue semantics do
  not.
- `netboot_ref` name-string plumbing: `server.py:286` (schema column),
  `_api.py:326-331` (capture at register), `server.py:895-898` + `922-942`
  (sibling lookup by name). Replaced by `netboot_src` URL cross-reference;
  artifacts move from `<artifacts_dir>/<export-name>/` to content-addressed
  `/artifacts/<sha256>/`.
- `_app.py:377-822` all `/ui/*` pages + `/admin/*` form handlers, and
  `_templates/ui/*` + `static/*`: nbdmux's operator UI chrome duplicates
  bty-web's. Bty's is the base (see Cross-cutting picks). Export-status
  content (status pill + progress bar, `exports.html:113-128`) folds into a
  pixie page built on bty chrome.
- `_events_log.py`, `_settings_store.py`, `_table_state.py`: explicit shape-
  copies of bty's modules (each says so in its docstring); adopt bty's.
  nbdmux-specific settings keys (`withcache.url`, `withcache.browser.url`)
  are cross-service config that dissolves.
- `_app.py:115-133` `_NoopWarmer` / `_NoopNbdServer` stubs: test scaffolding
  tied to the dropped Warmer split.
- `deploy/` (Containerfile, compose.yml, envvars.example) as artifacts:
  superseded by pixie's single container; only the nbdkit-version rationale
  carries over.

## 3. Non-obvious runtime deps

- `nbdkit` >= 1.44 with the `file` plugin and `cow` + `partition` filters
  (`server.py:1377-1407`, Containerfile FROM comment). On Ubuntu the
  plugin/filters ship in the main `nbdkit` package. Older nbdkit silently
  corrupts under cow + named exports; pin the base image accordingly (Ubuntu
  24.04+; nbdmux container uses 26.04's 1.46). Without it every export spawn
  raises at `server.py:1416-1422`.
- `curl` (`server.py:978, 1091`): the entire fetch path is curl subprocesses,
  not Python HTTP. Also used by the container HEALTHCHECK. Missing -> every
  fetch fails with FileNotFoundError from Popen.
- `gzip` (`gunzip -c`), `zstd`, `xz-utils` (`server.py:1215-1220`): stdin ->
  stdout decompressors. Missing -> fetch of any compressed image fails at
  spawn. Overlaps with bty's live-env deps.
- `tar` with gzip support (`server.py:984`): netboot bundle extract. Base
  images have it; busybox tar would need checking.
- No `/dev/nbd*`, no kernel modules, no cap-adds, not root-required: nbdkit
  is a pure userspace TCP server (client side loads `nbd.ko`, not this
  container). `--network=host` in pixie is driven by TFTP/PXE, not NBD.
- Port range: one TCP port per export, `--nbd-port` base 10809, scanning up
  to base+256 (`server.py:1438`). Compose publishes only `10809:10809` today,
  which looks like a latent bug for the 2nd+ export under bridge networking;
  host networking in pixie makes it moot, but the "NBD port range" in pixie's
  published-ports doc must say `10809..10809+N`.
- `ca-certificates`: curl fetches over TLS when the source is HTTPS-direct.
- Path assumptions: `NBDMUX_DATA_DIR=/data` with `state.db`, `session-secret`,
  `images/`, `artifacts/` beneath it; separate `/images` ro bind-mount
  convention for operator pre-placed files. Exports store absolute `file`
  paths in the DB, so pixie's data dir layout must stay stable across
  restarts or rows point at nothing.
- `time.sleep(0.2)` spawn-then-poll check (`server.py:1414-1416`): cheap
  liveness gate; means export registration has a built-in 200 ms latency per
  spawn.

# 4. Flag for me

Ambiguous items across the three repos. Each is a question, not a decision;
answer them and I can update the punch list.

**bty:**

- `src/bty/web/_backup.py` + `_portability.py` + `_routes_backups.py` +
  `ui/backups.html` (~1100 lines: scheduled export bundles, retention,
  import). Not mentioned in the pixie spec either way. Keep, or does one
  `state.db` + a docs'd `cp` replace it?
- `src/bty/web/_releases.py` GitHub fetch is a second "fetch" verb (live-env
  artifacts, not images). Does pixie's one-verb rule absorb it, or does the
  `/ui/netboot` fetch stay a separate surface?
- `PUT /boot/{name}` (`_app.py:2145-2158`, authed stream-upload of live-env
  artifacts): keep as the manual alternative to release fetch?
- `GET /catalog.toml` (`_app.py:2061-2143`) rewrites every src to withcache
  blob URLs; in pixie this becomes "rewrite to pixie's own blob route".
  Should the emitted manifest also carry `netboot_src` so a hand-pointed
  `pixie --catalog` sees bundles?
- `ipxe_ramboot.j2:19-27` fallback to bty-media-baked ramboot-init kernel
  when no netboot bundle exists: keep the dual path in pixie, or is
  image-native the only ramboot?
- `bty-media/` + `cijoe/` (live-env image bake, custom iPXE build, PXE chain
  test) are build-time tooling in a repo slated for archival. Where do they
  live post-pixie?
- `src/bty/images.py:127-146,202-303` local image-root dir-scan + sha
  sidecars: still used by the TUI's local mode (`tui/_app.py:438-459`) but
  nothing server-side. Port for the TUI only?
- `deploy.py:1096-1121` netavark firewall drop-in install: podman-networking
  workaround; still needed under `--network=host`?
- `machines.sanboot_drive` column + `_models.DEFAULT_SANBOOT_DRIVE`:
  BIOS-era drive numbering kept through the ipxe-exit rename. Carry into
  pixie or fold into the template default?
- `_jobs.py:77-85` docstring still names Hash/Download managers that no
  longer exist. Stale comments to not copy over.

**withcache:**

- `server.py:989-1029` + `_api.py:86-91` `_oras_tag_moved` deletes stale
  bytes on the serve path when a mutable tag moved, turning the request into
  a 404 until re-download. Keep as-is, or is delete-on-read too close to
  "auto" lifecycle for pixie? (Alternative: surface "tag moved" on the
  catalog page and let the operator force-refetch.)
- `server.py:141-185` `_serialise_catalog` silently drops unknown keys while
  `_api.py:315-320` hard-rejects them. With `netboot_src` landing and nosi's
  `gen_catalog.py` evolving, do you want pixie's emitter to preserve unknown
  keys so a nosi field pixie doesn't know yet survives the round-trip?
- `_api.py:135-145` blob serve has no HTTP Range support (full-stream only).
  The live env's `wget|dd` never needs it, but nbdmux/pixie-internal
  consumers or a resumed TUI download might. Decide before the wire contract
  freezes.
- `_api.py:236-259` `GET /catalog` filters to downloaded-only entries
  ("presence IS readiness"). Is that ready/pending-by-another-name and thus
  dead in pixie (everything in-process now), or does the TUI still want the
  downloaded-only view as its default?
- `server.py:306-377` `add_oras_entry` derives name/format/arch
  heuristically from the tag; overlaps with `_promote_url_to_catalog`
  (`_app.py:734-773`). Pixie probably wants one add-entry path; pick which
  heuristic survives.
- `server.py:465, 1049-1053` `--keep-query` cache-key knob: ever used in your
  deploys? If not, drop the flag and always strip query strings.
- `_app.py:503` comment admits `_latest_job_by_url` "latest" relies on dict
  insertion order matching append order. Works today; if pixie keeps it,
  worth an honest `max(key=job.id)`.
- `client.py:125-143` `is_healthy` backs bty's Settings reachability pill;
  dies with the merge, but confirm pixie's dashboard doesn't want an
  equivalent self-check row.
- `_settings_store.py:11-15` catalog URL override deliberately lives in a
  flat file (`catalog_url`), not the settings table. With one `state.db` in
  pixie, fold it into the settings store?
- `wgetwithcache` / `curlwithcache` are published as native Zig binaries via
  `hatch_build.py`. If anything in your lab scripts still exports
  `WITHCACHE_SERVER`, archiving the repo kills their upstream; confirm
  nothing outside the trio depends on the shims.

**nbdmux:**

- `_app.py:393-395` dashboard computes ready/pending/failed via
  `getattr(e, "status", None)` on dicts (always returns None), and filters
  for status `"pending"` which is not a state the machine ever sets (states
  are queued/fetching/decompressing/ready/failed). Latent bug; don't port.
  Confirms the dashboard counts were never right.
- `server.py:1459-1517` probe export: does pixie keep `probe.img` as the
  always-on smoke target? Predates pixie's design; occupies an export name
  + port.
- Artifacts GC: artifacts are deleted with their export
  (`_api.py:385-387`), but under pixie's content-addressed
  `/artifacts/<sha256>/` several exports may share one bundle. Who owns
  deletion? Refcount, or GC on catalog removal?
- `manifest.json` schema: nbdmux never parses it (existence check only,
  `server.py:1016`; served raw at `_api.py:204-214`). Does pixie's PXE
  renderer start reading fields from it (kernel version, sha256s), and is
  nosi's `netboot_bundle_pack` schema stable enough to depend on?
- `server.py:295` schema policy: rotate state.db on version mismatch relies
  on "operators re-POST their exports; bty-web does this automatically when
  a machine boots ramboot". In pixie the re-registration driver is internal;
  confirm the ramboot boot path still self-heals exports after a state.db
  reset, or fetched images become orphans.
- Cow overlay lifetime: `--filter=cow` keeps per-connection writes in
  nbdkit-internal overlay (tmp-backed). Multiple ramboot targets on one
  export share one nbdkit; confirm whether overlay-per-connection isolation
  is what bty relies on today, and where the overlay bytes land on disk
  (container tmpfs sizing for the pixie image).
- `server.py:363-418` `upsert_export` ON CONFLICT resets `bytes_done=0` /
  `error=NULL` and re-queues: re-fetching an already-ready export by
  re-POSTing the same name is the implicit "refresh image" story. Does
  pixie want an explicit re-fetch verb instead?
- `_api.py:24-28` docstring says read routes stay open "so bty polls without
  a session". In pixie `GET /exports` equivalents could go behind the
  session too. Do unauthenticated target machines (iPXE) ever hit anything
  beyond `/artifacts/*` and NBD itself?

**Cross-repo consumers to check before archival:**

- `nbdmux.client` and `withcache.client` are HTTP shims that only bty uses,
  but a grep over `~/git/nosi`, `~/git/boots`, and any lab-scripts tree is
  worth doing before archiving the source repos.
