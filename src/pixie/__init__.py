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

# Default catalog URL. Pointed at nosi's rolling release so a fresh
# pixie -- on the web UI's import form + on the TUI's ``[d]``
# (default) source screen -- lands on a working set of images
# without the operator having to know where they come from. Nosi's
# catalog.toml pins refs to a dated tag so an import today is
# reproducible tomorrow (the ``:latest`` release rolls forward but
# each fetched catalog carries the tag it was cut against).
DEFAULT_CATALOG_URL = "https://github.com/safl/nosi/releases/latest/download/catalog.toml"

__all__ = ["DEFAULT_CATALOG_URL", "__version__"]
