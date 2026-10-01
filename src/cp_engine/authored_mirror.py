"""Render a spine element's MC-2 rows to the markdown file `parse_substance`
round-trips. Every file under `spine/` is a generated MIRROR of MC-2 rows
(architecture step 4c): authored elements land at `spine/_authored/<slug>.md`,
distilled work-item elements at their row's `rel_path`. Authored rows have
est_item_kind=None in the DB; their file carries the sentinel kind `context`
(parse_substance requires a kind, and authored elements are placement=context)."""
from __future__ import annotations

from pathlib import Path

from cp_engine.substance import (
    SubstanceVersion, WorkItemSubstance, render_substance,
)

_AUTHORED_KIND = "context"   # sentinel: parse_substance requires a kind


def _version_sort_key(row):
    # newest first: v-number descending. label like "v3". The label string is
    # the tie-break, so two rows whose labels don't parse (or a fetch that
    # returns rows in a different order) still render the same bytes — the
    # generated-mirror guard reads ANY byte change as news.
    lbl = str(row.get("version_label", "v0"))
    n = int(lbl[1:]) if lbl.startswith("v") and lbl[1:].isdigit() else 0
    return (n, lbl)


def _shape_steps(steps: list[dict] | None) -> tuple[dict, ...]:
    """Order spine_steps rows by position and project to the mirror shape
    {position, title, status, date, note} — `step_date` renders as `date`."""
    if not steps:
        return ()
    ordered = sorted(steps, key=lambda s: s.get("position") or 0)
    return tuple(
        {
            "position": s.get("position"),
            "title": s.get("title"),
            "status": s.get("status"),
            "date": s.get("step_date"),
            "note": s.get("note"),
        }
        for s in ordered
    )


def render_element(*, est_item_id: str, rows: list[dict],
                   steps: list[dict] | None = None,
                   kind: str | None = None, path: Path | None = None) -> str:
    """The file text for one element's DB rows (pure — no IO).

    ``kind`` is the frontmatter ``est_item_kind``: the sentinel ``context`` for
    an authored element (its rows carry NULL), the row's own kind for a
    distilled work-item element. Raises ValueError unless exactly one version
    is live — a malformed DB state must fail loud, never mirror corrupt."""
    if not rows:
        raise ValueError("render_element: no rows")
    ordered = sorted(rows, key=_version_sort_key, reverse=True)
    first = ordered[0]
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
            f"element {est_item_id!r} has {n_live} live versions "
            f"(expected 1); refusing to mirror"
        )
    item = WorkItemSubstance(
        est_item_id=est_item_id,
        est_item_kind=kind or first.get("est_item_kind") or _AUTHORED_KIND,
        phase=first.get("phase"),
        binding=str(first.get("binding") or "unbound"),
        versions=versions,
        path=path or Path(f"{est_item_id}.md"),
        layer=first.get("layer"),
        placement=str(first.get("placement") or "context"),
        serves=tuple(first.get("serves") or ()),
        archived=bool(first.get("archived", False)),
        steps=_shape_steps(steps),
    )
    return render_substance(item)


def authored_slug(est_item_id: str) -> str:
    """``_authored/<slug>`` → ``<slug>``; a bare id is its own slug."""
    return est_item_id.split("/", 1)[1] if "/" in est_item_id else est_item_id


def write_authored_element(project_dir: Path, *, project_code: str,
                           est_item_id: str, rows: list[dict],
                           steps: list[dict] | None = None,
                           out_dir: Path | None = None) -> Path:
    """Render `rows` (an authored element's versions) to spine/_authored/<slug>.md.

    `out_dir` overrides the destination directory (the account-scope mirror
    writes to `<account-dir>/_stakeholders/` — same file format, different
    home). Returns the written path. `rows` may be in any order; versions are
    emitted newest-first (as render_substance expects).

    Writes only when the bytes change, so an unchanged element keeps its mtime
    (the Exec Summary staleness check reads spine mtimes as "work arrived").
    Sync's generated-mirror pass does NOT call this — it renders through
    `spine_mirror`, which also guards against hand edits; this is the
    single-element write a promote uses right after its MC-2 write."""
    if not rows:
        raise ValueError("write_authored_element: no rows")
    if out_dir is None:
        out_dir = project_dir / "spine" / "_authored"
    path = out_dir / f"{authored_slug(est_item_id)}.md"
    text = render_element(est_item_id=est_item_id, rows=rows, steps=steps,
                          kind=_AUTHORED_KIND, path=path)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
    return path
