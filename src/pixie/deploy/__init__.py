"""pixie-lab: deploy generator + operator convenience CLI.

``pixie-lab init [dest]`` emits a ready-to-run compose deployment for
pixie: a ``compose.yml`` with one service on ``--network=host``, an
``envvars.example`` with the settings an operator MUST fill (admin
password, host address for LAN visibility), a ``data/`` scaffold and
a README pointing at ``PLAN.md``.

``pixie-lab deploy [dest]`` builds on init: auto-fills envvars with a
detected LAN IP + generated password, then runs ``podman compose
up -d`` and waits for ``/healthz`` to answer 200.

``pixie-lab purge [dest]`` tears the stack down (``podman compose
down -v``) so a fresh ``deploy`` on the same dir starts clean.

Kept deliberately shallower than bty-lab: pixie is one container, so
the whole file is measured in hundreds of LOC, not thousands. The
extra knobs (Quadlet emission, upgrade paths, backup / restore) land
in a follow-up if operators ask.
"""

from __future__ import annotations

from pixie.deploy._main import main

__all__ = ["main"]
