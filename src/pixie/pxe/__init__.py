"""PXE plan rendering + boot flow.

An operator points a target's DHCP + TFTP chain at pixie's
``/pxe-bootstrap.ipxe`` (fetched over HTTP by iPXE); the bootstrap
chain-loads ``/pxe/<mac>``, which returns the target-specific iPXE
plan derived from the machine's boot mode:

* ``ipxe-exit`` -> chain out of iPXE, boot the local disk.
* ``nbdboot`` -> image-native kernel + initrd from the artifacts
  directory of the image's netboot bundle, root over NBD from the
  auto-created export against the disk-image blob.

Both templates are content-addressed: every URL the plan emits
carries a sha256 as its cache-safe identifier, so a catalog rename
or bump-and-redeploy of pixie does not silently boot a different
image than the operator intended.
"""

from __future__ import annotations
