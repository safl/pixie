# Hardware quirks

Known board / firmware quirks that keep a target from booting
pixie's live env end-to-end, and the kernel cmdline tokens that
unblock them. Set the tokens via the Settings page
(`/ui/settings`, "Live-env" card, "Extra kernel cmdline") or via
`PIXIE_LIVE_ENV_EXTRA_CMDLINE` in `envvars`. Either path is
appended verbatim to the `pixie-live-env.j2` kernel line, after
`pixie.mac=` + `bty.mac=` so last-token-wins on any conflict.

The Settings page persists across restarts (state.db). The
envvars path is a compose-file pin for infrastructure-as-code
deploys. DB override wins when both are set.

An empty setting is the default and is the correct choice on
target hardware that boots without any workaround.

## When to reach for this page

Symptom pattern from the pixie access log:

    GET /pxe-bootstrap.ipxe                    200
    GET /pxe/<mac>                             200
    GET /boot/pixie-live-env/vmlinuz           200
    GET /boot/pixie-live-env/initrd            200
    # ...silence. No /boot/pixie-live-env/live.squashfs fetch.

iPXE handed the kernel + initrd off to Linux, then Linux couldn't
bring up the boot NIC (driver -EIO, DMA fail, ROM-BAR conflict,
etc). live-boot has no interface to run `fetch=` through and the
target hangs silently before any userspace runs.

Get to a serial console (BMC SoL, USB-serial adapter, or physical
COM1). If the console shows the kernel initialising, then falling
silent right after some driver's probe fails, this table is where
to start.

## Known-good tokens

### GIGABYTE MC12-LE0 (Ryzen server board, BIOS F06+)

    pci=realloc=on,nocrs

BIOS defect: the ACPI CRS advertises a PCI root window too small
to hold every enumerated device's BARs. The two onboard Intel
i210 NICs at `0000:06:00.0` and `0000:07:00.0` each want a
512 KiB BAR0 + 16 KiB BAR3 that the window can't fit; `pci_bus`
enumeration prints:

    pci 0000:06:00.0: BAR 0 [mem size 0x00080000]: can't assign; no space
    pci 0000:06:00.0: BAR 0 [mem size 0x00080000]: failed to assign

`ioremap()` returns NULL when `igb_probe()` tries to map the
device, the driver hands back `-EIO`, and the log shows:

    igb 0000:06:00.0: probe with driver igb failed with error -5
    igb 0000:07:00.0: probe with driver igb failed with error -5

`pci=nocrs` tells Linux to ignore the ACPI-provided PCI root
windows and compute its own; `pci=realloc=on` then rebalances
every device's BARs into the (larger) usable window. When the
workaround kicks in the kernel logs:

    pci 0000:06:00.0: working around ROM BAR overlap defect
    pci 0000:06:00.0: BAR 0 [mem 0xec300000-0xec37ffff]
    ...
    igb 0000:06:00.0: Intel(R) Gigabit Ethernet Network Connection
    igb 0000:06:00.0 enp6s0: igb: enp6s0 NIC Link is Up 1000 Mbps Full Duplex

...and the live-boot initrd fetches the squashfs normally.

## Target firmware prerequisites (not cmdline)

Two quirks are about the target's firmware / BMC rather than a kernel
token, but they produce the same "nbdboot doesn't work" report.

### UEFI network boot must be enabled in BIOS

nbdboot only happens if the target's UEFI actually PXE-boots. If the
board's network stack / PXE option-ROM is disabled in BIOS setup, or
the network device isn't in the boot order, the machine silently boots
its local disk instead - and, because that local install answers SSH,
it's easy to mistake for a working nbdboot. Some BMCs make this worse:
a one-shot **or** persistent "boot from PXE" override set over IPMI /
Redfish is accepted by the BMC (`bootparam get 5` shows `Force PXE`)
yet **ignored by the BIOS**, and the target never even appears in
pixie's access log. Seen on a Supermicro board whose BIOS network boot
had been turned off. Fix in BIOS setup directly: enable the UEFI
network stack + PXE, and put the NIC ahead of the local disk in the
boot order. Confirm success from pixie's side (a `GET /pxe/<mac>` in
the log, then `online.started … ramboot.up` status events), not from
SSH being open.

### Don't trust IPMI SOL to prove a boot

Some BMCs (e.g. the GIGABYTE MC12-LE0's Aspeed) do **not** mirror the
host serial console to IPMI SOL, so SOL stays blank even though the
target booted fine and `serial-getty@ttyS1` is running. Absence of a
login prompt on SOL is not proof of a failed boot on those boards.
Judge nbdboot by ground truth instead: root is `/dev/nbd0` (not the
SATA/NVMe disk), a live NBD socket to pixie's export port, and the
image's hostname rather than the local install's.

## Adding a row

If you hit new hardware, keep the row short. Reader wants:

1. Board + firmware version, in the heading.
2. The exact cmdline token(s).
3. The pixie-access-log or SoL-console signature that would
   send an operator to this page.
4. The kernel dmesg lines the workaround emits when it kicks
   in (so a future operator can confirm it's the right one).

Don't paste full dmesgs. Don't recommend disabling IOMMU / MSI /
ASPM unless every less-invasive alternative has been ruled out on
that specific board.
