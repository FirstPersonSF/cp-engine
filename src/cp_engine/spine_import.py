"""One-time step-4c import: hand-written files in ``spine/`` → MC-2.

``spine/`` became a view rendered from MC-2 (architecture step 4c). Before
that, a person or a skill could drop a file there and the tree kept it — but
the disk→MC-2 reader skipped ``_authored/``, so MC-2 never saw it. The first
render would quarantine and remove every such file. This module finds them
and gives each a home:

* a **card** — a file under ``spine/_authored/`` that parses as a spine
  element (``parse_substance``) and whose ``est_item_id`` has no MC-2 row —
  is imported as an authored element: one ``spine_substance`` row per
  version, through the same row builder ``create_spine_element`` uses
  (`build_create_rows`), plus its steps. Idempotent: an element MC-2 already
  holds is skipped, so the import runs once however often it is invoked.
* a **document** — anything else the render would not produce (a working
  doc parked inside ``spine/``) is NOT an element; the caller moves it out of
  ``spine/`` to the same relative path under the workstream dir.

Also recovers cards from the region-edit quarantine, in case a render ran
before the import (the quarantine keeps the removed file verbatim).

``scripts/archive/step4c_import_spine_cards.py`` is the operator entry point (dry run
by default).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from cp_engine.authored_element import build_create_rows
from cp_engine.mc2_db import Tables
from cp_engine.spine_mirror import (
    GUARD_REGION,
    RENDER_SELECT,
    _is_exempt,
    plan_project_files,
)
from cp_engine.substance import WorkItemSubstance, parse_substance


@dataclass
class Handwritten:
    path: Path               # absolute
    rel: str                 # relative to the workstream's spine/
    kind: str                # "card" | "document"
    item: WorkItemSubstance | None = None
    reason: str = ""
    already_in_mc2: bool = False
    from_quarantine: Path | None = None
    words: int = 0
    rows: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)


def _fetch_rows(client, project_id: str) -> list[dict]:
    return (
        client.table(Tables.SPINE_SUBSTANCE)
        .select(RENDER_SELECT)
        .eq("project_id", project_id)
        .execute()
        .data
    ) or []


def classify(path: Path, rel: str) -> tuple[str, WorkItemSubstance | None, str]:
    parts = Path(rel).parts
    if len(parts) != 2 or parts[0] != "_authored":
        return "document", None, "not a top-level spine/_authored/ file"
    try:
        item = parse_substance(path)
    except Exception as exc:  # noqa: BLE001 — not an element file
        return "document", None, f"does not parse as an element ({exc})"
    if not str(item.est_item_id).startswith("_authored/"):
        return "document", None, f"est_item_id {item.est_item_id!r} is not authored-shaped"
    return "card", item, ""


def find_handwritten(client, *, project_id: str, project_dir: Path) -> list[Handwritten]:
    """Every ``spine/`` file a render from MC-2 would NOT produce (exempt
    families — snapshots, the legacy meeting history — excluded)."""
    spine = project_dir / "spine"
    if not spine.is_dir():
        return []
    rows = _fetch_rows(client, project_id)
    produced = {p for p, *_ in plan_project_files(rows, project_dir)[0]}
    held = {str(r["est_item_id"]) for r in rows}
    out: list[Handwritten] = []
    for md in sorted(spine.rglob("*.md")):
        rel_path = md.relative_to(spine)
        if md in produced or _is_exempt(rel_path):
            continue
        kind, item, reason = classify(md, rel_path.as_posix())
        text = md.read_text(encoding="utf-8", errors="replace")
        hw = Handwritten(path=md, rel=rel_path.as_posix(), kind=kind, item=item,
                         reason=reason, words=len(text.split()))
        if item is not None:
            hw.already_in_mc2 = item.est_item_id in held
        out.append(hw)
    return out


_QFILE_RE = re.compile(r"^- \*\*File:\*\* `(?P<rel>[^`]+)`", re.M)
_QBODY_RE = re.compile(r"## Edit that was replaced\n\n````markdown\n(?P<body>.*?)\n````", re.S)


def quarantined_cards(tenant_root: Path, workstream_rel: str) -> list[tuple[Path, str, str]]:
    """``[(quarantine file, spine-relative path, original text)]`` for files of
    this workstream's ``spine/_authored/`` that a render already quarantined
    and removed."""
    qdir = tenant_root / "exceptions" / "region-edits"
    if not qdir.is_dir():
        return []
    prefix = f"{workstream_rel.rstrip('/')}/spine/"
    out = []
    for q in sorted(qdir.glob(f"*--{GUARD_REGION}--*.md")):
        text = q.read_text(encoding="utf-8")
        m, b = _QFILE_RE.search(text), _QBODY_RE.search(text)
        if not (m and b) or not m.group("rel").startswith(prefix):
            continue
        out.append((q, m.group("rel")[len(prefix):], b.group("body") + "\n"))
    return out


def card_rows(item: WorkItemSubstance, *, project_id: str, project_code: str) -> list[dict]:
    """One ``spine_substance`` row per version of ``item``, built by the same
    builder ``create_spine_element`` uses, keyed to the file's own identity
    (est_item_id, labels, dates, statuses)."""
    rows = []
    for v in item.versions:
        (r,) = build_create_rows(
            project_id=project_id, project_code=project_code,
            label=v.framing, type_=item.layer or "context", body=v.body,
            serves=list(item.serves), now_iso=v.date or "1970-01-01",
            sources=list(v.sources),
        )
        r["est_item_id"] = item.est_item_id
        r["id"] = f"{project_code}/{item.est_item_id}/{v.label}"
        r["version_label"] = v.label
        r["version_date"] = v.date
        r["status"] = v.status
        rows.append(r)
    return rows


def step_rows(item: WorkItemSubstance, *, project_id: str) -> list[dict]:
    return [
        {
            "project_id": project_id,
            "est_item_id": item.est_item_id,
            "position": s.get("position") or i,
            "title": s.get("title"),
            "status": s.get("status") or "done",
            "step_date": s.get("date"),
            "note": s.get("note"),
        }
        for i, s in enumerate(item.steps, start=1)
    ]


def import_card(client, hw: Handwritten, *, project_id: str, project_code: str) -> str:
    """Write one card to MC-2. Returns ``"imported"`` or ``"already in MC-2"``.

    Re-checks MC-2 immediately before writing, so a second run (or a card
    that a hosted session created meanwhile) is a no-op — never a second
    copy, never an overwrite of rows a person has since edited."""
    assert hw.kind == "card" and hw.item is not None
    held = (
        client.table(Tables.SPINE_SUBSTANCE)
        .select("id")
        .eq("project_id", project_id)
        .eq("est_item_id", hw.item.est_item_id)
        .limit(1)
        .execute()
        .data
    ) or []
    if held:
        return "already in MC-2"
    rows = card_rows(hw.item, project_id=project_id, project_code=project_code)
    client.table(Tables.SPINE_SUBSTANCE).insert(rows).execute()
    steps = step_rows(hw.item, project_id=project_id)
    if steps:
        client.table(Tables.SPINE_STEPS).insert(steps).execute()
    return "imported"
