"""Operator-curated image library.

Pixie's catalog holds two peer entry shapes: **disk images**
(``format`` = ``img.gz`` / ``img.zst`` / ``img.xz`` / ``img`` / ...)
that pixie can flash to a target disk or serve over NBD for ramboot,
and **netboot bundles** (``format`` = ``tar.gz``) whose contents pixie
unpacks into a content-addressed artifacts directory (vmlinuz +
initrd + manifest.json) so image-native ramboot has kernel + initrd
URLs to serve.

Disk-image entries can carry a ``netboot_src`` field pointing at the
sibling bundle by URL; the two are peer catalog entries rather than
one nested under the other, so a ramboot-only workflow can fetch a
bundle standalone and never touch a disk image.

There is ONE verb: :func:`pixie.catalog.fetch.fetch`. Downloads +
sha256s the bytes; if the entry's ``format`` is ``tar.gz``, additionally
unpacks vmlinuz + initrd + manifest.json into
``<state_dir>/artifacts/<content-sha256>/``. Presence on disk IS
readiness; no ready/pending vocabulary, no auto-fetch, no misses page.
"""

from __future__ import annotations

DEFAULT_CATALOG_URL = "https://github.com/safl/nosi/releases/latest/download/catalog.toml"
