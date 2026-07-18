"""Closed registry of event kinds pixie emits.

Every mutation carries an event with a well-defined identifier from
this module. Callers import the constant and pass it to
:meth:`EventsLog.emit`; the emit call rejects any string not in
:data:`KNOWN_EVENT_KINDS`. A new mutation site adds a constant here
first (with a docstring explaining what state changed and what
``subject_kind`` / ``subject_id`` / ``details`` carry), then wires it
into the call site -- never the other way around.

Naming convention: ``<subject>.<verb>[.<outcome>]`` in lower kebab
form. ``<subject>`` matches the ``subject_kind`` field on the row
so filtering ``kind LIKE 'catalog.%'`` and ``subject_kind =
'entry'`` gives roughly the same slice. ``<outcome>`` distinguishes
async terminal states (``.started`` / ``.done`` / ``.failed``);
synchronous verbs skip it.
"""

from __future__ import annotations

# ---------- catalog + fetch pipeline ---------------------------------
#
# Every catalog-store mutation emits one of these. ``subject_kind`` is
# always ``"entry"``; ``subject_id`` is the catalog entry name.

CATALOG_ENTRY_ADDED = "catalog.entry.added"
"""A new row landed via POST /catalog/entries, POST /ui/catalog/add,
or one of the entries in POST /ui/catalog/import. ``details.src``
carries the source URL so an operator can grep the log by URL."""

CATALOG_ENTRY_DELETED = "catalog.entry.deleted"
"""Row deleted via POST /ui/catalog/delete or DELETE /catalog/entries.
On-disk bytes are NOT touched here (see ``catalog.blob.deleted``)."""

CATALOG_ENTRY_UPDATED = "catalog.entry.updated"
"""A catalog entry's metadata was overwritten (Import overwrote an
existing name; a future edit-entry form lands here too). Fetch is
tracked separately via the ``catalog.fetch.*`` triple."""

CATALOG_FETCH_STARTED = "catalog.fetch.started"
"""Fetch pipeline kicked off. ``details`` carries the source URL +
whether this is an initial Fetch (``update = False``) or an Update
on an already-fetched entry."""

CATALOG_FETCH_DONE = "catalog.fetch.done"
"""Fetch pipeline finished successfully. ``details.content_sha256``
+ ``details.size_bytes`` name the new bytes."""

CATALOG_FETCH_FAILED = "catalog.fetch.failed"
"""Fetch pipeline errored. ``details.error`` carries the message."""

CATALOG_BLOB_DELETED = "catalog.blob.deleted"
"""On-disk bytes for an entry dropped via POST /ui/catalog/delete-blob.
Row stays with content_sha256 cleared so a subsequent Fetch re-runs
the pipeline. ``details.forced`` is True when the operator confirmed
through the in-use warning banner."""

CATALOG_IMPORT_OK = "catalog.import.ok"
"""POST /ui/catalog/import fetched a catalog.toml and upserted every
entry. ``details.url`` + ``details.count`` + ``details.new``
summarise the result; individual ``catalog.entry.added`` /
``.updated`` events fire per entry."""

CATALOG_IMPORT_FAILED = "catalog.import.failed"
"""POST /ui/catalog/import could not reach the URL or parse the TOML.
``details.error`` carries the exception message."""

# ---------- exports + NBD supervisor ---------------------------------
#
# ``subject_kind`` is always ``"export"``; ``subject_id`` is the export
# name (``pixie-<sha12>.img`` shape).

EXPORT_REGISTERED = "export.registered"
"""New export row via POST /exports; carries ``details.content_sha256``
+ ``details.nbd_port``."""

EXPORT_DELETED = "export.deleted"
"""Export row + running nbdkit torn down via DELETE /exports/<name>
or POST /ui/exports/delete."""

EXPORT_NBDKIT_SPAWNED = "export.nbdkit.spawned"
"""Supervisor spawned an nbdkit subprocess (fresh spawn or the
startup-time respawn walk). ``details.pid`` + ``details.nbd_port``."""

EXPORT_NBDKIT_EXITED = "export.nbdkit.exited"
"""Supervisor observed an nbdkit subprocess died out of band
(``_refresh_row`` noticed no live proc for a ``running`` row).
``details.previous_port`` + ``details.error`` explain."""

# ---------- machines + PXE ------------------------------------------
#
# ``subject_kind`` is always ``"machine"``; ``subject_id`` is the
# canonical MAC address.

MACHINE_DISCOVERED = "machine.discovered"
"""First-ever GET /pxe/<mac> for a MAC pixie has not seen before.
Emitted from ``machines_store.touch_seen`` when a new row is
created; subsequent hits do NOT emit (they update ``last_seen_at``
via the ``pxe.plan.rendered`` event instead)."""

MACHINE_BOUND = "machine.bound"
"""First bind for a MAC: PUT /machines/<mac> or POST
/ui/machines/bind. ``details.boot_mode`` +
``details.image_content_sha256`` capture the target state."""

MACHINE_BINDING_CHANGED = "machine.binding.changed"
"""Existing machine's ``boot_mode`` or ``image_content_sha256``
changed via the same route. Distinct from ``.bound`` so an
operator can filter for "any change" vs "first bind ever"."""

MACHINE_DELETED = "machine.deleted"
"""Row deleted via DELETE /machines/<mac> or POST
/ui/machines/delete."""

MACHINE_INVENTORY_UPDATED = "machine.inventory.updated"
"""POST /pxe/<mac>/inventory landed a new hardware inventory blob
from the live-env pixie CLI."""

# ---------- PXE plan render + status --------------------------------
#
# The renderer fires exactly one of ``pxe.plan.rendered`` /
# ``pxe.plan.unavailable`` per GET /pxe/<mac>. Status POSTs from the
# target's live env / nbdboot initrd flow through ``pxe.status.received``.

PXE_PLAN_RENDERED = "pxe.plan.rendered"
"""GET /pxe/<mac> emitted a bootable iPXE plan for a known
boot_mode. ``details.boot_mode`` names which mode; ``subject_kind``
= ``machine``, ``subject_id`` = mac."""

PXE_PLAN_UNAVAILABLE = "pxe.plan.unavailable"
"""GET /pxe/<mac> fell back to unavailable.j2 (missing binding,
missing bundle, missing live-env media, unknown mode).
``details.reason`` carries the human-readable explanation the
plan comment carries."""

PXE_STATUS_RECEIVED = "pxe.status.received"
"""POST /pxe/<mac>/status from the target initrd or live env.
``details.status`` is the raw status token
(``ramboot.up``, ``ramboot.nbd_connect_failed``, ``ramboot.die``, ...)."""

# ---------- TFTP subprocess supervisor ------------------------------
#
# No subject (``subject_kind`` = "") -- the TFTP process is per-pixie,
# not per-resource. Useful for the operator to prove udp/69 came up.

TFTP_STARTED = "tftp.started"
"""``pixie.tftp.TftpServer.start`` succeeded and ``in.tftpd`` is
listening on the resolved bind:port."""

TFTP_STOPPED = "tftp.stopped"
"""The tftp supervisor tore its subprocess down on shutdown."""

# ---------- auth ----------------------------------------------------
#
# ``subject_kind`` = "" (single-tenant; no per-operator identity).

AUTH_LOGIN_SUCCEEDED = "auth.login.succeeded"
"""POST /ui/login with the correct admin password."""

AUTH_LOGIN_FAILED = "auth.login.failed"
"""POST /ui/login with a wrong admin password."""


# The canonical closed set. Every kind above is registered here; the
# ``EventsLog.emit`` call rejects anything not in this frozenset.
KNOWN_EVENT_KINDS: frozenset[str] = frozenset(
    {
        CATALOG_ENTRY_ADDED,
        CATALOG_ENTRY_DELETED,
        CATALOG_ENTRY_UPDATED,
        CATALOG_FETCH_STARTED,
        CATALOG_FETCH_DONE,
        CATALOG_FETCH_FAILED,
        CATALOG_BLOB_DELETED,
        CATALOG_IMPORT_OK,
        CATALOG_IMPORT_FAILED,
        EXPORT_REGISTERED,
        EXPORT_DELETED,
        EXPORT_NBDKIT_SPAWNED,
        EXPORT_NBDKIT_EXITED,
        MACHINE_DISCOVERED,
        MACHINE_BOUND,
        MACHINE_BINDING_CHANGED,
        MACHINE_DELETED,
        MACHINE_INVENTORY_UPDATED,
        PXE_PLAN_RENDERED,
        PXE_PLAN_UNAVAILABLE,
        PXE_STATUS_RECEIVED,
        TFTP_STARTED,
        TFTP_STOPPED,
        AUTH_LOGIN_SUCCEEDED,
        AUTH_LOGIN_FAILED,
    }
)
