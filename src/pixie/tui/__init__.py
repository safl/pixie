"""pixie.tui - the pixie terminal interface.

Module name is historical (Rich-based wizard module); the
console script is ``pixie``. This module is intentionally
lightweight: it imports nothing from :mod:`rich` at module level
so an install without the ``[tui]`` extra can still ``import
pixie.tui`` for introspection without crashing. The actual Rich-
based app lives in :mod:`pixie.tui._app`, which is loaded only
when ``pixie`` is invoked.
"""

from __future__ import annotations

import argparse
import contextlib
import sys

import pixie

# Default ``--server`` value for the wizard. ``pixie`` is the
# canonical LAN-DNS / mDNS hostname operators are encouraged to point
# at their pixie server, so ``pixie --mac X`` against a fresh box Just
# Works without any flags. Owned here (the [tui]-extra-free entry
# module) so the argparse default and ``BtyTui``'s constructor default
# can both depend on it without the import dragging in Rich.
DEFAULT_SERVER = "pixie"


def main(argv: list[str] | None = None, *, prog: str = "pixie") -> None:
    """Console-script entry point for ``pixie``.

    Defers loading the Rich-based app until invocation time so a
    missing ``[tui]`` extra produces a clear "reinstall with extras"
    message rather than a raw ``ModuleNotFoundError``.

    The deploy-bootstrap surface lives in :mod:`pixie.deploy` (the
    ``pixie-lab init`` console script) so this script stays lean -- one
    job, one wizard.
    """
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            f"{prog}: flash images onto target disks, locally or via PXE. "
            f"Three modes:\n\n"
            f"  {prog}                          - interactive wizard\n"
            f"                                    (local image-root only)\n"
            f"  {prog} --catalog <URL>          - interactive wizard with\n"
            f"                                    the given catalog pre-loaded\n"
            f"                                    (equivalent to picking [c]\n"
            f"                                    on the source screen and\n"
            f"                                    typing the URL).\n"
            f"  {prog} --mac <MAC>              - server-driven mode:\n"
            f"                                    fetches a plan from\n"
            f"                                    --server's /pxe/<MAC>/plan\n"
            f"                                    and acts on it (auto-flash,\n"
            f"                                    interactive, or local-boot,\n"
            f"                                    whatever the server says).\n\n"
            "The operator-facing surface is intentionally narrow: in\n"
            "server-driven mode every knob (image, target disk, catalog\n"
            "overlay) comes from the pixie's machine record, not the\n"
            "cmdline. --catalog is only useful for hand-driven runs.\n\n"
            "To bootstrap a pixie + withcache + nbdmux container deploy,\n"
            "use the sibling ``pixie-lab init`` script (runnable via\n"
            "``uvx pixie-lab init`` without installing)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{prog} {pixie.__version__}",
    )
    parser.add_argument(
        "--server",
        type=str,
        default=DEFAULT_SERVER,
        help=f"pixie base URL or hostname. Default ``{DEFAULT_SERVER}`` "
        "(operator convenience: pair with a LAN DNS entry pointing at "
        "the pixie server and ``pixie --mac X`` just works). The netboot "
        "and USB-PXE paths pass this explicitly via ``pixie.server=`` "
        "on the kernel cmdline. Bare hostnames are accepted; missing "
        "scheme defaults to ``http://``.",
    )
    parser.add_argument(
        "--mac",
        type=str,
        default=None,
        help="Self-MAC of this client (e.g. ``aa:bb:cc:dd:ee:ff``). "
        "When supplied, pixie switches to server-driven mode: it "
        "GETs ``<server>/pxe/<mac>/plan`` and dispatches on the "
        "returned plan (auto-flash, interactive, or no-op). The "
        "live env passes this via ``pixie.mac=`` on the kernel cmdline.",
    )
    parser.add_argument(
        "--catalog",
        type=str,
        default=None,
        help="Catalog URL or path to pre-load (http(s):// for HTTP, "
        "oras:// for OCI, or a local path). When given, the SELECT_CATALOG "
        "screen is skipped and the wizard jumps straight to SELECT_IMAGE "
        "with this catalog overlaying the local image-root (equivalent "
        "to picking ``[c]`` on the source screen and typing the URL). "
        "Ignored in server-driven mode (``--mac`` set) because the server "
        "supplies the catalog as part of /pxe/<mac>/plan.",
    )
    args = parser.parse_args(argv)

    # Lifecycle progress -- the launch path has two slow phases an
    # operator stares at without feedback otherwise:
    #
    #   1. ``from pixie.tui._app import BtyTui`` (1-3s): pulls Rich
    #      + pixie.catalog + pixie.flash + withcache.oras into the
    #      interpreter. On slower hardware (low-end mini-PCs, EPYC
    #      bringup boxes) this is several seconds of "blinking
    #      cursor".
    #   2. ``BtyTui(...).run()`` -> the wizard prints its first
    #      header (Rich is no-alt-screen, so prior stderr output
    #      stays visible above the header). On the live env's
    #      framebuffer console first print is typically under a
    #      second after import.
    #
    # Print progress to stderr BEFORE the import + the run. The
    # operator sees: wrapper banner (from /usr/local/sbin/pixie-on-tty1
    # on the live env) -> these progress lines -> the pixie header.
    # The blank-screen window narrows to a few hundred ms while
    # Rich's Console initialises.
    #
    # Also mirror to ``/run/pixie.status`` so an operator who Alt-F2'd
    # to tty2 can ``cat`` it without having to read tty1's transient
    # output. ``/run`` is tmpfs on the live env so this is forgotten
    # on reboot; cheap to write.
    def _progress(msg: str) -> None:
        line = f"{prog}: {msg}"
        print(line, file=sys.stderr, flush=True)
        with contextlib.suppress(OSError), open("/run/pixie.status", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    _progress(f"v{pixie.__version__} starting...")
    _progress("loading UI dependencies (Rich)...")
    try:
        from pixie.tui._app import BtyTui
    except ImportError as exc:
        print(
            f"{prog} {pixie.__version__}: required dependency is not installed "
            f"({exc.name or exc}); reinstall with "
            '`pipx install "pixie-lab[tui]"`',
            file=sys.stderr,
        )
        sys.exit(1)
    _progress("dependencies loaded")
    if args.mac:
        _progress(f"server-driven mode: server={args.server} mac={args.mac}")
    _progress("starting interface (first paint may take a few seconds)...")

    # Lifecycle bookends, broadcast via /dev/kmsg + /dev/console
    # (the same fanout the chain-test markers + flash milestones
    # use). The pair lets an operator on IPMI SoL, on the kernel
    # serial log, or tailing ``journalctl -u pixie-on-tty1`` follow
    # along regardless of which pixie mode runs (auto-flash from a
    # plan, interactive wizard, USB-local). The mid-flight
    # markers (``auto-flash starting``, ``download NN%``,
    # ``write NN%``, ``flash complete; rebooting``) fire between
    # these bookends as they always have.
    #
    # The import lives here, not at module top, because the
    # missing-dep branch above must still produce a clean
    # "reinstall with extras" message instead of crashing on the
    # import itself; once we reach this line, ``_app`` is known
    # to import cleanly.
    from pixie.tui._app import _emit_console_marker

    _emit_console_marker(f"pixie: entered v{pixie.__version__}")
    try:
        BtyTui(server=args.server, mac=args.mac, catalog=args.catalog).run()
    finally:
        # ``finally`` so the marker fires for every exit path:
        # clean run, SystemExit from a sys.exit(N) deep inside
        # the wizard, KeyboardInterrupt, an unhandled exception,
        # and the post-flash ``_do_reboot`` (which returns
        # promptly before systemd kills us). Best-effort under
        # contextlib.suppress inside ``_emit_console_marker``;
        # never raises.
        _emit_console_marker(f"pixie: exiting v{pixie.__version__}")
