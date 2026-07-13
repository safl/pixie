# Changelog

Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The
format captures what actually matters to an operator running pixie (the
`pixie-lab` PyPI package + `pixie` container): behaviour the operator
perceives, defaults that survived a `pip install -U`, and gates that
landed in CI.

Per-release commit history lives in `git log`; this file is the
operator-facing summary.

## [Unreleased]

## [0.1.0] - TBD

### Added

First release. Skeleton only: a FastAPI app with `/healthz`,
session-cookie login/logout, and a placeholder dashboard. No catalog,
no fetch, no exports, no PXE plan renderer, no TFTP, no TUI, no
deploy generator. Publishes to PyPI (`pixie-lab`) and ghcr.io
(`ghcr.io/safl/pixie`).

The intent of a 0.1.0 with nothing operator-usable in it is to lock
the package name, the container image name, the release pipeline, and
the shape of the FastAPI app before the port PRs land. See `PLAN.md`.
