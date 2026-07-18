# pixie-docs

Build and dev-server tooling for the pixie project documentation.

Provides three console scripts:

- `pixie-docs-serve` - live-rebuild dev server on `http://localhost:8000`.
- `pixie-docs-build-html` - one-shot HTML build to `docs/_build/html/`.
- `pixie-docs-build-pdf` - one-shot PDF build via LaTeX to
  `docs/_build/latex/pixie.pdf`.

## Install

```bash
pipx install ./docs/tooling
```

The PDF build additionally requires a LaTeX distribution (`texlive`
variants on Linux, `latexmk` via MacTeX on macOS).
