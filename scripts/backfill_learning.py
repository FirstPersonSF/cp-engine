"""Backfill learning from past projects (learning phase 7, 2026-11-11).

For the Closed and Archived workstreams, in order. Each step is opt-in;
with no step flag it only reports what it would do (a dry run).

  --ingest    ingest each project's Dropbox folder (the binding follows an
              archived folder). Turns `enable_dropbox` on where a folder is
              bound but ingest was off, so the run is not refused.
  --close     run the Close distillation (cp_engine.closing.run_close)
              against the LOCAL tenant checkout (--tenant): drafted report
              where there is material, lesson candidates, freelancer review
              requests. Commit the written reports afterwards.
  --publish   upload each drafted close-out report (wrap-report-*.md in the
              local tenant) to its project's Dropbox spine folder and ingest
              it, so MC-2 shows it on the job page.
  --dossiers  draft person lookouts (candidates) from the account-scoped
              stakeholder dossiers, Janet's first.

Needs SUPABASE_URL / SUPABASE_SERVICE_KEY (and ANTHROPIC_API_KEY for
--close/--dossiers, VOYAGE_API_KEY for embeddings) in the environment, e.g.
`railway run -s cp-engine -- python scripts/backfill_learning.py ...`.
Prints codes and counts only, never env values.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from supabase import create_client

from cp_engine.mc2_db import Tables

_DOSSIER_PROMPT = """\
From this internal stakeholder dossier, list the LOOKOUTS a teammate should
know before working with this person: how they decide, what they need to
see, what tends to go wrong, what to avoid. Only what the dossier supports;
never invent. 0–4 items, each one sentence, phrased as what to do or watch
for. Return ONLY a JSON list:
[{{"text": "...", "moments": ["<optional, from: {moments}>"], "evidence": "<the dossier line>"}}]

# Dossier — {name}
{body}
"""


def _client():
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("SUPABASE_URL / SUPABASE_SERVICE_KEY not in env")
    return create_client(url, key), url, key


def _llm():
    from cp_engine.closing import CLOSE_MODEL
    from cp_engine.plan_from_transcript import _call_claude

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY not in env")
    return lambda prompt: _call_claude(prompt, model=CLOSE_MODEL, api_key=key, timeout=240)


def _embed():
    if not os.environ.get("VOYAGE_API_KEY"):
        return None
    from cp_engine.lessons import voyage_embedder

    return voyage_embedder("document")


def finished_projects(client) -> list[dict]:
    rows = (client.table(Tables.PROJECTS)
            .select("id, number, code, full_job_name, mc_status, enable_dropbox, companies(code)")
            .in_("mc_status", ["Closed", "Archived"]).execute().data or [])
    bound = {r["project_id"] for r in (client.table("project_integrations")
             .select("project_id").eq("service", "dropbox").execute().data or [])}
    out = []
    for r in rows:
        company = ((r.get("companies") or {}).get("code") or "").lower()
        out.append({**r, "cp_code": f"{company}-{r['number']}" if company and r.get("number") else None,
                    "dropbox_bound": r["id"] in bound})
    return sorted(out, key=lambda r: r["full_job_name"] or "")


def material(client, pid: str) -> dict:
    def n(table, **eq):
        q = client.table(table).select("id", count="exact")
        for k, v in eq.items():
            q = q.eq(k, v)
        return q.limit(1).execute().count or 0

    return {"sources": n(Tables.RAG_ASSETS, project_id=pid, status="active"),
            "meetings": n(Tables.FATHOM_MEETINGS, project_id=pid),
            "spine": n(Tables.SPINE_SUBSTANCE, project_id=pid, status="live")}


def step_ingest(client, url, key, projects, apply: bool) -> None:
    from cp_engine import asset_ingest

    unbound = [p["cp_code"] or p["full_job_name"] for p in projects if not p["dropbox_bound"]]
    if unbound:
        # Never guessed (a folder comes from the MC-2 binding): bind these in
        # MC-2 and re-run.
        print(f"  {len(unbound)} with no Dropbox folder bound in MC-2: {', '.join(unbound)}")
    for p in projects:
        if not p["dropbox_bound"]:
            continue
        if not apply:
            print(f"  would ingest {p['cp_code']} (enable_dropbox={p['enable_dropbox']})")
            continue
        if not p["enable_dropbox"]:
            client.table(Tables.PROJECTS).update({"enable_dropbox": True}).eq("id", p["id"]).execute()
        asset_ingest._clear_listing_cache()
        try:
            run = asset_ingest.ingest_project_assets(
                p["cp_code"] or p["code"], mc_project_id=p["id"], supabase_url=url, supabase_key=key)
            print(f"  {p['cp_code']}: created {run.created}, versioned {run.versioned}, "
                  f"skipped {run.skipped}, failed {run.failed}"
                  + (f" ({run.unconfigured_reason})" if run.unconfigured_reason else ""))
        except Exception as exc:  # noqa: BLE001
            print(f"  {p['cp_code']}: FAILED {type(exc).__name__}: {str(exc)[:160]}")


def step_close(client, projects, tenant: Path, apply: bool) -> None:
    from cp_engine.closing import request_freelancer_reviews, run_close
    from cp_engine.render import ENGINE_VERSION

    llm = _llm() if apply else None
    embed = _embed() if apply else None
    for p in projects:
        if not p["cp_code"]:
            print(f"  skip {p['full_job_name']}: no company code/number")
            continue
        m = material(client, p["id"])
        enough = m["sources"] or m["meetings"] or m["spine"] > 2
        if not apply:
            print(f"  {p['cp_code']}: {m} -> {'report + lessons' if enough else 'reviews only'}")
            continue
        if not enough:
            out = request_freelancer_reviews(client, p["id"])
            print(f"  {p['cp_code']}: no material; reviews {out}")
            continue
        out = run_close(client, code=p["cp_code"], project_id=p["id"], tenant_root=tenant,
                        llm=llm, ingest=None, embed=embed, engine_version=ENGINE_VERSION)
        summary = {k: (v.get("ok"), v.get("queued", v.get("requested", v.get("error", ""))))
                   for k, v in out["steps"].items() if k != "ingest"}
        print(f"  {p['cp_code']}: report={out['report_path']} {summary}")


def step_publish(client, url, key, projects, tenant: Path, apply: bool) -> None:
    from cp_engine import asset_ingest
    from cp_engine.close_out import find_close_workdir
    from cp_engine.closing import publish_report

    connector = asset_ingest._dropbox_connector() if apply else None
    for p in projects:
        if not p["cp_code"]:
            continue
        try:
            workdir, _ = find_close_workdir(tenant, p["cp_code"])
        except Exception:  # noqa: BLE001 — no tenant dir, no report
            continue
        reports = sorted(workdir.glob(f"wrap-report-{p['cp_code']}-*.md"))
        if not reports:
            continue
        latest = reports[-1]
        if not apply:
            print(f"  would publish {p['cp_code']}: {latest.name}")
            continue

        def _ingest(report_file, p=p):
            from cp_engine.spine_promote import ingest_single_file

            res = ingest_single_file(str(report_file), p["id"], report_file.name,
                                     supabase_url=url, supabase_key=key)
            return {"ingested": bool(res)}

        out = publish_report(client, connector, project_id=p["id"], code=p["cp_code"],
                             report_file=latest, ingest=_ingest)
        print(f"  {p['cp_code']}: {out.get('dropbox_path') or out.get('note') or out.get('error')}"
              f" ingest={out.get('ingest') or out.get('ingest_error')}")


def step_dossiers(client, apply: bool) -> None:
    from cp_engine.lessons import known_lists, record_candidates

    rows = (client.table(Tables.SPINE_SUBSTANCE)
            .select("id, est_item_id, framing, body, company_id, project_id")
            .eq("layer", "Stakeholders").eq("scope", "account").eq("status", "live")
            .execute().data or [])
    rows.sort(key=lambda r: (0 if "janet" in (r.get("framing") or "").lower() else 1,
                             r.get("framing") or ""))
    print(f"  {len(rows)} account-scoped dossiers")
    if not apply:
        return
    llm, embed = _llm(), _embed()
    _types, moments = known_lists(client)
    for r in rows:
        name = (r.get("framing") or "").split(" - ")[0].split(" — ")[0].strip()
        body = (r.get("body") or "").strip()
        if len(body) < 200 or not r.get("company_id"):
            continue
        try:
            raw = llm(_DOSSIER_PROMPT.format(name=name, body=body[:12000], moments=", ".join(moments)))
            start, end = raw.find("["), raw.rfind("]")
            items = json.loads(raw[start:end + 1]) if start >= 0 else []
        except Exception as exc:  # noqa: BLE001
            print(f"  {name}: FAILED {str(exc)[:120]}")
            continue
        for it in items:
            it.update(scope="person", person=name)
        # Person lessons carry the company; record_candidates takes it from the project.
        out = record_candidates(client, items, project_code=None, project_id=r.get("project_id"),
                                source_kind="dossier", source_ref=r.get("est_item_id"), embed=embed)
        print(f"  {name}: queued {len(out['queued'])}, skipped {len(out['skipped'])}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--close", action="store_true")
    ap.add_argument("--dossiers", action="store_true")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--apply", action="store_true", help="write; otherwise a dry run")
    ap.add_argument("--only", default="", help="comma-separated cp codes (e.g. ibx-5192)")
    ap.add_argument("--tenant", default=str(Path.home() / "Documents/Python/cp"))
    # Drew, 2026-11-11: SNT 5194 BlackHat was a potential job that did not
    # happen — leave it alone.
    ap.add_argument("--skip", default="snt-5194", help="comma-separated cp codes to leave alone")
    args = ap.parse_args()
    client, url, key = _client()
    projects = finished_projects(client)
    if args.only:
        want = {c.strip().lower() for c in args.only.split(",")}
        projects = [p for p in projects if (p["cp_code"] or "") in want]
    skip = {c.strip().lower() for c in (args.skip or "").split(",") if c.strip()}
    projects = [p for p in projects if (p["cp_code"] or "") not in skip]
    print(f"{len(projects)} closed/archived projects; mode {'APPLY' if args.apply else 'DRY RUN'}")
    if args.ingest:
        print("ingest:")
        step_ingest(client, url, key, projects, args.apply)
    if args.close:
        print("close:")
        step_close(client, projects, Path(args.tenant), args.apply)
    if args.publish:
        print("publish:")
        step_publish(client, url, key, projects, Path(args.tenant), args.apply)
    if args.dossiers:
        print("dossiers:")
        step_dossiers(client, args.apply)


if __name__ == "__main__":
    main()
