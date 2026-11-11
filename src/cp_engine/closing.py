"""Close a project: distil it before it leaves the active list (learning phase 4).

Drew, 2026-11-11: "Close should run the report and lessons, and archive is
really just closing the tools." So when a workstream's status becomes Closed,
MC-2 asks the webhook to run this, in order:

1. **Final ingest** — pull the project's remaining files into the source
   store while its folder is still where the binding says.
2. **Close-out report** — drafted from the `/cp-wrap` facts
   (`wrap_report.build_wrap`) against the same nine-section contract, written
   to the working dir as `wrap-report-<code>-<date>-v01.md`, marked
   DRAFTED by cp for the project owner to confirm or rewrite.
   The report is then uploaded to the project's Dropbox spine folder and
   ingested, so it is a source in MC-2 (the job page shows it) and searchable.
3. **Lesson candidates** — what the report's learning axes teach, queued in
   MC-2 as `candidate`s (source `wrap_report`) for the owner.
4. **Freelancer reviews** — one `requested` review per freelancer who
   logged hours on the project.

Every step is best-effort and reported: a failed ingest never stops the
report, a failed model call never stops the review requests. Nothing here
archives tools or touches search — Archive does that, separately.

The model call and the ingest are injected so the webhook passes its own
and tests pass fakes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from cp_engine.mc2_db import Tables

#: Same model the transcript planner uses.
CLOSE_MODEL = "claude-opus-4-7"

DRAFT_MARKER = "🤖 DRAFTED by cp"

_PROMPT = """\
You are drafting the close-out report for a finished First Person
engagement, from FACTS gathered from MC-2 and the tenant tree. The project
owner will confirm or rewrite it. Never invent a number, a budget, a margin,
a licensing status or a rating: the human-entry fields stay as visible
blanks. Say plainly when a fact is missing.

# Facts (JSON)
{facts}

# Exec Summary at close
{exec_summary}

# Write

Return ONE JSON object, nothing else:

{{
  "report_markdown": "<the report>",
  "lessons": [
    {{"text": "<what to do or watch for next time, one sentence>",
      "scope": "person | client | type | practice",
      "person": "<name, only for scope person>",
      "moments": ["<from: {moments}>"],
      "evidence": "<the fact in this report that teaches it>"}}
  ]
}}

The report has these sections, in order: Summary; Timeline & effort (say
hours are ALLOCATED, not timesheet actuals); What we delivered; What worked;
What was hard; What we learned about the client; What we learned about
freelancers and vendors; Scope & budget (what took longer than estimated,
what we would price differently); Not assessed (the human-entry fields, as
blanks to fill). Be specific and adversarial: a report that only records
wins is worth nothing.

Lessons: 0–8, only ones the facts support. Each is something to do
differently next time, not a summary. Person and client lessons are
internal and never reach the client.
"""


def _json_block(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        raise ValueError("model returned no JSON object")
    return json.loads(m.group(0))


def _exec_summary(workdir: Path) -> str:
    cp = workdir / "cp.md"
    if not cp.is_file():
        return ""
    text = cp.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"<!-- cp-engine:start exec-summary -->(.*?)<!-- cp-engine:end exec-summary -->",
                  text, re.DOTALL)
    return (m.group(1) if m else "").strip()[:6000]


def report_path(workdir: Path, code: str, today: date) -> Path:
    """`wrap-report-<code>-<date>-vNN.md`, the next free version."""
    n = 1
    while True:
        p = workdir / f"wrap-report-{code}-{today.isoformat()}-v{n:02d}.md"
        if not p.exists():
            return p
        n += 1


def write_report(workdir: Path, code: str, name: str, owner: str, body: str,
                 today: date, engine_version: str) -> Path:
    path = report_path(workdir, code, today)
    header = (
        "---\n"
        f"Project: {code} — {name}\n"
        f"Provenance: Version {path.stem.rsplit('-v', 1)[-1]} | {today.isoformat()}\n"
        f"Filename: {path.name}\n"
        f"Author: cp-engine (close-out draft, v{engine_version})\n"
        "---\n\n"
        f"> {DRAFT_MARKER} at Close — {owner or 'the owner'} to confirm or rewrite.\n\n"
    )
    path.write_text(header + body.strip() + "\n", encoding="utf-8")
    return path


def request_freelancer_reviews(client, project_id: str) -> dict:
    """One `requested` review per freelancer with hours on the project.
    Idempotent: a freelancer already reviewed (any status) is left alone."""
    rows = (client.table(Tables.SPRINT_ALLOCATIONS).select("entity_id, hours")
            .eq("project_id", project_id).execute().data or [])
    hours: dict[str, float] = {}
    for r in rows:
        if r.get("entity_id"):
            hours[r["entity_id"]] = hours.get(r["entity_id"], 0.0) + float(r.get("hours") or 0)
    ids = sorted(e for e, h in hours.items() if h > 0)
    if not ids:
        return {"requested": 0, "already": 0}
    people = (client.table(Tables.ENTITIES).select("id, is_external, kind")
              .in_("id", ids).execute().data or [])
    external = [p["id"] for p in people
                if p.get("is_external") and p.get("kind") in (None, "freelancer")]
    have = {r["entity_id"] for r in (client.table(Tables.FREELANCER_REVIEWS)
                                     .select("entity_id").eq("project_id", project_id)
                                     .execute().data or [])}
    todo = [e for e in external if e not in have]
    if todo:
        client.table(Tables.FREELANCER_REVIEWS).insert(
            [{"entity_id": e, "project_id": project_id, "status": "requested"} for e in todo]
        ).execute()
    return {"requested": len(todo), "already": len(external) - len(todo)}


def run_close(
    client,
    *,
    code: str,
    project_id: str,
    tenant_root: Path,
    llm: Callable[[str], str] | None,
    ingest: Callable[[], dict] | None = None,
    embed=None,
    today: date | None = None,
    engine_version: str = "",
    publish: Callable[[Path], dict] | None = None,
) -> dict:
    """Run the Close steps. Returns `{steps: {...}, report_path, lesson_ids}`
    with each step `ok` or its `error`. The caller commits any written file."""
    from cp_engine import close_out as _close
    from cp_engine import lessons as L
    from cp_engine import wrap_report as W
    from cp_engine.clock import tenant_today

    today = today or tenant_today()
    out: dict[str, Any] = {"code": code, "project_id": project_id, "steps": {},
                           "report_path": None, "lesson_ids": []}
    steps = out["steps"]

    # 1. Final ingest.
    if ingest is not None:
        try:
            steps["ingest"] = {"ok": True, **(ingest() or {})}
        except Exception as exc:  # noqa: BLE001
            steps["ingest"] = {"ok": False, "error": str(exc)[:300]}
    else:
        steps["ingest"] = {"ok": False, "error": "no ingest configured"}

    # 2–3. Report and lessons.
    workdir = None
    try:
        workdir, _parked = _close.find_close_workdir(tenant_root, code)
    except Exception as exc:  # noqa: BLE001
        steps["report"] = {"ok": False, "error": f"no working dir: {exc}"}
    if workdir is not None:
        notes: list[str] = []
        try:
            bundle, payload = W.build_wrap(client, code, workdir, today=today, warn=notes.append)
            if llm is None:
                raise RuntimeError("no model configured")
            _types, moments = L.known_lists(client)
            raw = llm(_PROMPT.format(
                facts=json.dumps(payload, default=str, indent=1)[:40000],
                exec_summary=_exec_summary(workdir) or "(none)",
                moments=", ".join(moments),
            ))
            draft = _json_block(raw)
            path = write_report(workdir, code, bundle.name or code, bundle.account_manager,
                                str(draft.get("report_markdown") or ""), today, engine_version)
            out["report_path"] = str(path.relative_to(tenant_root))
            steps["report"] = {"ok": True, "path": out["report_path"], "warnings": notes}
            if publish is not None:
                pub = publish(path)
                steps["publish"] = {"ok": not pub.get("error"), **pub}
            queued = L.record_candidates(
                client, list(draft.get("lessons") or []), project_code=code,
                project_id=project_id, source_kind="wrap_report",
                source_ref=f"close:{project_id}:{today.isoformat()}", embed=embed)
            out["lesson_ids"] = queued["queued"]
            steps["lessons"] = {"ok": True, "queued": len(queued["queued"]),
                                "skipped": queued["skipped"]}
        except Exception as exc:  # noqa: BLE001
            steps.setdefault("report", {"ok": False, "error": str(exc)[:300], "warnings": notes})
            steps.setdefault("lessons", {"ok": False, "error": "no report to learn from"})

    # 4. Freelancer reviews.
    try:
        steps["freelancer_reviews"] = {"ok": True, **request_freelancer_reviews(client, project_id)}
    except Exception as exc:  # noqa: BLE001
        steps["freelancer_reviews"] = {"ok": False, "error": str(exc)[:300]}
    return out


# ── Publish: the report where people work (Drew, 2026-11-11: "both") ───

#: Where generated work lands in a project's Dropbox: an initiative's bare
#: `Spine/` when the folder has one, else the job template's spine folder.
SPINE_DIRS = ("Spine", "03 Assets/06 Spine")


def project_dropbox_folder(client, project_id: str) -> str | None:
    """The project's bound Dropbox folder (path or shared link), or None —
    read through the bindings like every other folder read."""
    from cp_engine.mc2_bindings import fetch_binding_rows, hydrate_project_row

    grouped = fetch_binding_rows(client, project_ids=[project_id])
    row = hydrate_project_row({"id": project_id}, grouped.get(project_id, []))
    return row.get("mc_dropbox_folder_id") or None


def publish_report(client, connector, *, project_id: str, code: str, report_file: Path,
                   ingest: Callable[[Path], dict] | None = None) -> dict:
    """Upload the drafted report to the project's Dropbox spine folder, and
    ingest it so it is a source in MC-2 (the job page shows it) and
    searchable. `ingest` takes the LOCAL report: cp's own output is embedded
    directly (as meeting summaries are), so a project's curated
    `asset_ingest_folders` list — which may not include the spine folder,
    and is never widened — cannot leave it out. Never raises."""
    from cp_engine import project_sources as ps

    try:
        folder = project_dropbox_folder(client, project_id)
        if not folder:
            # Still a source in MC-2: ingest the local report.
            out = {"dropbox_path": None, "ingested": False,
                   "note": "no Dropbox folder bound in MC-2; not uploaded"}
            if ingest is not None:
                try:
                    out["ingest"], out["ingested"] = ingest(report_file), True
                except Exception as exc:  # noqa: BLE001
                    out["ingest_error"] = str(exc)[:300]
            return out
        subdir = SPINE_DIRS[1]
        try:
            probe = ps.dropbox_destination(connector, folder, SPINE_DIRS[0])
            if isinstance(probe, str):
                meta = connector.dbx.files_get_metadata(probe)
                if getattr(meta, "size", None) is None:
                    subdir = SPINE_DIRS[0]
        except Exception:  # noqa: BLE001 — no Spine/: the job layout
            pass
        pushed = ps.push_to_dropbox(connector, folder, report_file,
                                    dest_name=f"{subdir}/{report_file.name}", overwrite=True)
        if pushed.get("error"):
            return {"error": pushed["error"]}
        out = {"dropbox_path": pushed.get("dropbox_path"), "ingested": False}
        if ingest is not None:
            try:
                out["ingest"] = ingest(report_file)
                out["ingested"] = True
            except Exception as exc:  # noqa: BLE001
                out["ingest_error"] = str(exc)[:300]
        return out
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:300]}
