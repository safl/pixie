"""FastAPI app wiring for pixie.

Application factory in :mod:`pixie.web.main` mounts every feature
router (catalog, machines, PXE, exports, TFTP, events) and the
session-cookie auth middleware; the templates + partials for each
UI page live under :mod:`pixie.web._templates`. Import this package
for the ``create_app`` factory; the runtime tools (uvicorn, health-
checks) live in the sibling ``main`` module.
"""
