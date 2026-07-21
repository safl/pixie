"""NBD-export supervisor + persistence.

An **export** is a name-plus-content_sha256 pair that pixie serves over
NBD. The bytes come from the catalog blob at
``<state_dir>/blobs/<content_sha256>/blob``; the name is a short
identifier the operator can type + iPXE can reference.

One nbdkit subprocess per export, one TCP port each, one filter chain
(``--filter=cow``, plus ``--filter=partition`` when the blob has an
MBR/GPT). Ports are allocated from a base + scan; the assigned port
lands on the export row so ``GET /exports`` surfaces it.
"""
