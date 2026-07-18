# pixie

Pixie is a single-container bare-metal netboot appliance. It hosts a
catalog of disk images, serves them over HTTP + NBD, chains iPXE
targets into flash / netboot / interactive-TUI flows, and exposes an
operator UI + JSON API to bind machines to boot modes.

The design goal is one small daemon that covers the whole
DHCP-to-target-OS chain for a lab of appliances. An operator points
their DHCP at pixie's TFTP + iPXE endpoint, adds catalog entries with
source URLs, hits Fetch, and binds machines by MAC to what pixie
should serve next time each target PXEs.

## What pixie is for

- **CI-driven appliance labs.** Machines reflashed per test job,
  per new image, or on failure. Bind them once, let CI drive the
  boot cycle.
- **Netboot without persistent disks.** `nbdboot` streams the
  chosen image over NBD; root becomes an overlay-on-tmpfs. No
  local writes survive; every reboot lands on the same source
  image.
- **Interactive image picks.** `pixie-tui` boots the target into
  the pixie live env; an operator drives the wizard from the
  target's own console. Useful for one-off bring-up.
- **Inventory first.** `pixie-inventory` boots the target,
  collects disk + NIC info via the pixie CLI, POSTs it back, then
  exits to firmware. Prerequisite for the flash modes.
- **Predictable flashing.** `pixie-flash-once` and
  `pixie-flash-always` write the picked image to a serial-matched
  disk on every PXE (or exactly once, if the operator prefers).
- **OS-agnostic by design.** Linux, FreeBSD, Windows targets all
  netboot the same iPXE chain. macOS is out of scope.

## Contents

```{toctree}
:maxdepth: 2

quickstart
boot-modes
deployment
hardware-quirks
```
