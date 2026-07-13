"""pixie-lab (deploy generator).

Placeholder at 0.1.0: the real deploy generator, ported from bty-lab
and shaped for one container on ``--network=host``, lands in PR 3.
This stub exists so the ``pixie-lab`` console-script is a valid entry
point at package-install time.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    print(
        "pixie-lab: deploy generator not implemented in 0.1.0.\n"
        "See PLAN.md; init/deploy/purge ports from bty-lab in PR 3.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
