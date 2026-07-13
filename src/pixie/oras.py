"""ORAS / OCI registry adapter for fetching disk images and other artifacts.

Resolves ``oras://`` references against an OCI v2 registry into a plain
HTTPS blob URL plus the auth headers the registry expects. The fetch
pipeline (``pixie.catalog._fetcher``) calls in here when the operator
hits Fetch on an ``oras://`` catalog entry so the existing Range-resume
fetch loop can stream the bytes; library consumers use the same helpers
to validate catalog entries and pre-resolve content digests.

Hard-forked from withcache 0.13.3's ``withcache.oras`` on 2026-07-13.
Kept as stdlib-only (no ``requests``/``httpx`` dep) so a slim install
still gets ORAS resolution.

URL shape::

    oras://<host>[:port]/<owner>/<repo>[/<extra>]:<tag>
    oras://<host>[:port]/<owner>/<repo>[/<extra>]@sha256:<64-hex>

Digest-pinned references skip the manifest fetch (the digest IS the
address); tag references go through manifest resolution + layer pick.

Why ``oras://`` and not ``ghcr:``
---------------------------------

The ORAS spelling disambiguates from container references. A reader
who sees ``ghcr.io/safl/nosi/debian-sysdev:latest`` in a docs example
might reach for ``docker pull`` or ``podman run`` -- which would
fail and leave them confused, because nosi publishes disk-image
artifacts, not runnable container images. ``oras://`` is the
ecosystem term for OCI-Registry-As-Storage; an operator googling it
lands at oras.land which explicitly explains "store arbitrary
artifacts, not just containers". The ``://`` form also composes
with other registries -- ``oras://quay.io/...``,
``oras://registry.example.com:5000/...`` -- without per-registry
schemes.

.. _ORAS: https://oras.land/

Auth
----

Spec-compliant OCI v2 registries (GHCR included) return 401 on every
request even for public packages. Their ``/token`` endpoint
mints anonymous tokens on a plain credential-less GET. So the flow
is: hit ``https://<host>/token``, take the returned bearer, set
``Authorization: Bearer`` on the manifest + blob requests. No
registry login, no PAT, no secrets shipped.

The token endpoint is built from the URL's host (``ghcr.io`` ->
``https://ghcr.io/token``), which works for GHCR and any registry
that follows the same convention. Registries with non-standard auth
flows (private registries with custom realms, e.g.) would need the
proper ``WWW-Authenticate`` challenge dance instead -- noted as
future work; not needed for the homelab / nosi use case this module
ships for.

Layer picker
------------

A nosi manifest carries two layers: the ``.img.gz`` disk image and a
``.sha256`` sidecar. The picker drops layers whose
``org.opencontainers.image.title`` annotation ends in a known sidecar
suffix (``.sha256``, ``.sha512``, ``.sig``, ``.asc``, ``.pem``,
``.cert``, ``.sbom``, ``.att``, ``.json``), then takes the largest
remaining layer by declared size. Manifests with no useful
annotations fall through to the largest layer overall -- a reasonable
bet that the image bytes dwarf any metadata sidecar.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

ORAS_SCHEME = "oras://"

# Transient HTTP statuses worth retrying: 429 (rate limit, common on
# GHCR / Docker Hub under load) plus the gateway/server-blip 5xx range.
# Everything else (401/403 auth, 404 not-found, other 4xx) is permanent
# and raised immediately, since retrying would just stall the caller.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = 0.5  # seconds; exponential: 0.5, 1.0 between attempts

# Defensive ceiling on the metadata-fetch response body. Real-world
# OCI tokens are <2 KiB and manifests are <100 KiB; anything beyond
# this is either a misconfigured registry or a deliberately hostile
# response. Capping the read keeps a runaway registry from exhausting
# the host process's RAM on a tag resolve. Blob bytes do NOT go
# through this helper (they stream via the cache-host's resume loop
# with its own bounds).
_MAX_METADATA_BYTES = 10 * 1024 * 1024  # 10 MiB

# Accept type covers OCI v1 + Docker v2 manifest media types so the
# registry doesn't bounce us with a 406 if the package was originally
# pushed as a Docker manifest.
_MANIFEST_ACCEPT = (
    "application/vnd.oci.image.manifest.v1+json,"
    "application/vnd.docker.distribution.manifest.v2+json"
)

# Layer titles ending in any of these are non-image sidecars (sha
# sums, signatures, attestations); skip them when picking the image
# layer so a future ``oras attach`` on the same artifact doesn't
# silently start being served.
_SIDECAR_SUFFIXES = (
    ".sha256",
    ".sha512",
    ".sig",
    ".asc",
    ".pem",
    ".cert",
    ".sbom",
    ".att",
    ".json",
)

# Layer mediaTypes that are NEVER disk images. A manifest whose largest
# layer carries one of these is a Helm chart, a Cosign signature, an
# in-toto attestation, a SPDX SBOM, etc. Without this filter
# ``pick_image_layer`` would happily pick the Helm tarball or the
# signature blob and downstream consumers (e.g. ``bty.flash``) would
# write its bytes onto an operator's target disk (tar headers /
# signature bytes into the MBR). An operator typing ``oras://`` at a
# non-image artifact deserves a clear error, not corruption.
_NON_IMAGE_LAYER_MEDIA_TYPES = (
    # Helm OCI charts.
    "application/vnd.cncf.helm.chart.v1.tar+gzip",
    "application/vnd.cncf.helm.chart.provenance.v1.prov",
    # Cosign signatures.
    "application/vnd.dev.cosign.simplesigning.v1+json",
    "application/vnd.dev.cosign.artifact.sig.v1+json",
    # in-toto attestations.
    "application/vnd.in-toto+json",
    "application/vnd.dev.cosign.artifact.bundle.v1+json",
    "application/vnd.dsse.envelope.v1+json",
    # SBOMs.
    "application/spdx+json",
    "application/vnd.cyclonedx+json",
    # OCI empty descriptor (used by artifact references with no payload).
    "application/vnd.oci.empty.v1+json",
)


def _is_non_image_layer(layer: dict[str, Any]) -> bool:
    media_type = layer.get("mediaType")
    if not isinstance(media_type, str):
        return False
    return media_type in _NON_IMAGE_LAYER_MEDIA_TYPES


class OrasError(OSError):
    """Raised on parse / resolution / fetch errors against an OCI registry.

    Inherits from :class:`OSError` so it's caught by callers that
    handle remote-I/O failures generically -- semantically the same
    family as :class:`urllib.error.URLError`, which also subclasses
    :class:`OSError`. Code paths that need to distinguish ORAS-
    specific failures from arbitrary network errors still can:
    :class:`OrasError` is a strict subclass."""


@dataclass(frozen=True)
class OrasRef:
    """Parsed ``oras://`` reference.

    Exactly one of ``tag`` / ``digest`` is set. ``digest`` references
    skip the manifest fetch (the digest is content-addressed, so the
    blob URL is fully determined). ``tag`` references go through the
    manifest to resolve a layer digest first.
    """

    host: str  # e.g. "ghcr.io" or "registry.example.com:5000"
    repository: str  # e.g. "safl/nosi/debian-sysdev"
    tag: str | None = None
    digest: str | None = None

    @property
    def manifest_locator(self) -> str:
        """Value used in the ``/manifests/<X>`` URL path."""
        if self.digest is not None:
            return self.digest
        assert self.tag is not None, "OrasRef must have either tag or digest set"
        return self.tag


# Host: DNS hostname (or registry.example.com:5000 with optional port).
# Repository: lowercase alnum + ``/_.-``, must contain at least one
# ``/`` after the host (owner + repo). Tag: OCI tag charset (alnum +
# ``._-``). Digest: only sha256 today; future algorithms would need
# extending. Layout overall::
#
#     <host>[:port]/<repo>(:<tag>|@sha256:<hex>)
#
# applied to the body after stripping the ``oras://`` scheme.
_REF_RE = re.compile(
    r"^"
    r"(?P<host>[a-zA-Z0-9][a-zA-Z0-9.-]*(?::[0-9]+)?)"
    r"/"
    r"(?P<repo>[a-z0-9][a-z0-9/_.-]*)"
    r"(?:(?:@(?P<digest>sha256:[0-9a-f]{64}))"
    r"|(?::(?P<tag>[A-Za-z0-9._-]+)))"
    r"$"
)


def parse_ref(ref: str) -> OrasRef:
    """Parse an ``oras://`` reference into a :class:`OrasRef`.

    Accepts the two canonical forms::

        oras://<host>/<owner>/<repo>[/<extra>]:<tag>
        oras://<host>/<owner>/<repo>[/<extra>]@sha256:<64-hex>

    Raises :class:`OrasError` on any malformed input. The repository
    component must contain at least one ``/`` -- a bare top-level
    path like ``oras://ghcr.io/nosi:latest`` is rejected because OCI's
    URL scheme requires owner+repo under the host.
    """
    if not ref.startswith(ORAS_SCHEME):
        raise OrasError(f"not an oras:// reference: {ref!r}")
    body = ref[len(ORAS_SCHEME) :]
    if not body:
        raise OrasError(f"empty oras:// reference: {ref!r}")
    match = _REF_RE.match(body)
    if match is None:
        raise OrasError(
            f"malformed oras:// reference {ref!r}: "
            f"expected oras://<host>/<owner>/<repo>:<tag> or "
            f"oras://<host>/<owner>/<repo>@sha256:<hex>"
        )
    repo = match.group("repo")
    if "/" not in repo:
        raise OrasError(
            f"oras:// reference must include <host>/<owner>/<repo>: "
            f"{ref!r} (got bare repo {repo!r} after host)"
        )
    return OrasRef(
        host=match.group("host"),
        repository=repo,
        tag=match.group("tag"),
        digest=match.group("digest"),
    )


def _urlopen_retry(req: urllib.request.Request | str, *, timeout: float) -> bytes:
    """GET ``req`` and return the body bytes, retrying transient
    failures with exponential backoff.

    Retries on :data:`_RETRYABLE_STATUS` (429 / 5xx) and on raw
    connection errors / timeouts. Permanent HTTP errors (401/403/404,
    other 4xx) raise immediately -- a retry can't fix an auth or
    not-found failure, only delay the inevitable. After
    :data:`_RETRY_ATTEMPTS` the last error is re-raised so the caller's
    ``except`` (which wraps into :class:`OrasError`) still fires.
    """
    last: BaseException | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                # Cap the read: a registry that returns a 500 MiB
                # "manifest" should fail loud, not silently consume
                # process RAM. ``read(N)`` returns up to N bytes; we
                # take N+1 and reject the response if it overflows.
                data = bytes(resp.read(_MAX_METADATA_BYTES + 1))
                if len(data) > _MAX_METADATA_BYTES:
                    raise OrasError(f"oras metadata response exceeded {_MAX_METADATA_BYTES} bytes")
                return data
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRYABLE_STATUS:
                raise
            last = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last = exc
        if attempt < _RETRY_ATTEMPTS - 1:
            time.sleep(_RETRY_BACKOFF * (2**attempt))
    assert last is not None  # loop ran at least once
    raise last


def parse_www_authenticate(header: str) -> dict[str, str]:
    """Parse an OCI ``WWW-Authenticate: Bearer realm="...",service="...",
    scope="..."`` challenge into a dict of its (lower-cased) params.

    Returns ``{}`` for a non-Bearer or unparseable challenge so callers
    can treat "no usable challenge" uniformly.
    """
    header = header.strip()
    if header[:7].lower() != "bearer ":
        return {}
    return {m.group(1).lower(): m.group(2) for m in re.finditer(r'(\w+)="([^"]*)"', header[7:])}


def _token_from_endpoint(url: str, host: str, repository: str, *, timeout: float) -> str:
    """GET a token endpoint and extract the bearer. Raises OrasError."""
    payload = json.loads(_urlopen_retry(url, timeout=timeout))
    # OCI registries return ``token``; some spell it ``access_token``.
    token = payload.get("token") or payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise OrasError(
            f"oras token response for {host}/{repository} did not contain "
            f"a token (keys: {sorted(payload.keys())})"
        )
    return token


def _discover_bearer_challenge(host: str, *, timeout: float) -> dict[str, str]:
    """Probe ``GET https://{host}/v2/`` and return the parsed Bearer
    challenge from the 401 response's ``WWW-Authenticate`` header.

    The spec-compliant way to find a registry's token endpoint when it
    isn't the GHCR-convention ``https://<host>/token`` (Docker Hub uses
    ``auth.docker.io``, e.g.). Returns ``{}`` if the registry answers
    200 (no auth) or sends no usable challenge."""
    req = urllib.request.Request(f"https://{host}/v2/", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return {}  # 200: no auth challenge to discover
    except urllib.error.HTTPError as exc:
        if exc.code != 401 or exc.headers is None:
            return {}
        return parse_www_authenticate(exc.headers.get("WWW-Authenticate", ""))
    except (urllib.error.URLError, TimeoutError, OSError):
        return {}


def fetch_anonymous_token(host: str, repository: str, *, timeout: float = 30.0) -> str:
    """Grab an anonymous bearer token for ``repository:pull``.

    Spec-compliant OCI v2 registries expose a token endpoint that mints
    short-lived anonymous bearers for public packages (no creds, no
    PAT). Two-step:

    1. Try the GHCR / oras.land convention ``https://<host>/token`` --
       one request for the common case.
    2. If that fails, fall back to the spec discovery: ping
       ``GET /v2/``, read the realm advertised in the ``WWW-Authenticate``
       Bearer challenge, and fetch from there. This covers registries
       whose token endpoint isn't ``<host>/token`` (Docker Hub ->
       ``auth.docker.io``, Quay's custom realm, etc.).
    """
    scope = f"repository:{urllib.parse.quote(repository, safe='/')}:pull"
    conv_url = f"https://{host}/token?service={host}&scope={scope}"
    try:
        return _token_from_endpoint(conv_url, host, repository, timeout=timeout)
    except (OSError, json.JSONDecodeError, ValueError, OrasError) as conv_exc:
        # Convention failed; try spec discovery before giving up.
        challenge = _discover_bearer_challenge(host, timeout=timeout)
        realm = challenge.get("realm")
        if realm:
            service = challenge.get("service", host)
            disc_url = f"{realm}?service={urllib.parse.quote(service, safe='')}&scope={scope}"
            try:
                return _token_from_endpoint(disc_url, host, repository, timeout=timeout)
            except (OSError, json.JSONDecodeError, ValueError, OrasError) as exc:
                raise OrasError(
                    f"oras token fetch failed for {host}/{repository} via discovered "
                    f"realm {realm}: {exc}"
                ) from exc
        # No usable discovery; surface the original conventional error.
        raise OrasError(
            f"oras token fetch failed for {host}/{repository}: {conv_exc}"
        ) from conv_exc


def fetch_manifest(ref: OrasRef, token: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """Fetch the OCI manifest for ``ref`` using a previously-acquired token.

    Returns the parsed JSON. Raises :class:`OrasError` on network or
    parse failure. The caller is responsible for layer selection.
    """
    locator = urllib.parse.quote(ref.manifest_locator, safe=":")
    url = f"https://{ref.host}/v2/{ref.repository}/manifests/{locator}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": _MANIFEST_ACCEPT,
        },
    )
    try:
        payload = json.loads(_urlopen_retry(request, timeout=timeout))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise OrasError(
            f"oras manifest fetch failed for "
            f"{ref.host}/{ref.repository}:{ref.manifest_locator}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise OrasError(
            f"oras manifest for "
            f"{ref.host}/{ref.repository}:{ref.manifest_locator} "
            f"is not a JSON object"
        )
    return payload


def _layer_title(layer: dict[str, Any]) -> str:
    annotations = layer.get("annotations")
    if not isinstance(annotations, dict):
        return ""
    title = annotations.get("org.opencontainers.image.title", "")
    return title if isinstance(title, str) else ""


def pick_image_layer(manifest: dict[str, Any]) -> dict[str, Any]:
    """Pick the disk-image layer from a (possibly multi-layer) manifest.

    Drops sidecar-looking layers by title-annotation suffix, then takes
    the largest by declared ``size``. A manifest with no usable
    annotations falls through to the largest layer overall -- the image
    bytes will dwarf any metadata blob in practice.

    Raises :class:`OrasError` if the manifest has no layers at all.
    """
    layers = manifest.get("layers")
    if not isinstance(layers, list) or not layers:
        # A multi-arch image *index* (``manifests`` instead of
        # ``layers``) is a common cause: a rolling tag that resolves
        # to an OCI index rather than a single artifact manifest. Name
        # it so the operator points at a concrete manifest/digest
        # instead of staring at "no layers".
        if isinstance(manifest.get("manifests"), list) and manifest["manifests"]:
            raise OrasError(
                "oras ref resolved to a multi-arch image index, not a single "
                "artifact manifest; reference a concrete platform manifest by "
                "its @sha256:<digest> instead of the index tag"
            )
        raise OrasError("manifest has no layers")

    # First pass: drop layers whose mediaType is definitively not a
    # disk image (Helm chart, Cosign sig, SBOM, attestation, ...).
    # Unlike title-suffix sidecars, these are never the right pick AND
    # never get a fallback; flashing a Helm chart's tar+gzip into a
    # disk's MBR is the data-loss scenario this guard exists for.
    rejected_media_types: list[str] = []
    image_candidates: list[dict[str, Any]] = []
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        if _is_non_image_layer(layer):
            mt = layer.get("mediaType")
            if isinstance(mt, str):
                rejected_media_types.append(mt)
            continue
        image_candidates.append(layer)
    if not image_candidates:
        if rejected_media_types:
            unique = ", ".join(sorted(set(rejected_media_types)))
            raise OrasError(
                f"oras ref resolved to a non-disk-image artifact "
                f"(layer mediaTypes: {unique}); refusing to serve "
                f"signature / Helm chart / SBOM blobs as disk images"
            )
        raise OrasError("manifest has no usable layers")

    # Second pass: among the image-eligible layers, prefer ones that
    # DON'T look like title-annotation sidecars (.sha256 / .sig / ...
    # filenames). If every remaining layer carries a sidecar-looking
    # title, fall back to "pick the largest" so the resolver gets a
    # chance to fail loudly downstream rather than this picker
    # mis-classifying.
    image_like: list[dict[str, Any]] = []
    for layer in image_candidates:
        title = _layer_title(layer)
        if title and any(title.endswith(suffix) for suffix in _SIDECAR_SUFFIXES):
            continue
        image_like.append(layer)
    candidates = image_like or image_candidates
    return max(candidates, key=lambda layer: layer.get("size") or 0)


@dataclass(frozen=True)
class ResolvedBlob:
    """Everything a fetcher needs to stream the image bytes.

    ``blob_url`` is the final ``/v2/<repo>/blobs/sha256:<digest>``
    endpoint. ``headers`` carries the bearer the registry expects. The
    ``digest`` is what the fetcher should verify the downloaded bytes
    against -- when the caller started from a tag, this is the digest
    the registry resolved to right now, frozen for the rest of the
    fetch.
    """

    blob_url: str
    headers: dict[str, str]
    digest: str
    size: int | None
    title: str | None


def resolve_ref(ref: str | OrasRef, *, timeout: float = 30.0) -> ResolvedBlob:
    """Resolve an ``oras://`` reference (or pre-parsed :class:`OrasRef`)
    to a :class:`ResolvedBlob`.

    For tag references: anonymous token -> manifest -> layer pick ->
    ``ResolvedBlob`` with the layer's content-addressed digest. The
    digest is frozen at resolve time so a tag that moves under us
    between resolve and fetch still produces the bytes we committed to.

    For digest-pinned references: anonymous token only; the blob URL is
    fully determined by the digest, the manifest fetch is skipped, and
    size / title come back as ``None`` (the descriptor's optional
    ``size_bytes`` field can carry that info instead if known).
    """
    if isinstance(ref, str):
        ref = parse_ref(ref)
    token = fetch_anonymous_token(ref.host, ref.repository, timeout=timeout)
    headers = {"Authorization": f"Bearer {token}"}

    if ref.digest is not None:
        digest = ref.digest
        size: int | None = None
        title: str | None = None
    else:
        manifest = fetch_manifest(ref, token, timeout=timeout)
        layer = pick_image_layer(manifest)
        raw_digest = layer.get("digest")
        if not isinstance(raw_digest, str) or not raw_digest.startswith("sha256:"):
            raise OrasError(
                f"picked layer for "
                f"{ref.host}/{ref.repository}:{ref.manifest_locator} "
                f"has unusable digest {raw_digest!r}"
            )
        digest = raw_digest
        layer_size = layer.get("size")
        size = layer_size if isinstance(layer_size, int) else None
        title = _layer_title(layer) or None

    blob_url = f"https://{ref.host}/v2/{ref.repository}/blobs/{digest}"
    return ResolvedBlob(
        blob_url=blob_url,
        headers=headers,
        digest=digest,
        size=size,
        title=title,
    )


def is_oras_url(url: str) -> bool:
    """True iff ``url`` is an ``oras://`` reference rather than http(s)://."""
    return url.startswith(ORAS_SCHEME)
