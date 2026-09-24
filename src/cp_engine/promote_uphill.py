"""Promote a decision or commitment ONE level up the workstream tree (#304).

Plan §3.6 ("Every capture names its level"): a capture lands on the
workstream the caller NAMED. Nothing in the engine reads a decision's text
and decides it "sounds like" an account-level fact — that is the inference
Tony ruled out (design doc, 14:57). When something recorded on a job turns
out to belong to its account or program, promoting it is an explicit
action, and the action leaves a trail:

  * a **commitment** is COPIED to the parent (`project_id` = parent,
    `source_kind='promoted'`, `cp_hash` derived from the original's hash +
    the parent code); the original stays where it was;
  * a **decision** bullet is COPIED into the parent's current sprint file
    (`## Meeting notes & decisions / ### Decisions`) through the same writer
    the auto-ingest webhook uses (`ingest._write_decision`), so the copy
    carries the standard `<!-- cp:hash=… -->` trailer and re-running is a
    no-op;
  * every promotion appends a step to the parent's `Promoted uphill` spine
    element ("Promoted from <code>: <first 80 chars>", provenance in the
    note) so the parent's trail says where the item came from.

The parent is read from `.cp-engine/paths.json` (#302) — the one committed
statement of the tree — never guessed from a code prefix. A top-level
workstream has no parent and promotion refuses with "no parent".

Idempotent on the same item: the derived `cp_hash` (commitment) or the
content hash the writer already computes (decision) makes a second call
report `already: true` and write nothing, including no second step.

The hosted server (`prototypes/hosted-mcp/server.py`) carries its own copy
of the commitment path — it does not import cp_engine — and hands the
decision path back to `cxp promote-uphill`, because it has no file write
and the mc-2 → webhook route it delegates through carries session and Exec
Summary captures only, not sprint-file bullets.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from cp_engine.authored_element import (
    authored_est_item_id,
    build_create_rows,
    slugify,
)
from cp_engine.mc2_db import Tables, _resolve_project_id, canonical_spine_code
from cp_engine.state import PATHS_INDEX_REL, load_paths_index

ITEM_KINDS = ("decision", "commitment")
SOURCE_KIND_PROMOTED = "promoted"

# The parent-side element every promotion steps on. One per workstream,
# created on first use; its trail IS the promotion log.
PROMOTIONS_LABEL = "Promoted uphill"
PROMOTIONS_EST_ITEM_ID = authored_est_item_id(slugify(PROMOTIONS_LABEL))
PROMOTIONS_BODY = (
    "Items promoted from child workstreams by `promote_uphill` / "
    "`cxp promote-uphill`. Each promotion is one step on this element's "
    "trail, naming the child it came from. The level of a capture is never "
    "inferred from its content — a promotion is always someone's explicit "
    "call, and this is where those calls are recorded."
)

TITLE_EXCERPT_CHARS = 80

# The rule every capture verb states in its description (hosted server,
# stdio server, CLI). One spelling, so the three surfaces cannot drift.
LEVEL_RULE = (
    "LEVEL: writes land on the named workstream (`project_code`), and the "
    "response echoes `level: {code, label, parent}` so you can see where it "
    "landed. Default to the deepest workstream in focus (mode 2's loaded "
    "project). To record something at the account or program level, name "
    "THAT code — or call `promote_uphill` afterwards, which copies the item "
    "to the parent and leaves a step. The level is never inferred from "
    "content."
)

# `- [decision · 2026-09-24][cross-cutting] text <!-- cp:hash=abcd1234 -->`,
# with or without the code-span backticks hand-written bullets carry (#272),
# with or without the trailer (a hand-written bullet has none).
_DECISION_RE = re.compile(
    r"^\s*-\s+`?\[decision\s*·\s*(?P<date>[^\]]+)\]`?"
    r"(?P<cross>\s*\[cross-cutting\])?\s*"
    r"(?P<text>.*?)"
    r"(?:\s*<!--\s*cp:hash=(?P<hash>[0-9a-f]{8})\s*-->)?\s*$"
)
_HASH_TRAILER_RE = re.compile(r"\s*<!--\s*cp:hash=[0-9a-f]+\s*-->\s*$")

# What the copy carries over. `id`, `project_id`, `status`, `cp_hash` are
# read for the checks; everything else is copied verbatim.
COMMITMENT_COPY_COLUMNS = (
    "id, project_id, status, cp_hash, description, owner_email, owner_name, "
    "direction, due_date, work_item_id, work_item_kind, spine_element_id, "
    "source_meeting_id"
)


# ──────────────────────────────────────────────────────────────────────
#  Level — where a code sits in the tree
# ──────────────────────────────────────────────────────────────────────


def level_for(tenant_root: Path, code: str) -> dict[str, Any]:
    """`{code, label, parent, indexed}` for `code` from `.cp-engine/paths.json`.

    Exact key first; then the `<code>-` prefix form (`ibx-5153` for
    `ibx-5153-ai-campaign`) when it is unique. An unindexed code comes back
    with `indexed: False` and no parent — the caller decides whether that
    is fatal (promotion: yes; an echo on a capture: no).
    """
    index = load_paths_index(tenant_root)
    wanted = (code or "").strip()
    entry = index.get(wanted)
    if entry is None and wanted:
        lowered = wanted.lower()
        hits = [e for k, e in index.items() if k.lower().startswith(lowered + "-")]
        if len(hits) == 1:
            entry = hits[0]
    if entry is None:
        return {"code": wanted, "label": None, "parent": None, "indexed": False}
    return {
        "code": entry.code,
        "label": entry.label,
        "parent": entry.parent,
        "indexed": True,
    }


def _parent_or_error(
    tenant_root: Path, code: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(child level, parent level) or (None, error)."""
    child = level_for(tenant_root, code)
    if not child["indexed"]:
        return None, {
            "error": (
                f"{code!r} is not in {PATHS_INDEX_REL} — run `cxp sync` (the "
                "index is written by sync) or check the code"
            )
        }
    parent_code = child.get("parent")
    if not parent_code:
        return None, {
            "error": (
                f"no parent: {child['code']} is a top-level workstream "
                f"({child.get('label') or 'unlabelled'}); there is nowhere "
                "uphill to promote to"
            ),
            "level": child,
        }
    parent = level_for(tenant_root, parent_code)
    return child, parent


def promoted_hash(original: str, parent_code: str) -> str:
    """The copy's `cp_hash`: derived from the ORIGINAL's identity plus the
    parent code, so the same item promoted twice collides (idempotent) and
    the same text on two different children does not."""
    raw = f"promote-uphill|{original}|{parent_code}".encode()
    return hashlib.sha256(raw).hexdigest()[:8]


def promotion_step_title(child_code: str, text: str) -> str:
    excerpt = " ".join((text or "").split())
    if len(excerpt) > TITLE_EXCERPT_CHARS:
        excerpt = excerpt[:TITLE_EXCERPT_CHARS].rstrip() + "…"
    return f"Promoted from {child_code}: {excerpt}"


# ──────────────────────────────────────────────────────────────────────
#  The parent-side trail
# ──────────────────────────────────────────────────────────────────────


def ensure_promotions_element(
    client: Any, parent_id: str, parent_code: str, *, now: datetime | None = None
) -> str:
    """The parent's `Promoted uphill` element's est_item_id, creating v1 if
    the parent has none yet (the engine's own authored-element row shape)."""
    existing = (
        client.table(Tables.SPINE_SUBSTANCE)
        .select("est_item_id")
        .eq("project_id", parent_id)
        .eq("est_item_id", PROMOTIONS_EST_ITEM_ID)
        .limit(1)
        .execute()
        .data
        or []
    )
    if existing:
        return PROMOTIONS_EST_ITEM_ID
    stamp = (now or datetime.now(UTC)).isoformat()
    rows = build_create_rows(
        project_id=parent_id,
        project_code=canonical_spine_code(client, parent_id, parent_code),
        label=PROMOTIONS_LABEL,
        type_="note",
        body=PROMOTIONS_BODY,
        serves=[],
        now_iso=stamp,
        note="engine-managed trail: one step per promote_uphill",
    )
    client.table(Tables.SPINE_SUBSTANCE).insert(rows).execute()
    return PROMOTIONS_EST_ITEM_ID


def leave_promotion_step(
    client: Any,
    *,
    parent_id: str,
    parent_code: str,
    child_code: str,
    item_kind: str,
    item_ref: str,
    text: str,
    note: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Append the promotion step to the parent's trail (a LIVE step — the
    promotion was an explicit human call, not machine-inferred progress)."""
    from cp_engine.spine_steps import add_step

    ensure_promotions_element(client, parent_id, parent_code)
    provenance = f"{item_kind} {item_ref} promoted from {child_code}"
    if note and note.strip():
        provenance += f" — {note.strip()}"
    return add_step(
        client,
        parent_id,
        PROMOTIONS_EST_ITEM_ID,
        promotion_step_title(child_code, text),
        status="done",
        step_date=(today or date.today()).isoformat(),
        note=provenance,
    )


# ──────────────────────────────────────────────────────────────────────
#  Commitments
# ──────────────────────────────────────────────────────────────────────


def promote_commitment(
    client: Any,
    *,
    tenant_root: Path,
    code: str,
    commitment_id: str,
    note: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Copy commitment `commitment_id` from `code` to its parent workstream."""
    from cp_engine.commitments import (
        INTERNAL,
        commitment_already_present,
        write_commitment,
    )

    child, parent = _parent_or_error(tenant_root, code)
    if child is None:
        return parent  # the error dict
    assert parent is not None
    parent_code = parent["code"]

    child_id = _resolve_project_id(client, child["code"])
    if child_id is None:
        return {"error": f"MC-2 resolves no project for {child['code']!r}"}
    parent_id = _resolve_project_id(client, parent_code)
    if parent_id is None:
        return {"error": f"MC-2 resolves no project for parent {parent_code!r}"}

    ref = (commitment_id or "").strip()
    if not ref:
        return {"error": "item_ref is required: the commitment id"}
    rows = (
        client.table(Tables.COMMITMENTS)
        .select(COMMITMENT_COPY_COLUMNS)
        .eq("id", ref)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        return {"error": f"no commitment with id {ref!r}"}
    row = rows[0]
    if str(row.get("project_id")) != str(child_id):
        return {
            "error": (
                f"commitment {ref} is not on {child['code']} — promote it from "
                "the workstream that owns it"
            )
        }

    new_hash = promoted_hash(row.get("cp_hash") or row["id"], parent_code)
    base = {
        "ok": True,
        "item_kind": "commitment",
        "item_ref": ref,
        "from": child,
        "to": parent,
        "level": parent,
        "cp_hash": new_hash,
    }
    if commitment_already_present(client, new_hash):
        return {**base, "promoted": False, "already": True}

    outcome = write_commitment(
        client,
        owner={"id": parent_id, "code": parent_code, "kind": "project"},
        description=row.get("description") or "",
        cp_hash=new_hash,
        source_kind=SOURCE_KIND_PROMOTED,
        direction=row.get("direction") or INTERNAL,
        owner_email=row.get("owner_email"),
        owner_name=row.get("owner_name"),
        due_date=row.get("due_date"),
        work_item_id=row.get("work_item_id"),
        work_item_kind=row.get("work_item_kind"),
        spine_element_id=row.get("spine_element_id"),
        source_meeting_id=row.get("source_meeting_id"),
    )
    if outcome == "duplicate":
        return {**base, "promoted": False, "already": True}

    step = leave_promotion_step(
        client,
        parent_id=parent_id,
        parent_code=parent_code,
        child_code=child["code"],
        item_kind="commitment",
        item_ref=ref,
        text=row.get("description") or "",
        note=note,
        today=today,
    )
    return {**base, "promoted": True, "already": False, "step": step}


# ──────────────────────────────────────────────────────────────────────
#  Decisions
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FoundDecision:
    text: str
    date: str
    hash: str | None
    cross_cutting: bool
    path: Path
    week: str


def _sprint_files(sprints_root: Path, stem: str) -> list[Path]:
    """Newest week first."""
    return sorted(sprints_root.glob(f"*/{stem}.md"), reverse=True)


def find_decision(tenant_root: Path, code: str, item_ref: str) -> FoundDecision | None:
    """Locate a decision bullet on `code` by its `cp:hash` or its exact text.

    Newest sprint file first; the match is exact — a substring would let a
    short ref promote the wrong decision, and there is no undo for a copy.
    """
    from cp_engine.sprints import resolve_sprint_code

    sprints_root = tenant_root / "sprints"
    stem = resolve_sprint_code(sprints_root, code)
    ref = _HASH_TRAILER_RE.sub("", (item_ref or "").strip())
    ref_text = " ".join(ref.split())
    if not ref_text:
        return None
    for path in _sprint_files(sprints_root, stem):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            m = _DECISION_RE.match(line)
            if not m:
                continue
            text = " ".join((m.group("text") or "").split())
            if not text:
                continue
            h = m.group("hash")
            if (h and h == ref_text.lower()) or text == ref_text:
                return FoundDecision(
                    text=text,
                    date=m.group("date").strip(),
                    hash=h,
                    cross_cutting=bool(m.group("cross")),
                    path=path,
                    week=path.parent.name,
                )
    return None


def _decision_already_promoted(sprints_root: Path, parent_stem: str, hash_hex: str) -> Path | None:
    marker = f"cp:hash={hash_hex}"
    for path in _sprint_files(sprints_root, parent_stem):
        try:
            if marker in path.read_text(encoding="utf-8"):
                return path
        except OSError:
            continue
    return None


def promote_decision(
    client: Any | None,
    *,
    tenant_root: Path,
    code: str,
    item_ref: str,
    note: str | None = None,
    today: date | None = None,
    week_iso: str | None = None,
) -> dict[str, Any]:
    """Copy a decision bullet from `code`'s sprint files into the parent's
    current sprint file. `client` may be None (no MC-2 creds): the bullet is
    still written and the missing spine step is reported, not hidden."""
    from cp_engine.ingest import _calendar_week_iso, _content_hash, _write_decision
    from cp_engine.sprints import resolve_sprint_code, scaffold_from_prior

    child, parent = _parent_or_error(tenant_root, code)
    if child is None:
        return parent  # the error dict
    assert parent is not None
    parent_code = parent["code"]

    found = find_decision(tenant_root, child["code"], item_ref)
    if found is None:
        return {
            "error": (
                f"no decision on {child['code']} matches {item_ref!r} — pass the "
                "bullet's cp:hash or its exact text"
            )
        }

    sprints_root = tenant_root / "sprints"
    parent_stem = resolve_sprint_code(sprints_root, parent_code, supabase=client)
    today = today or date.today()
    week = week_iso or _calendar_week_iso(today)
    text = f"{found.text} (promoted from {child['code']})"
    new_hash = _content_hash(parent_stem, "add-decision", text)
    base = {
        "ok": True,
        "item_kind": "decision",
        "item_ref": found.hash or found.text,
        "from": child,
        "to": parent,
        "level": parent,
        "cp_hash": new_hash,
        "source": str(found.path.relative_to(tenant_root)),
    }

    prior = _decision_already_promoted(sprints_root, parent_stem, new_hash)
    if prior is not None:
        return {
            **base,
            "promoted": False,
            "already": True,
            "sprint_path": str(prior.relative_to(tenant_root)),
        }

    parent_path = sprints_root / week / f"{parent_stem}.md"
    if not parent_path.exists():
        scaffolded = scaffold_from_prior(
            tenant_root=tenant_root,
            project_code=parent_stem,
            target_week_iso=week,
            supabase=client,
        )
        if scaffolded is None:
            return {
                "error": (
                    f"no sprint file for parent {parent_code} in {week} and "
                    "nothing to scaffold it from — run `cxp sync` first"
                )
            }

    written = _write_decision(
        parent_stem,
        {"text": text, "date": found.date, "cross_cutting": found.cross_cutting},
        parent_path,
        today=today,
    )
    if not written:
        return {
            **base,
            "promoted": False,
            "already": True,
            "sprint_path": str(parent_path.relative_to(tenant_root)),
        }

    result = {
        **base,
        "promoted": True,
        "already": False,
        "sprint_path": str(parent_path.relative_to(tenant_root)),
    }
    if client is None:
        result["step"] = {
            "note": "no MC-2 client — the parent's spine step was NOT written; "
            "re-run with credentials or add it by hand"
        }
        return result

    parent_id = _resolve_project_id(client, parent_code)
    if parent_id is None:
        result["step"] = {"error": f"MC-2 resolves no project for parent {parent_code!r}"}
        return result
    result["step"] = leave_promotion_step(
        client,
        parent_id=parent_id,
        parent_code=parent_code,
        child_code=child["code"],
        item_kind="decision",
        item_ref=found.hash or found.text,
        text=found.text,
        note=note,
        today=today,
    )
    return result


def promote_uphill(
    client: Any | None,
    *,
    tenant_root: Path,
    code: str,
    item_kind: str,
    item_ref: str,
    note: str | None = None,
    today: date | None = None,
    week_iso: str | None = None,
) -> dict[str, Any]:
    """Dispatch on `item_kind` ∈ decision | commitment."""
    kind = (item_kind or "").strip().lower()
    if kind not in ITEM_KINDS:
        return {"error": f"item_kind must be one of {list(ITEM_KINDS)}"}
    if kind == "commitment":
        if client is None:
            return {"error": "promoting a commitment needs an MC-2 client (the row lives there)"}
        return promote_commitment(
            client, tenant_root=tenant_root, code=code, commitment_id=item_ref,
            note=note, today=today,
        )
    return promote_decision(
        client, tenant_root=tenant_root, code=code, item_ref=item_ref,
        note=note, today=today, week_iso=week_iso,
    )
