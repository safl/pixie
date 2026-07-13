"""pixie: bare-metal netboot appliance in one container.

See PLAN.md at the repo root for the design rationale, locked
decisions, and ordered roadmap.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("pixie-lab")
except PackageNotFoundError:
    # Not installed (development checkout without editable install).
    # Never raise on import; downstream code assumes ``pixie.__version__``
    # is always a string.
    __version__ = "0.0.0.dev0+unknown"

__all__ = ["__version__"]
