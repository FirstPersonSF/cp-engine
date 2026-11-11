"""Lessons — what we learn from projects (learning phase 1, mc-2 migs 215–217).

WHY THIS EXISTS. Drew, 2026-11-11: "I'd really like to continuously learn
from our projects." That means client lookouts (personality, patterns: the
Janet lookouts, for every client), lessons by kind of work, and small
operational lessons that are hard to keep in mind ("get travelling freelance
directors' expenses in before the final invoice"). Plan:
`docs/plans/2026-11-11-learning-from-projects.md`.

One MC-2 `lessons` row is one short lesson. The DATABASE enforces the rules
(scope requirements, list membership for types and moments, internal-only for
person/client lessons, the 12-month review date, a stale embedding cleared on
edit). This module checks the same rules first only to give a clear message
before a round trip; it never relaxes them.

Embeddings are injected (`embed=`), never constructed here from service
credentials, so the hosted server and the CLI can each pass their own. A
failed embed never loses the lesson: the row is written, the result says
`embedded: false`, and `embed_missing` catches it up.
"""

from __future__ import annotations

import os
import re as _re
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, timezone
from typing import Any

from cp_engine.mc2_db import Tables

#: Read shape. Never the embedding (1024 floats per row).
LESSON_COLUMNS = (
    "id, text, scope, company_id, stakeholder_name, stakeholder_item_id, "
    "project_types, moments, other_moment, source_project_id, source_kind, "
    "source_ref, evidence, status, owner_name, internal_only, created_at, "
    "updated_at, confirmed_at, review_due, retired_at, retired_reason, embedded_at"
)

SCOPES = ("person", "client", "type", "practice")
STATUSES = ("candidate", "confirmed", "retired")
SOURCE_KINDS = ("session", "meeting", "wrap_report", "dossier", "backfill", "manual")
EMBED_MODEL = "voyage-3-large"

Embedder = Callable[[str], Sequence[float]]


class LessonError(ValueError):
    """A lesson that the database would refuse, said plainly."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _a_year_on(d: date) -> date:
    """The same day next year (28 Feb for 29 Feb), matching Postgres' `+ 12 months`."""
    try:
        return d.replace(year=d.year + 1)
    except ValueError:
        return d.replace(year=d.year + 1, day=28)


def _clean_list(values: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for v in values or ():
        s = str(v).strip()
        if s and s not in out:
            out.append(s)
    return out


def list_project_types(client, *, include_inactive: bool = False) -> list[dict]:
    q = client.table(Tables.PROJECT_TYPES).select("slug, name, sort, active")
    if not include_inactive:
        q = q.eq("active", True)
    return q.order("sort").execute().data or []


def list_moments(client, *, include_inactive: bool = False) -> list[dict]:
    q = client.table(Tables.LESSON_MOMENTS).select("slug, name, description, sort, active")
    if not include_inactive:
        q = q.eq("active", True)
    return q.order("sort").execute().data or []


def check_draft(
    *,
    text: str,
    scope: str,
    company_id: str | None = None,
    stakeholder_name: str | None = None,
    project_types: Sequence[str] = (),
    moments: Sequence[str] = (),
    known_types: Iterable[str] | None = None,
    known_moments: Iterable[str] | None = None,
) -> None:
    """Raise LessonError for what the database would refuse."""
    body = (text or "").strip()
    if not 10 <= len(body) <= 1200:
        raise LessonError("a lesson is 10–1,200 characters")
    if scope not in SCOPES:
        raise LessonError(f"scope must be one of {', '.join(SCOPES)}")
    if scope == "client" and not company_id:
        raise LessonError("a client lesson needs the client (company)")
    if scope == "person" and not (stakeholder_name or "").strip():
        raise LessonError("a person lesson needs the person's name")
    if scope == "type" and not project_types:
        raise LessonError("a type lesson needs at least one project type")
    if known_types is not None:
        unknown = [t for t in project_types if t not in set(known_types)]
        if unknown:
            raise LessonError(f"unknown project type(s): {', '.join(unknown)}")
    if known_moments is not None:
        unknown = [m for m in moments if m not in set(known_moments)]
        if unknown:
            raise LessonError(
                f"unknown moment(s): {', '.join(unknown)} — put a moment that is "
                "not on the list in other_moment"
            )


def _embed_row(client, lesson_id: str, text: str, embed: Embedder | None) -> dict:
    """Best-effort: write the embedding for one lesson. Never raises."""
    if embed is None:
        return {"embedded": False, "embed_reason": "no embedder"}
    try:
        vector = list(embed(text))
        (
            client.table(Tables.LESSONS)
            .update({"embedding": vector, "embedded_at": _now()})
            .eq("id", lesson_id)
            .execute()
        )
        return {"embedded": True}
    except Exception as exc:  # noqa: BLE001 — the lesson is already saved
        return {"embedded": False, "embed_reason": str(exc)[:200]}


def lesson_row(
    *,
    text: str,
    scope: str,
    company_id: str | None = None,
    stakeholder_name: str | None = None,
    stakeholder_item_id: str | None = None,
    project_types: Sequence[str] = (),
    moments: Sequence[str] = (),
    other_moment: str | None = None,
    source_project_id: str | None = None,
    source_kind: str = "session",
    source_ref: str | None = None,
    evidence: str | None = None,
    status: str = "candidate",
    owner_name: str | None = None,
    known_types: Iterable[str] | None = None,
    known_moments: Iterable[str] | None = None,
) -> dict:
    """The row to INSERT, checked. Pure: the caller does the write, so the
    hosted server's writes stay visible in its own code (its read/write
    classification is derived from the code)."""
    types = _clean_list(project_types)
    moms = _clean_list(moments)
    if status not in STATUSES:
        raise LessonError(f"status must be one of {', '.join(STATUSES)}")
    if source_kind not in SOURCE_KINDS:
        raise LessonError(f"source_kind must be one of {', '.join(SOURCE_KINDS)}")
    check_draft(text=text, scope=scope, company_id=company_id,
                stakeholder_name=stakeholder_name, project_types=types, moments=moms,
                known_types=known_types, known_moments=known_moments)
    row = {
        "text": text.strip(),
        "scope": scope,
        "company_id": company_id,
        "stakeholder_name": (stakeholder_name or "").strip() or None,
        "stakeholder_item_id": stakeholder_item_id,
        "project_types": types,
        "moments": moms,
        "other_moment": (other_moment or "").strip() or None,
        "source_project_id": source_project_id,
        "source_kind": source_kind,
        "source_ref": source_ref,
        "evidence": (evidence or "").strip() or None,
        "status": status,
        "owner_name": owner_name,
    }
    if status == "confirmed":
        row["confirmed_at"] = _now()  # the trigger also sets it; the check needs it on insert
    return row


def known_lists(client) -> tuple[list[str], list[str]]:
    """Every type and moment slug, retired ones included (they stay valid)."""
    return ([t["slug"] for t in list_project_types(client, include_inactive=True)],
            [m["slug"] for m in list_moments(client, include_inactive=True)])


def read_shape(row: dict) -> dict:
    """A written row in the read shape (never the embedding)."""
    return {k: row.get(k) for k in LESSON_COLUMNS.split(", ") if k in row}


def add_lesson(client, *, embed: Embedder | None = None, validate_lists: bool = True,
               **fields) -> dict:
    """Write one lesson (fields as `lesson_row`); embed it if an embedder is
    given. Returns the read shape plus `embedded` / `embed_reason`."""
    known_types = known_moments = None
    if validate_lists and (fields.get("project_types") or fields.get("moments")):
        known_types, known_moments = known_lists(client)
    row = lesson_row(known_types=known_types, known_moments=known_moments, **fields)
    inserted = client.table(Tables.LESSONS).insert(row).execute().data or []
    if not inserted:
        raise LessonError("the lesson was not saved (no row returned)")
    saved = read_shape(inserted[0])
    saved.update(_embed_row(client, saved["id"], saved.get("text") or row["text"], embed))
    return saved


def list_lessons(
    client,
    *,
    status: str | None = "confirmed",
    scope: str | None = None,
    company_id: str | None = None,
    project_types: Sequence[str] | None = None,
    moments: Sequence[str] | None = None,
    owner_name: str | None = None,
    source_project_id: str | None = None,
    review_due_by: date | None = None,
    limit: int = 50,
) -> list[dict]:
    """Filtered read, newest first. `status=None` reads every status."""
    q = client.table(Tables.LESSONS).select(LESSON_COLUMNS)
    if status:
        q = q.eq("status", status)
    if scope:
        q = q.eq("scope", scope)
    if company_id:
        q = q.eq("company_id", company_id)
    if project_types:
        q = q.overlaps("project_types", list(project_types))
    if moments:
        q = q.overlaps("moments", list(moments))
    if owner_name:
        q = q.eq("owner_name", owner_name)
    if source_project_id:
        q = q.eq("source_project_id", source_project_id)
    if review_due_by:
        q = q.lte("review_due", review_due_by.isoformat())
    return q.order("created_at", desc=True).limit(max(1, min(limit, 200))).execute().data or []


def due_for_review(client, *, today: date | None = None, owner_name: str | None = None) -> list[dict]:
    """Confirmed lessons whose 12-month review has come round."""
    return list_lessons(client, status="confirmed", owner_name=owner_name,
                        review_due_by=today or date.today(), limit=200)


def status_patch(status: str, *, reason: str | None = None,
                 keep_review: bool = False, today: date | None = None) -> dict:
    """The UPDATE for confirm / retire / back to candidate. Confirming
    restarts the 12-month review unless `keep_review`."""
    if status not in STATUSES:
        raise LessonError(f"status must be one of {', '.join(STATUSES)}")
    patch: dict[str, Any] = {"status": status}
    if status == "confirmed":
        patch["confirmed_at"] = _now()
        if not keep_review:
            # Set here, not left to the trigger: it only dates a lesson on its
            # FIRST confirmation, and a re-confirm after review must restart
            # the clock too.
            patch["review_due"] = _a_year_on(today or date.today()).isoformat()
    if status == "retired":
        patch["retired_reason"] = (reason or "").strip() or None
    return patch


EDITABLE = frozenset({"text", "scope", "company_id", "stakeholder_name", "stakeholder_item_id",
                      "project_types", "moments", "other_moment", "evidence", "owner_name"})


def edit_patch(known_types: Iterable[str] | None = None,
               known_moments: Iterable[str] | None = None, **fields) -> dict:
    """The UPDATE for an edit, checked. Empty fields are left out."""
    bad = set(fields) - EDITABLE
    if bad:
        raise LessonError(f"cannot edit {', '.join(sorted(bad))}")
    patch = dict(fields)
    for key in ("project_types", "moments"):
        if key in patch:
            patch[key] = _clean_list(patch[key])
    if "text" in patch:
        patch["text"] = (patch["text"] or "").strip()
        if not 10 <= len(patch["text"]) <= 1200:
            raise LessonError("a lesson is 10–1,200 characters")
    if "scope" in patch and patch["scope"] not in SCOPES:
        raise LessonError(f"scope must be one of {', '.join(SCOPES)}")
    if known_types is not None and patch.get("project_types"):
        unknown = [t for t in patch["project_types"] if t not in set(known_types)]
        if unknown:
            raise LessonError(f"unknown project type(s): {', '.join(unknown)}")
    if known_moments is not None and patch.get("moments"):
        unknown = [m for m in patch["moments"] if m not in set(known_moments)]
        if unknown:
            raise LessonError(f"unknown moment(s): {', '.join(unknown)} — put a moment "
                              "that is not on the list in other_moment")
    return patch


def set_status(client, lesson_id: str, status: str, *, reason: str | None = None,
               keep_review: bool = False) -> dict:
    patch = status_patch(status, reason=reason, keep_review=keep_review)
    rows = client.table(Tables.LESSONS).update(patch).eq("id", lesson_id).execute().data or []
    if not rows:
        raise LessonError(f"no lesson {lesson_id}")
    return read_shape(rows[0])


def edit_lesson(client, lesson_id: str, *, embed: Embedder | None = None, **fields) -> dict:
    """Change a lesson's text or tags. A text change re-embeds (the trigger
    has already cleared the stale vector)."""
    patch = edit_patch(**fields)
    rows = client.table(Tables.LESSONS).update(patch).eq("id", lesson_id).execute().data or []
    if not rows:
        raise LessonError(f"no lesson {lesson_id}")
    out = read_shape(rows[0])
    if "text" in patch:
        out.update(_embed_row(client, lesson_id, patch["text"], embed))
    return out


def search_lessons(
    client,
    query_vector: Sequence[float],
    *,
    limit: int = 10,
    company_id: str | None = None,
    project_types: Sequence[str] | None = None,
    moments: Sequence[str] | None = None,
    include_candidates: bool = False,
) -> list[dict]:
    """By meaning, via `match_lessons`. With `company_id`, another client's
    person/client lessons never come back (enforced in the RPC)."""
    params = {
        "query_embedding": list(query_vector),
        "match_count": max(1, min(limit, 50)),
        "p_company_id": company_id,
        "p_project_types": list(project_types) if project_types else None,
        "p_moments": list(moments) if moments else None,
        "p_include_candidates": include_candidates,
    }
    return client.rpc("match_lessons", params).execute().data or []


def embed_missing(client, embed: Embedder, *, limit: int = 100) -> dict:
    """Catch up lessons saved without an embedding (a failed or skipped embed)."""
    rows = (
        client.table(Tables.LESSONS).select("id, text")
        .is_("embedding", "null").neq("status", "retired")
        .limit(max(1, min(limit, 500))).execute().data or []
    )
    done = failed = 0
    for r in rows:
        if _embed_row(client, r["id"], r.get("text") or "", embed).get("embedded"):
            done += 1
        else:
            failed += 1
    return {"checked": len(rows), "embedded": done, "failed": failed}


def voyage_embedder(input_type: str = "document") -> Embedder:
    """An embedder on the corpus model. `document` to store, `query` to search."""
    import voyageai

    vc = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])

    def _embed(text: str) -> list[float]:
        return vc.embed([text], model=EMBED_MODEL, input_type=input_type).embeddings[0]

    return _embed


def record_meeting_candidates(client, items, *, project_code, meeting_id, embed=None) -> dict:
    """Meeting ingest's lesson queue (phase 2) — `record_candidates` keyed
    on the meeting."""
    return record_candidates(client, items, project_code=project_code,
                             source_kind="meeting", source_ref=meeting_id, embed=embed)


def record_candidates(
    client,
    items: Sequence[Any],
    *,
    project_code: str | None,
    source_kind: str,
    source_ref: str | None,
    embed: Embedder | None = None,
    project_id: str | None = None,
) -> dict:
    """Queue lessons a meeting ingest proposed, as `candidate`s for the
    project owner to confirm (Drew, 2026-11-11: the owner reviews).

    Never raises and never fails the caller: a bad item is skipped with a
    reason. Idempotent per source (`source_ref`: a meeting, a close-out
    report): an item whose text that source already queued is skipped, so a
    re-run adds nothing. `project_id` wins over resolving `project_code`.
    """
    out: dict[str, Any] = {"queued": [], "skipped": []}
    if not items:
        return out
    pid = cid = None
    ptypes: list[str] = []
    try:
        from cp_engine.clickup_routing import engagement_number

        number = engagement_number(project_code or "") if project_code else None
        rows = []
        if project_id:
            rows = (client.table(Tables.PROJECTS)
                    .select("id, company_id, project_types").eq("id", project_id)
                    .limit(1).execute().data or [])
        elif number is not None:
            rows = (client.table(Tables.PROJECTS)
                    .select("id, company_id, project_types").eq("number", number)
                    .limit(1).execute().data or [])
        if rows:
            pid, cid = rows[0]["id"], rows[0].get("company_id")
            ptypes = list(rows[0].get("project_types") or [])
        known_types, known_moments = known_lists(client)
        already = set()
        if source_ref:
            already = {
                (r.get("text") or "").strip().lower()
                for r in (client.table(Tables.LESSONS).select("text")
                          .eq("source_ref", str(source_ref)).execute().data or [])
            }
    except Exception as exc:  # noqa: BLE001 — a lesson queue never fails an ingest
        out["skipped"].append(f"lookup failed: {str(exc)[:200]}")
        return out

    for raw in items:
        item = {"text": raw} if isinstance(raw, str) else raw
        if not isinstance(item, dict):
            out["skipped"].append("not a mapping")
            continue
        text = str(item.get("text") or "").strip()
        if text.lower() in already:
            out["skipped"].append(f"already queued: {text[:60]}")
            continue
        scope = str(item.get("scope") or "practice").strip().lower()
        moments = [m for m in _clean_list(item.get("moments")) if m in known_moments]
        unlisted = [m for m in _clean_list(item.get("moments")) if m not in known_moments]
        try:
            row = lesson_row(
                text=text, scope=scope,
                company_id=cid if scope in ("client", "person") else None,
                stakeholder_name=item.get("person"),
                project_types=ptypes if scope == "type" else [],
                moments=moments, other_moment=", ".join(unlisted) or None,
                source_project_id=pid, source_kind=source_kind,
                source_ref=str(source_ref) if source_ref else None,
                evidence=item.get("evidence"), status="candidate",
                known_types=known_types, known_moments=known_moments,
            )
            saved = client.table(Tables.LESSONS).insert(row).execute().data or []
        except Exception as exc:  # noqa: BLE001
            out["skipped"].append(f"{text[:60]!r}: {str(exc)[:160]}")
            continue
        if saved:
            already.add(text.lower())
            out["queued"].append(saved[0].get("id"))
            if embed is not None:
                _embed_row(client, saved[0]["id"], row["text"], embed)
    return out


# ── Phase 3: surfacing ────────────────────────────────────────────────

#: Lookout groups, in the order a reader wants them.
LOOKOUT_GROUPS = (("people", "People"), ("client", "Client"),
                  ("type", "This kind of work"), ("practice", "How we work"))


def _project_facts(client, project_id: str) -> dict:
    rows = (client.table(Tables.PROJECTS).select("id, company_id, project_types")
            .eq("id", project_id).limit(1).execute().data or [])
    return rows[0] if rows else {}


def lookouts(client, project_id: str | None, *, per_group: int = 6) -> dict:
    """What a project should watch for: its client's people and client
    lessons, its project types' lessons, and practice lessons — confirmed
    only. Never another client's person/client lessons. Each group is
    `{count, items}`; a failed read is `{error}`, never shown as nothing.
    Also counts what awaits the owner (candidates for this project's client
    or this project, and confirmed lessons due for their 12-month review)."""
    out: dict[str, Any] = {"internal": True}
    if client is None or not project_id:
        return out
    try:
        facts = _project_facts(client, project_id)
    except Exception as exc:  # noqa: BLE001
        return {**out, "error": str(exc)[:200]}
    cid = facts.get("company_id")
    ptypes = list(facts.get("project_types") or [])
    out["project_types"] = ptypes

    def _group(**kw) -> dict:
        try:
            rows = list_lessons(client, status="confirmed", limit=per_group * 3, **kw)
            return rows
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:200]}

    raw = {
        "people": _group(company_id=cid, scope="person") if cid else [],
        "client": _group(company_id=cid, scope="client") if cid else [],
        "type": _group(scope="type", project_types=ptypes) if ptypes else [],
        "practice": _group(scope="practice"),
    }
    total = 0
    for key, rows in raw.items():
        if isinstance(rows, dict):
            out[key] = rows
            continue
        out[key] = {"count": len(rows), "items": [
            {"id": r.get("id"), "text": r.get("text"), "person": r.get("stakeholder_name"),
             "moments": r.get("moments") or [], "types": r.get("project_types") or []}
            for r in rows[:per_group]]}
        total += len(rows)
    out["total"] = total
    try:
        waiting = list_lessons(client, status="candidate", source_project_id=project_id, limit=200)
        if cid:
            seen = {r["id"] for r in waiting}
            waiting += [r for r in list_lessons(client, status="candidate", company_id=cid, limit=200)
                        if r["id"] not in seen]
        out["candidates"] = len(waiting)
    except Exception:  # noqa: BLE001 — a count never fails the block
        pass
    return out


def render_lookouts(block: dict) -> str:
    """Markdown for `cxp brief`: grouped, people named, internal said once."""
    if block.get("error"):
        return f"_Could not read lookouts: {block['error']}_"
    lines = ["_Internal: never in client-facing work._"]
    for key, label in LOOKOUT_GROUPS:
        part = block.get(key)
        if not part:
            continue
        if "error" in part:
            lines.append(f"- **{label}:** could not read ({part['error']})")
            continue
        if not part.get("count"):
            continue
        lines.append(f"- **{label}:**")
        for item in part["items"]:
            who = f"{item['person']}: " if item.get("person") else ""
            when = f" _({', '.join(item['moments'])})_" if item.get("moments") else ""
            lines.append(f"  - {who}{item['text']}{when}")
        if part["count"] > len(part["items"]):
            lines.append(f"  - … {part['count'] - len(part['items'])} more (`list_lessons`)")
    if block.get("candidates"):
        lines.append(f"- {block['candidates']} candidate(s) awaiting the owner's review.")
    return "\n".join(lines)


#: Words in what is coming up that mean a moment is near. Moments not listed
#: here (the list grows) match on their own name, so a new moment works on
#: day one and gets better words later.
MOMENT_SIGNALS: dict[str, tuple[str, ...]] = {
    "kickoff": ("kickoff", "kick-off", "kick off"),
    "estimating": ("estimate", "quote", "sow", "pricing", "budget"),
    "process-order": ("sign-off", "sign off", "approval", "skip"),
    "booking-resources": ("book ", "booking", "availability", "hold dates", "crew"),
    "hiring-freelancers": ("freelance", "contractor", "hire ", "director"),
    "contractor-hours": ("hours", "timesheet", "overage"),
    "scope-change": ("scope", "change order", "out of scope", "additional round"),
    "travel": ("travel", "flight", "hotel", "on-site", "onsite", "on site"),
    "shoot": ("shoot", "filming", "production day", "on set"),
    "client-review-round": ("review", "feedback", "round", "approval"),
    "legal-review": ("legal", "clearance", "compliance", "mlr"),
    "final-delivery": ("final delivery", "deliver final", "handoff", "hand-off", "launch", "final files"),
    "invoicing": ("invoice", "invoicing", "billing", "purchase order", " po "),
}


def moments_in(text: str, known_moments: Iterable[str] | None = None) -> list[str]:
    """Moments whose signal words appear in `text` (what is coming up)."""
    hay = f" {(text or '').lower()} "
    slugs = list(known_moments) if known_moments is not None else list(MOMENT_SIGNALS)
    found = []
    for slug in slugs:
        words = MOMENT_SIGNALS.get(slug) or (slug.replace("-", " "),)
        if any(w in hay for w in words):
            found.append(slug)
    return found


def watch_for(client, project_id: str | None, upcoming_text: str, *, limit: int = 5) -> list[dict]:
    """Confirmed lessons for the moments near on this project: its client's,
    its types' and practice ones — never another client's. Each item names
    the moment that brought it up. Empty on any failure (advisory)."""
    if client is None or not project_id or not (upcoming_text or "").strip():
        return []
    try:
        _types, known_moments = known_lists(client)
        near = moments_in(upcoming_text, known_moments)
        if not near:
            return []
        facts = _project_facts(client, project_id)
        cid, ptypes = facts.get("company_id"), set(facts.get("project_types") or [])
        rows = list_lessons(client, status="confirmed", moments=near, limit=100)
    except Exception:  # noqa: BLE001 — a prep hint never fails the bundle
        return []
    out = []
    for r in rows:
        if r.get("scope") in ("person", "client") and r.get("company_id") != cid:
            continue
        if r.get("scope") == "type" and not (set(r.get("project_types") or []) & ptypes):
            continue
        hit = [m for m in (r.get("moments") or []) if m in near]
        out.append({"id": r.get("id"), "text": r.get("text"), "moment": hit[0] if hit else None,
                    "scope": r.get("scope"), "person": r.get("stakeholder_name")})
        if len(out) >= limit:
            break
    return out


#: Moments that bear on scoping and pricing a piece of work.
SCOPING_MOMENTS = ("kickoff", "estimating", "process-order", "booking-resources",
                   "hiring-freelancers", "contractor-hours", "scope-change")


def scoping_lessons(client, project_id: str | None, *, limit: int = 8) -> dict:
    """For proposals, RFPs and estimates: confirmed lessons about scoping
    this kind of work (its types' and practice lessons on the scoping
    moments), plus the client's lookouts SEPARATELY, marked inform-only.

    The split is the guardrail's first half: `scoping` may shape what the
    document commits to; `client_lookouts` must never appear in it."""
    out: dict[str, Any] = {"scoping": [], "client_lookouts": [],
                           "rule": "client_lookouts inform you; never write them into the document"}
    if client is None or not project_id:
        return out
    try:
        facts = _project_facts(client, project_id)
        cid, ptypes = facts.get("company_id"), list(facts.get("project_types") or [])
        rows = list_lessons(client, status="confirmed", moments=list(SCOPING_MOMENTS), limit=100)
        scoping = [r for r in rows if r.get("scope") == "practice"
                   or (r.get("scope") == "type" and set(r.get("project_types") or []) & set(ptypes))]
        out["scoping"] = [{"text": r["text"], "moments": r.get("moments") or [],
                           "scope": r.get("scope")} for r in scoping[:limit]]
        if cid:
            people = list_lessons(client, status="confirmed", company_id=cid, limit=limit)
            out["client_lookouts"] = [{"text": r["text"], "person": r.get("stakeholder_name"),
                                       "scope": r.get("scope")} for r in people
                                      if r.get("scope") in ("person", "client")]
    except Exception as exc:  # noqa: BLE001 — advisory
        out["error"] = str(exc)[:200]
    return out


# ── Guardrail: internal lessons never reach client-facing work ─────────


#: Framings of documents a client or partner reads. Proposals and RFPs are
#: saved as spine elements with these framings (the proposal-architect and
#: rfp-authoring skills).
_CLIENT_FACING = _re.compile(
    r"engagement summary|proposal|\brfp\b|statement of work|\bsow\b|scope of work"
    r"|client (deck|email|brief|update)|partner brief", _re.I)

#: Consecutive words a document must share with a lesson to count as quoting it.
LEAK_SHINGLE = 6

_WORD = _re.compile(r"[a-z0-9']+")


def client_facing(framing: str | None) -> bool:
    return bool(_CLIENT_FACING.search(framing or ""))


def _shingles(text: str, n: int) -> set[tuple[str, ...]]:
    words = _WORD.findall((text or "").lower())
    return {tuple(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def leaks(text: str, lessons: Iterable[dict], *, n: int = LEAK_SHINGLE) -> list[dict]:
    """Lessons whose wording appears in `text` (n consecutive words). A name
    alone is not a leak — Janet belongs in a proposal; "Janet ends rounds only
    when legal has seen it" does not."""
    doc = _shingles(text, n)
    if not doc:
        return []
    return [les for les in lessons if _shingles(les.get("text") or "", n) & doc]


def internal_leaks(client, project_id: str | None, text: str) -> list[dict]:
    """The project's client's person/client lessons (any status but retired)
    quoted in `text`. Empty when there is nothing to check against."""
    if client is None or not project_id or not (text or "").strip():
        return []
    cid = _project_facts(client, project_id).get("company_id")
    if not cid:
        return []
    rows = [r for s in ("candidate", "confirmed")
            for r in list_lessons(client, status=s, company_id=cid, limit=200)
            if r.get("scope") in ("person", "client")]
    return [{"id": r.get("id"), "text": r.get("text"), "person": r.get("stakeholder_name")}
            for r in leaks(text, rows)]


def review_queue_by_owner(client, *, today: date | None = None) -> list[dict]:
    """Per reviewer: candidates to confirm and confirmed lessons due for
    their 12-month review. `[{owner, candidates, due}]`, owners sorted;
    empty on any failure (the master-cp agenda never fails a sync)."""
    if client is None:
        return []
    try:
        cands = list_lessons(client, status="candidate", limit=200)
        due = due_for_review(client, today=today)
    except Exception:  # noqa: BLE001
        return []
    by: dict[str, dict] = {}
    for rows, key in ((cands, "candidates"), (due, "due")):
        for r in rows:
            o = r.get("owner_name") or "No owner"
            by.setdefault(o, {"owner": o, "candidates": 0, "due": 0})[key] += 1
    return [by[o] for o in sorted(by)]
