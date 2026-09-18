"""A render may never move a provenance stamp backwards.

WHY THIS TEST EXISTS (2026-09-17). A `cxp` twelve releases behind its plugin
ran a routine render and rewrote **54 provenance stamps backwards**,
0.118.0 → 0.108.1, across 58 files in the shared tenant (cp `393ad9c8`).
Nothing raised: every write was a legitimate render by a legitimate install,
and the damage presented as an engine bug rather than as version drift.

The stamp was never a silent signal — `render.py` writes the running version
into every file on every render, so the regression was visible in `git diff`
before the commit, and was committed through anyway. The detector existed;
the enforcement did not. This is the enforcement.

Both stamp formats are covered because both regressed: `Provenance: Version
<X>` (project CPs, written through the region splice) and
`Provenance: cp-engine v<X>` (fully-generated files like CLAUDE.md, written
whole). A guard matching one format would have left 19 of the 54 unprotected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cp_engine.sync import (
    _guard_provenance_regression,
    _provenance_version,
    _write_if_changed,
)


# ── the parser ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "line, expected",
    [
        ("Provenance: Version 0.118.0 | 2026-09-17", (0, 118, 0)),
        ("Provenance: cp-engine v0.118.0 | 2026-09-17", (0, 118, 0)),
        ("Provenance: Version 0.42 | 2026-06-30", (0, 42)),
        # A two-digit DOCUMENT version, not an engine version. Parsing this
        # as 1 would make every hand-authored doc look like a regression
        # against any engine >= 1.
        ("Provenance: Version 01 | 2026-07-16", (1,)),
        ("Provenance: cp-engine v0.0.0-golden | 2026-05-13", (0, 0, 0)),
        ("no stamp here at all", None),
    ],
)
def test_parses_both_stamp_formats(line: str, expected) -> None:
    assert _provenance_version(f"---\n{line}\n---\n") == expected


# ── the guard ─────────────────────────────────────────────────────────

def _doc(stamp: str, body: str = "content") -> str:
    return f"---\nProvenance: {stamp} | 2026-09-17\n---\n\n{body}\n"


@pytest.mark.parametrize("fmt", ["Version {}", "cp-engine v{}"])
def test_backwards_stamp_is_held(fmt: str, tmp_path: Path, capsys) -> None:
    """The 2026-09-17 shape, in both formats: 0.118.0 rewritten by 0.108.1."""
    path = tmp_path / "cp.md"
    existing = _doc(fmt.format("0.118.0"), "old content")
    outgoing = _doc(fmt.format("0.108.1"), "new content")

    result = _guard_provenance_regression(path, existing, outgoing)

    # The stamp is held at the higher version...
    assert fmt.format("0.118.0") in result
    assert fmt.format("0.108.1") not in result
    # ...but the real content still gets written. Refusing the whole file
    # would make a stale CLI unable to sync at all — a bigger outage than a
    # frozen stamp.
    assert "new content" in result
    # And the operator is told, at the moment it would have done damage.
    assert "backwards" in capsys.readouterr().out


@pytest.mark.parametrize(
    "old, new",
    [
        ("0.118.0", "0.120.2"),   # the normal case: forward
        ("0.120.2", "0.120.2"),   # equal: no-op
        ("0.99.0", "0.100.0"),    # numeric, not lexical — "99" < "100"
    ],
)
def test_forward_and_equal_pass_untouched(old: str, new: str, tmp_path: Path) -> None:
    outgoing = _doc(f"Version {new}")
    assert _guard_provenance_regression(
        tmp_path / "cp.md", _doc(f"Version {old}"), outgoing
    ) == outgoing


def test_unparseable_side_means_no_opinion(tmp_path: Path) -> None:
    """None must mean "no opinion", never "regression".

    A file with no stamp, or a stamp this parser does not recognise, must
    pass through. A guard that blocks what it cannot read would turn every
    unrecognised header into a silent content freeze.
    """
    outgoing = _doc("Version 0.108.1")
    assert _guard_provenance_regression(
        tmp_path / "cp.md", "no stamp at all\n", outgoing
    ) == outgoing


# ── the chokepoint ────────────────────────────────────────────────────

def test_full_write_path_is_guarded(tmp_path: Path) -> None:
    """The fully-generated path — no splice regions.

    19 of the 54 backwards stamps came through here (CLAUDE.md, _repo-*.md).
    This path never calls `_refresh_provenance_header`, so the whole body,
    stamp included, comes from the running engine.
    """
    path = tmp_path / "CLAUDE.md"
    path.write_text(_doc("cp-engine v0.118.0", "old"))

    _write_if_changed(path, _doc("cp-engine v0.108.1", "new"), splice_regions=())

    written = path.read_text()
    assert "v0.118.0" in written, "stamp regressed on the full-write path"
    assert "new" in written, "content was not written"


def test_dry_run_still_writes_nothing(tmp_path: Path) -> None:
    """`cxp status` must stay read-only even when the guard would fire."""
    path = tmp_path / "CLAUDE.md"
    original = _doc("cp-engine v0.118.0", "old")
    path.write_text(original)

    _write_if_changed(
        path, _doc("cp-engine v0.108.1", "new"), splice_regions=(), dry_run=True
    )

    assert path.read_text() == original
