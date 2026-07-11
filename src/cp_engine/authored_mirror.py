"""Reverse-mirror: render an AUTHORED spine element's DB rows to a `spine/
_authored/<slug>.md` file that `parse_substance` round-trips. The disk file is a
generated MIRROR of the MC-2-owned rows (origin='authored'); MC-2 is the source
of truth. Authored rows have est_item_kind=None in the DB; the file carries the
sentinel kind `context` (parse_substance requires a kind, and authored elements
are placement=context)."""
from __future__ import annotations

from pathlib import Path

from cp_engine.substance import (
    SubstanceVersion, WorkItemSubstance, render_substance,
)

_AUTHORED_KIND = "context"   # sentinel: parse_substance requires a kind


def _version_sort_key(row):
    # newest first: v-number descending. label like "v3".
    lbl = str(row.get("version_label", "v0"))
    return int(lbl[1:]) if lbl.startswith("v") and lbl[1:].isdigit() else 0


def write_authored_element(project_dir: Path, *, project_code: str,
                           est_item_id: str, rows: list[dict],
                           out_dir: Path | None = None) -> Path:
    """Render `rows` (an authored element's versions) to spine/_authored/<slug>.md.

    `out_dir` overrides the destination directory (the account-scope mirror
    writes to `<account-dir>/_stakeholders/` — same file format, different
    home). Returns the written path. `rows` may be in any order; versions are
    emitted newest-first (as render_substance expects)."""
    if not rows:
        raise ValueError("write_authored_element: no rows")
    ordered = sorted(rows, key=_version_sort_key, reverse=True)
    first = ordered[0]
    slug = est_item_id.split("/", 1)[1] if "/" in est_item_id else est_item_id
    if out_dir is None:
        out_dir = project_dir / "spine" / "_authored"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{slug}.md"

    versions = tuple(
        SubstanceVersion(
            label=str(r["version_label"]),
            date=str(r.get("version_date") or ""),
            status=str(r["status"]),
            framing=str(r.get("framing") or ""),
            sources=tuple(r.get("sources") or ()),
            body=str(r.get("body") or ""),
        )
        for r in ordered
    )
    # Exactly one version must be live (parse_substance enforces this on read).
    # A malformed DB state (zero/two live — e.g. an upstream caller skipped the
    # prior-live demote) must fail loud, caught by sync's best-effort wrapper
    # (logged warning), rather than silently writing a corrupt mirror file.
    n_live = sum(1 for v in versions if v.status == "live")
    if n_live != 1:
        raise ValueError(
            f"authored element {est_item_id!r} has {n_live} live versions "
            f"(expected 1); refusing to mirror"
        )
    item = WorkItemSubstance(
        est_item_id=est_item_id,
        est_item_kind=_AUTHORED_KIND,         # sentinel
        phase=first.get("phase"),
        binding=str(first.get("binding") or "unbound"),
        versions=versions,
        path=path,
        layer=first.get("layer"),
        placement=str(first.get("placement") or "context"),
        serves=tuple(first.get("serves") or ()),
        archived=bool(first.get("archived", False)),
    )
    text = render_substance(item)
    path.write_text(text)
    return path
