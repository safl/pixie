"""Sphinx configuration for pixie documentation."""

from __future__ import annotations

project = "pixie"
author = "Simon A. F. Lund"
copyright = f"2026, {author}"

extensions = [
    "myst_parser",
    "sphinx_copybutton",
]

myst_enable_extensions = [
    "deflist",
    "fieldlist",
    "tasklist",
    "linkify",
    "colon_fence",
]

# Auto-generate slug anchors for headings (h1-h3) so intra-page links
# like ``[machine record](#machine-record)`` resolve without a manual
# ``(machine-record)=`` label on every heading.
myst_heading_anchors = 3

source_suffix = {".md": "markdown"}
master_doc = "index"

templates_path = ["_templates"]
exclude_patterns: list[str] = ["_build", "Thumbs.db", ".DS_Store"]

# HTML output
html_theme = "furo"
# Furo renders html_logo in the sidebar header where html_title would
# otherwise sit. Leaving html_title empty suppresses the long-form
# wordmark there, since the index.md H1 already carries it on the
# landing page and the sidebar logo identifies the project elsewhere.
html_title = ""
html_static_path = ["_static"]
# Furo falls back to ``project`` for the brand-text span next to
# where the logo would sit; the index.md H1 is the wordmark on the
# landing page, so keep the sidebar name hidden until a logo
# graphic is available.
html_theme_options = {
    "sidebar_hide_name": True,
}

# LaTeX / PDF output - pdflatex with sane UTF-8 (inputenc utf8). Avoid
# exotic Unicode (arrows, box-drawing, em-dashes) in docs sources;
# smart quotes are fine.
latex_engine = "pdflatex"
latex_documents = [
    ("index", "pixie.tex", "pixie - bare-metal netboot appliance", author, "manual"),
]
latex_elements = {
    "papersize": "a4paper",
    "pointsize": "11pt",
}
