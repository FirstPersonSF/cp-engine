"""Freelancers: how they worked out, their hours, and who to use next
(learning phase 5; mc-2 mig 218).

Drew, 2026-11-11:
- "keep track of which freelancers work out well and be able to recommend
  the right person based on history" → `freelancer_reviews` (one per
  freelancer per project, requested at Close) + `recommend`.
- "an agreed-upon number of hours with contractors before they start, and a
  way to ensure that they're tracking to those hours" → `contractor_hours`
  (`agreed`, newest counts; `actual` per invoice or Gusto timesheet) +
  `hours_status`.
- Actual hours come from the invoice, or Gusto timesheets for those paid
  through Gusto. Where neither has been recorded, a payable's amount ÷ the
  person's default rate gives an ESTIMATE, always labelled as one.

Freelancers are `entities` rows (`is_external`, kind `freelancer`); which
projects they worked on is already in `sprint_allocations`. Everything here
is internal: ratings and watch-outs about people never reach a client.

Row builders are pure (the hosted server does its own writes, so its
read/write split can see them); reads take a client.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from cp_engine.mc2_db import Tables

RECOMMEND = ("yes", "with-reservations", "no")
HOUR_KINDS = ("agreed", "actual")
HOUR_SOURCES = ("agreement", "invoice", "gusto", "manual")

#: Share of agreed hours at which a contractor is flagged as close.
NEAR_SHARE = 0.8


class FreelancerError(ValueError):
    """What the database would refuse, said plainly."""


def review_patch(*, rating: int, recommend: str, strengths: str = "", watch_outs: str = "",
                 reviewer: str = "", role: str = "", now_iso: str = "") -> dict:
    if not isinstance(rating, int) or not 1 <= rating <= 5:
        raise FreelancerError("rating is 1–5")
    if recommend not in RECOMMEND:
        raise FreelancerError(f"recommend is one of {', '.join(RECOMMEND)}")
    patch: dict[str, Any] = {"status": "done", "rating": rating, "recommend": recommend,
                             "strengths": strengths.strip() or None,
                             "watch_outs": watch_outs.strip() or None,
                             "reviewer_name": reviewer.strip() or None}
    if role.strip():
        patch["role"] = role.strip()
    if now_iso:
        patch["completed_at"] = now_iso
    return patch


def hours_row(*, entity_id: str, project_id: str, kind: str, hours: float,
              source: str = "", period_start: str | None = None,
              period_end: str | None = None, source_ref: str = "", note: str = "") -> dict:
    if kind not in HOUR_KINDS:
        raise FreelancerError(f"kind is one of {', '.join(HOUR_KINDS)}")
    try:
        h = float(hours)
    except (TypeError, ValueError) as exc:
        raise FreelancerError("hours must be a number") from exc
    if h < 0:
        raise FreelancerError("hours cannot be negative")
    src = source or ("agreement" if kind == "agreed" else "manual")
    if src not in HOUR_SOURCES:
        raise FreelancerError(f"source is one of {', '.join(HOUR_SOURCES)}")
    if kind == "actual" and src == "agreement":
        raise FreelancerError("actual hours come from an invoice, a Gusto timesheet, or manual")
    return {"entity_id": entity_id, "project_id": project_id, "kind": kind, "hours": h,
            "source": src, "period_start": period_start or None, "period_end": period_end or None,
            "source_ref": source_ref.strip() or None, "note": note.strip() or None}


def find_person(client, name: str) -> dict | None:
    """An entity by exact name, else a unique case-insensitive substring."""
    wanted = (name or "").strip()
    if not wanted:
        return None
    rows = (client.table(Tables.ENTITIES)
            .select("id, name, is_external, kind, default_hourly_rate, payment_method")
            .execute().data or [])
    exact = [r for r in rows if (r.get("name") or "").lower() == wanted.lower()]
    if exact:
        return exact[0]
    near = [r for r in rows if wanted.lower() in (r.get("name") or "").lower()]
    return near[0] if len(near) == 1 else None


def hours_status(client, *, project_id: str | None = None, entity_id: str | None = None) -> list[dict]:
    """Per (person, project): agreed hours (newest), actual hours recorded,
    and — when none are recorded — an estimate from payables ÷ rate.
    `state` is `over`, `near` (≥80%), `ok`, `no agreement`, or `no actuals`."""
    q = client.table(Tables.CONTRACTOR_HOURS).select(
        "entity_id, project_id, kind, hours, source, created_at, period_end")
    if project_id:
        q = q.eq("project_id", project_id)
    if entity_id:
        q = q.eq("entity_id", entity_id)
    rows = q.execute().data or []
    pairs: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["entity_id"], r["project_id"])
        p = pairs.setdefault(key, {"entity_id": key[0], "project_id": key[1], "agreed": None,
                                   "agreed_at": "", "actual": 0.0, "actual_sources": set()})
        if r["kind"] == "agreed":
            if str(r.get("created_at") or "") >= p["agreed_at"]:
                p["agreed"], p["agreed_at"] = float(r["hours"]), str(r.get("created_at") or "")
        else:
            p["actual"] += float(r["hours"])
            p["actual_sources"].add(r.get("source"))
    if not pairs:
        return []
    ids = sorted({k[0] for k in pairs})
    people = {e["id"]: e for e in (client.table(Tables.ENTITIES)
                                   .select("id, name, default_hourly_rate, payment_method")
                                   .in_("id", ids).execute().data or [])}
    out = []
    for (eid, pid), p in sorted(pairs.items()):
        person = people.get(eid, {})
        actual, estimated = p["actual"], False
        if not p["actual_sources"]:
            rate = float(person.get("default_hourly_rate") or 0)
            if rate > 0:
                paid = (client.table(Tables.PAYABLES).select("amount").eq("entity_id", eid)
                        .eq("project_id", pid).execute().data or [])
                total = sum(float(x.get("amount") or 0) for x in paid)
                if total:
                    actual, estimated = round(total / rate, 1), True
        agreed = p["agreed"]
        if agreed is None:
            state = "no agreement"
        elif not p["actual_sources"] and not estimated:
            state = "no actuals"
        elif agreed and actual > agreed:
            state = "over"
        elif agreed and actual >= NEAR_SHARE * agreed:
            state = "near"
        else:
            state = "ok"
        out.append({"entity_id": eid, "name": person.get("name"), "project_id": pid,
                    "agreed": agreed, "actual": round(actual, 1), "estimated": estimated,
                    "share": round(actual / agreed, 2) if agreed else None, "state": state,
                    "paid_by": person.get("payment_method"),
                    "actuals_from": ("Gusto timesheets" if person.get("payment_method") == "gusto"
                                     else "invoices" if person.get("payment_method") else
                                     "unknown — set how they are paid in MC-2")})
    return out


def recommend(client, *, need: str = "", project_types: list[str] | None = None,
              limit: int = 8, today: date | None = None) -> list[dict]:
    """Freelancers ranked for a need, with the evidence: their reviews
    (average rating, how often recommended), the projects they worked on
    and those projects' types, and how recently. A `no` recommendation sinks
    a person to the bottom; it never hides them (it is evidence too)."""
    today = today or date.today()
    people = [e for e in (client.table(Tables.ENTITIES)
                          .select("id, name, kind, is_external, roles, notes, archived_at")
                          .eq("is_external", True).execute().data or [])
              if e.get("kind") in (None, "freelancer") and not e.get("archived_at")]
    if not people:
        return []
    ids = [p["id"] for p in people]
    reviews = (client.table(Tables.FREELANCER_REVIEWS)
               .select("entity_id, project_id, status, rating, recommend, role, strengths, watch_outs")
               .in_("entity_id", ids).execute().data or [])
    allocs = (client.table(Tables.SPRINT_ALLOCATIONS).select("entity_id, project_id, hours, week_start")
              .in_("entity_id", ids).execute().data or [])
    pids = sorted({a["project_id"] for a in allocs if a.get("project_id")})
    projects = {p["id"]: p for p in (client.table(Tables.PROJECTS)
                                     .select("id, code, full_job_name, project_types")
                                     .in_("id", pids).execute().data or [])} if pids else {}
    want_types = set(project_types or [])
    words = [w for w in (need or "").lower().split() if len(w) > 2]
    ranked = []
    for p in people:
        mine = [r for r in reviews if r["entity_id"] == p["id"] and r.get("status") == "done"]
        worked = sorted({a["project_id"] for a in allocs
                         if a["entity_id"] == p["id"] and float(a.get("hours") or 0) > 0})
        last = max((str(a.get("week_start") or "") for a in allocs if a["entity_id"] == p["id"]),
                   default="")
        types = sorted({t for pid in worked for t in (projects.get(pid, {}).get("project_types") or [])})
        text = " ".join([str(p.get("roles") or ""), str(p.get("notes") or ""),
                         *[f"{r.get('role') or ''} {r.get('strengths') or ''}" for r in mine]]).lower()
        avg = round(sum(r["rating"] for r in mine) / len(mine), 1) if mine else None
        noes = sum(1 for r in mine if r.get("recommend") == "no")
        yeses = sum(1 for r in mine if r.get("recommend") == "yes")
        score = (
            (avg or 3.0) * 2
            + yeses - 3 * noes
            + 2 * len(want_types & set(types))
            + sum(1 for w in words if w in text)
            + min(len(worked), 5) * 0.5
            + (1 if last and last >= date(today.year - 1, today.month, 1).isoformat() else 0)
        )
        if words and not any(w in text for w in words) and not (want_types & set(types)):
            score -= 3
        ranked.append({
            "entity_id": p["id"], "name": p.get("name"), "score": round(score, 1),
            "average_rating": avg, "reviews": len(mine), "recommended": yeses, "not_recommended": noes,
            "projects": [projects.get(x, {}).get("code") or x for x in worked][:8],
            "project_types": types, "last_worked": last or None,
            "watch_outs": [r["watch_outs"] for r in mine if r.get("watch_outs")][:3],
        })
    ranked.sort(key=lambda r: (-r["score"], r["name"] or ""))
    return ranked[:limit]


def pending_reviews(client) -> list[dict]:
    """Requested reviews, with the person and the project's owner."""
    rows = (client.table(Tables.FREELANCER_REVIEWS).select("id, entity_id, project_id")
            .eq("status", "requested").execute().data or [])
    if not rows:
        return []
    names = {e["id"]: e.get("name") for e in (client.table(Tables.ENTITIES).select("id, name")
             .in_("id", sorted({r["entity_id"] for r in rows})).execute().data or [])}
    projs = {p["id"]: p for p in (client.table(Tables.PROJECTS).select("id, code, account_manager")
             .in_("id", sorted({r["project_id"] for r in rows})).execute().data or [])}
    return [{"review_id": r["id"], "freelancer": names.get(r["entity_id"]),
             "project": projs.get(r["project_id"], {}).get("code"),
             "owner": projs.get(r["project_id"], {}).get("account_manager") or "No owner"}
            for r in rows]
