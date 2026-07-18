"""Console-script entry points for the pixie docs tooling."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _docs_root() -> Path:
    """Locate the docs root: the directory containing ``src/conf.py``.

    The commands are expected to run from inside ``pixie/docs/`` (the
    directory holding this ``tooling/`` package and the ``src/``
    Sphinx tree). Falls back to checking the parent in case the user
    invoked from ``pixie/docs/tooling``.
    """
    cwd = Path.cwd()
    for candidate in (cwd, cwd.parent):
        if (candidate / "src" / "conf.py").exists():
            return candidate
    sys.exit(
        "pixie-docs: could not find src/conf.py - run from the docs directory (e.g. pixie/docs)"
    )


def build_html() -> None:
    root = _docs_root()
    src = root / "src"
    out = root / "_build" / "html"
    subprocess.run(
        [sys.executable, "-m", "sphinx", "-b", "html", str(src), str(out)],
        check=True,
    )


def build_pdf() -> None:
    root = _docs_root()
    src = root / "src"
    latex_out = root / "_build" / "latex"
    subprocess.run(
        [sys.executable, "-m", "sphinx", "-b", "latex", str(src), str(latex_out)],
        check=True,
    )
    subprocess.run(["make"], cwd=latex_out, check=True)


def serve() -> None:
    root = _docs_root()
    src = root / "src"
    out = root / "_build" / "html"
    subprocess.run(
        [sys.executable, "-m", "sphinx_autobuild", "--port", "8000", str(src), str(out)],
        check=True,
    )
