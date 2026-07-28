"""Mutation sentinels: a fast, gating slice of mutation testing.

Coverage proves a line RAN; it does not prove a test would FAIL if that
line were wrong. Full mutation testing (``make mutation`` / the weekly
``mutation`` CI workflow) proves the latter exhaustively, but it can't be
a per-PR gate: it is slow, its mutant IDs shift on every edit, and a
large share of its survivors are *equivalent* mutants (e.g. a mutated
type annotation, which is never evaluated) that no test can ever kill --
so a "zero survivors" bar is unsatisfiable.

This module is the gateable subset. It hand-picks the handful of
SAFETY-CRITICAL predicates in ``flash.py`` -- the ones that decide
whether a disk gets clobbered, whether a corrupt download is caught,
whether the right partition is made bootable -- flips each one, and
asserts the suite goes red. If a future change weakens a test until one
of these predicates is no longer guarded, THIS test fails the PR. It runs
in the normal (fast, unit) suite, so it is part of ``make ci``.

Mechanism: each sentinel patches ``flash.py`` on disk, runs its designated
killer test in a SUBPROCESS (fresh interpreter, so it imports the mutated
source without disturbing this process), asserts a non-zero exit, and
restores the file in a ``finally``. A missing/duplicated anchor means the
code drifted -- the sentinel errors loudly so it gets re-pinned rather
than silently passing.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FLASH = _REPO_ROOT / "src" / "pixie" / "flash.py"


@dataclass(frozen=True)
class Sentinel:
    name: str
    # Exact source substring to flip (must occur exactly once) ...
    old: str
    # ... and what to flip it to (a plausible, wrong variant).
    new: str
    # The unit test that MUST fail once the mutation is applied.
    killer: str


# Each entry is a real bug a careless edit could introduce, paired with
# the test that must catch it. Keep the killer tests unit-level so this
# stays in the fast suite.
SENTINELS: list[Sentinel] = [
    Sentinel(
        "size-fits check (image-larger-than-target) inverted",
        "plan.image.virtual_size_bytes > plan.target.size_bytes",
        "plan.image.virtual_size_bytes < plan.target.size_bytes",
        "test_validate_rejects_image_larger_than_target",
    ),
    Sentinel(
        "unrecognised-format guard inverted",
        "if plan.image.format is None:",
        "if plan.image.format is not None:",
        "test_validate_rejects_unrecognized_format",
    ),
    Sentinel(
        "gzip 4 GiB uncompressed-size wrap guard disabled",
        "if uncompressed < compressed:",
        "if False:",
        "test_parse_gzip_listing_wrap_returns_none",
    ),
    Sentinel(
        "on-the-wire integrity check inverted (mismatch no longer raises)",
        "if observed is not None and observed != expected:",
        "if observed is not None and observed == expected:",
        "test_verify_digest_mismatch_raises",
    ),
    Sentinel(
        "declared-digest normalisation drops lower-casing",
        "sha = sha.strip().lower()",
        "sha = sha.strip()",
        "test_normalize_digest_bare_hex_lowercased_and_prefixed",
    ),
    Sentinel(
        "HEAD Content-Length guard inverted (size mis-recorded)",
        "if parsed_len is not None:",
        "if parsed_len is None:",
        "test_probe_image_url_raw_img_sets_virtual_size",
    ),
    Sentinel(
        "ESP partition-type match inverted",
        '(child.get("parttype") or "").lower() != _ESP_TYPE_GUID',
        '(child.get("parttype") or "").lower() == _ESP_TYPE_GUID',
        "test_find_esp_partition_number_none_when_no_esp",
    ),
    Sentinel(
        "boot-entry label match loosened to a substring",
        # raw string: the source contains a literal backslash-t, not a tab
        r'm.group(2).split("\t", 1)[0].strip() == label',
        "label in m.group(2)",
        "test_boot_entries_with_label_matches_exact_label_only",
    ),
]


@pytest.mark.parametrize("s", SENTINELS, ids=lambda s: s.killer)
def test_mutation_is_caught(s: Sentinel) -> None:
    original = _FLASH.read_text()
    count = original.count(s.old)
    assert count == 1, (
        f"sentinel anchor for {s.name!r} occurred {count}x (expected 1); "
        f"flash.py drifted -- re-pin the anchor: {s.old!r}"
    )
    try:
        _FLASH.write_text(original.replace(s.old, s.new, 1))
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                f"tests/test_flash_unit.py::{s.killer}",
                "-q",
                "-x",
                "-p",
                "no:cacheprovider",
            ],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
        )
    finally:
        _FLASH.write_text(original)
    assert proc.returncode != 0, (
        f"MUTATION SURVIVED: flipping {s.name!r} did NOT make "
        f"{s.killer} fail. That predicate is no longer guarded by a test.\n"
        f"{proc.stdout[-1500:]}"
    )
