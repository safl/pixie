"""Machine registry.

A **machine** in pixie is a MAC-keyed record of what pixie should
answer when a target PXEs. Discovery is implicit: the first time a
MAC hits ``GET /pxe/<mac>`` the row is created with the default boot
mode (``ipxe-exit`` -- chain out of iPXE, boot the local disk). An
operator later binds a machine to a catalog entry via the JSON API or
the operator UI, flipping ``boot_mode`` to ``ramboot``.

The bound image is referenced by content sha256 (not URL sha; not
name-string). Content addressing means a machine binding survives a
catalog rename or re-add as long as the content is the same, and the
same content shared across multiple entries lives on disk once.
"""

from __future__ import annotations
