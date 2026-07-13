"""pixie CLI (operator TUI).

Placeholder at 0.1.0: the real TUI, ported from bty-tui and adapted for
the pixie in-process API, lands in PR 3. This stub exists so the
``pixie`` console-script is a valid entry point at package-install
time.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Placeholder entry. Prints a hint and exits non-zero so a
    misdirected shell script doesn't quietly succeed."""
    argv = argv if argv is not None else sys.argv[1:]
    print(
        "pixie: TUI not implemented in 0.1.0.\n"
        "See PLAN.md; the interactive TUI ports from bty-tui in a later PR.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
