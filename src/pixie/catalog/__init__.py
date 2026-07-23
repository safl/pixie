"""Operator-curated image library.

Pixie's catalog holds two peer entry shapes: **disk images**
(``format`` = ``img.gz`` / ``img.zst`` / ``img.xz`` / ``img`` / ...)
that pixie can flash to a target disk or serve over NBD for nbdboot,
and **netboot bundles** (``format`` = ``tar.gz``) whose contents pixie
unpacks into a content-addressed artifacts directory (vmlinuz +
initrd + manifest.json) so image-native nbdboot has kernel + initrd
URLs to serve.

Disk-image entries can carry a ``netboot_src`` field pointing at the
sibling bundle by URL; the two are peer catalog entries rather than
one nested under the other, so a nbdboot-only workflow can fetch a
bundle standalone and never touch a disk image.

There is ONE verb: :func:`pixie.catalog.fetch.fetch`. Downloads +
sha256s the bytes; if the entry's ``format`` is ``tar.gz``, additionally
unpacks vmlinuz + initrd + manifest.json into
``<state_dir>/artifacts/<content-sha256>/``. Presence on disk IS
readiness; no ready/pending vocabulary, no auto-fetch, no misses page.
"""

from __future__ import annotations

from importlib.resources import files

# Default "Import catalog" + live-env TUI source. Points at PIXIE's own
# release copy of the curated catalog (a subset of nosi's, restricted to
# the netboot-capable images pixie tests + supports), NOT the upstream
# nosi catalog. Operators can still import the full nosi catalog by URL;
# this is just the convenience default.
DEFAULT_CATALOG_URL = "https://github.com/safl/pixie/releases/latest/download/catalog.toml"

# The same curated catalog shipped INSIDE the package, used to seed a
# fresh (empty) catalog on first start so a plain deploy comes up with
# the known-good set offline -- no network, no release dependency.
BUNDLED_CATALOG_RESOURCE = "catalog.toml"

# Startup seeding is gated on this env (default on). Tests + operators
# who want an empty catalog set it to ``0``. A one-shot settings marker
# (below) records that the seed already ran so a later restart never
# re-seeds after an operator curated the catalog down.
SEED_CATALOG_ENV = "PIXIE_SEED_CATALOG"
CATALOG_SEEDED_KEY = "catalog.seeded"


def bundled_catalog_bytes() -> bytes:
    """The curated ``catalog.toml`` shipped inside the package."""
    return (files("pixie.catalog") / BUNDLED_CATALOG_RESOURCE).read_bytes()
