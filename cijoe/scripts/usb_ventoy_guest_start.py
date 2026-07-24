"""
Start the Ventoy QEMU guest WITHOUT the is_up wait
==================================================

Thin wrapper around ``cijoe.qemu.wrapper.Guest.start()`` that returns
immediately after the guest is daemonised, skipping the upstream
``qemu.guest_start`` step's hardcoded 180-second ``is_up`` check
(which waits for the literal string ``login:`` on the serial console).

Why a local copy: Ventoy adds a chainload phase (menu -> grub ->
.iso -> initrd -> squashfs) on top of the pixie live env's normal
boot, which routinely pushes the time-to-login past the 180-second
ceiling baked into the upstream script. The YAML's
``core.wait_for_transport`` step already polls SSH readiness with
its own (configurable) timeout, so this script intentionally splits
"start the guest" from "wait for it to be reachable" -- the latter
is the only concern that needs a tunable timeout.

Retargetable: False (host-side; same constraints as the upstream
script).
"""

from __future__ import annotations

import logging as log
from argparse import ArgumentParser

from cijoe.qemu.wrapper import Guest


def add_args(parser: ArgumentParser):
    parser.add_argument("--guest_name", type=str, help="Name of the qemu guest.")


def main(args, cijoe):
    if "guest_name" not in args:
        log.error("missing argument: guest_name")
        return 1

    guest = Guest(cijoe, cijoe.config, args.guest_name)

    err = guest.start()
    if err:
        log.error(f"guest.start() : err({err})")
        return err

    log.info(f"guest '{args.guest_name}' started; readiness handled by wait_for_transport")
    return 0
