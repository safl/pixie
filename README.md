# pixie

Bare-metal netboot appliance in one container. Catalog + fetch + NBD +
PXE + TFTP + operator TUI, on one FastAPI process with one state.db and
one admin password.

Pixie consolidates into one process what bty (operator UI + machine
registry + PXE plan renderer + Rich TUI), nbdmux (NBD-export
multiplexer + netboot artifact serve), and a hard-fork of withcache
(catalog + fetch + blob store) implement today as three separate
FastAPI services. bty, nbdmux, and withcache all continue as their own
projects; pixie is a new sibling appliance that starts from a merged
design. See `PLAN.md` for the design rationale, locked decisions, and
ordered roadmap.

Nothing runs yet -- v0.1.0 is not tagged. This repo is under
construction; the container + PyPI package will publish on the first
tag. Track progress in `PLAN.md` and the design audit in
`docs/audit.md`.
