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


def add_lesson(
    client,
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
    embed: Embedder | None = None,
    validate_lists: bool = True,
) -> dict:
    """Write one lesson; embed it if an embedder is given. Returns the row
    (read shape) plus `embedded` / `embed_reason`."""
    types = _clean_list(project_types)
    moms = _clean_list(moments)
    if status not in STATUSES:
        raise LessonError(f"status must be one of {', '.join(STATUSES)}")
    if source_kind not in SOURCE_KINDS:
        raise LessonError(f"source_kind must be one of {', '.join(SOURCE_KINDS)}")
    known_types = known_moments = None
    if validate_lists and (types or moms):
        known_types = [t["slug"] for t in list_project_types(client, include_inactive=True)]
        known_moments = [m["slug"] for m in list_moments(client, include_inactive=True)]
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
    inserted = (
        client.table(Tables.LESSONS).insert(row).execute().data or []
    )
    if not inserted:
        raise LessonError("the lesson was not saved (no row returned)")
    saved = {k: inserted[0].get(k) for k in LESSON_COLUMNS.split(", ") if k in inserted[0]}
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


def set_status(client, lesson_id: str, status: str, *, reason: str | None = None,
               keep_review: bool = False) -> dict:
    """confirm / retire / back to candidate. Confirming a lesson that came up
    for review restarts its 12 months unless `keep_review`."""
    if status not in STATUSES:
        raise LessonError(f"status must be one of {', '.join(STATUSES)}")
    patch: dict[str, Any] = {"status": status}
    if status == "confirmed":
        patch["confirmed_at"] = _now()
        if not keep_review:
            # Set here, not left to the trigger: it only dates a lesson on its
            # FIRST confirmation, and a re-confirm after review must restart
            # the clock too.
            patch["review_due"] = _a_year_on(date.today()).isoformat()
    if status == "retired":
        patch["retired_reason"] = (reason or "").strip() or None
    rows = (
        client.table(Tables.LESSONS).update(patch).eq("id", lesson_id).execute().data or []
    )
    if not rows:
        raise LessonError(f"no lesson {lesson_id}")
    return {k: rows[0].get(k) for k in LESSON_COLUMNS.split(", ") if k in rows[0]}


def edit_lesson(client, lesson_id: str, *, embed: Embedder | None = None, **fields) -> dict:
    """Change a lesson's text or tags. A text change re-embeds (the trigger
    has already cleared the stale vector)."""
    allowed = {"text", "scope", "company_id", "stakeholder_name", "stakeholder_item_id",
               "project_types", "moments", "other_moment", "evidence", "owner_name"}
    bad = set(fields) - allowed
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
    rows = client.table(Tables.LESSONS).update(patch).eq("id", lesson_id).execute().data or []
    if not rows:
        raise LessonError(f"no lesson {lesson_id}")
    out = {k: rows[0].get(k) for k in LESSON_COLUMNS.split(", ") if k in rows[0]}
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
