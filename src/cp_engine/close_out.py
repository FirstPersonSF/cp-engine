"""The `cp close` close-out ritual scaffold (architecture review 2026-07-26 §5).

Closure today is a *move*, not a *ritual*: inactive projects freeze mid-flight
with "Next up" items that will never happen, thin spine stubs, and no terminal
synthesis. `cp close <code>` does NOT mutate any data — it emits a checklist
working file (`close-out-<code>-<date>.md` in the project's working dir) that
scaffolds the human/agent ritual:

  1. final-synthesis spine element TODO, with suggested `derives_from` targets
     (the project's live Synthesis/Output elements),
  2. terminal-form Exec Summary template (Objective past-tense / Outcome /
     where deliverables live / ≤3 bullets),
  3. open commitments enumerated for resolve/drop,
  4. thin-stub retire candidates (short body, nothing served) — listed
     verify-per-card, never auto-retired (the #112 lesson: 2 of 3 "redundant"
     cards were load-bearing),
  5. account-promotion candidates (stakeholder/synthesis elements worth
     `set_element_account_scope`),
  6. a working-dir hygiene note.

This module is the pure logic; the CLI verb lives in `cli_cmds/spine.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from cp_engine.state import INACTIVE_DIR_NAME
from cp_engine.status import is_active_initiative_status, is_active_status

# Body length below which an element counts as a thin stub (close-out retire
# candidate) when it also serves nothing. Matches the audit heuristic from
# cp-engine #112 / the 07-26 architecture review.
THIN_STUB_BODY_LEN = 200

# Layers whose live elements are the natural `derives_from` targets for the
# final synthesis — the "load-bearing cards" of §5 step 1.
SYNTHESIS_TARGET_LAYERS = frozenset({"Synthesis", "Output"})

# Layers worth considering for account promotion (§5 step 4): stakeholder
# dossiers and client-voice syntheses compound across future engagements.
PROMOTION_LAYERS = frozenset({"Stakeholders", "Synthesis"})

# Standing elements every spine carries by contract (Brief / SOW). They are
# often short but must never be pitched as retire candidates.
STANDING_LAYERS = frozenset({"Brief", "Agreement"})


@dataclass(frozen=True)
class CloseElement:
    """The slice of a spine element the close-out checklist reasons about."""

    key: str  # est_item_id (the addressable key MCP verbs resolve)
    title: str
    layer: str
    body_len: int
    serves: tuple[str, ...] = ()
    scope: str | None = None  # "account" when already promoted


def element_from_row(row: dict[str, Any]) -> CloseElement:
    """Build a CloseElement from a live `spine_substance` MC-2 row."""
    return CloseElement(
        key=str(row.get("est_item_id") or row.get("id") or ""),
        title=str(row.get("framing") or row.get("est_item_id") or row.get("id") or ""),
        layer=str(row.get("layer") or "Deliverables"),
        body_len=len(str(row.get("body") or "").strip()),
        serves=tuple(str(x) for x in (row.get("serves") or [])),
        scope=row.get("scope"),
    )


def element_from_spine(el: Any) -> CloseElement:
    """Build a CloseElement from a disk-parsed `SpineElement` (offline path)."""
    return CloseElement(
        key=str(el.id),
        title=str(el.title),
        layer=str(el.layer),
        body_len=len(str(el.body or "").strip()),
        serves=tuple(str(x) for x in (el.serves or ())),
        scope=None,  # scope is MC-2-only metadata; unknown offline
    )


def load_mirror_elements(workdir: Path) -> list[CloseElement]:
    """Offline/degraded fallback: read the ON-DISK spine mirror, read-only.

    A modern project's elements live under ``spine/`` as substance files —
    lowercase work-item phase dirs PLUS ``spine/_authored/`` (the DB→disk
    mirror of authored rows, where nearly all authored elements sit). The
    legacy reader (`spine.load_spine`) only walks the capitalized layer dirs
    and is effectively blind on modern projects, so this reader parses the
    substance files directly with `parse_substance`.

    Unlike `spine_substance_sync._load_substance_items`, ``_authored/`` is
    INCLUDED here: that reader's exclusion exists because its output flows
    disk→DB, which must never happen for DB-owned rows. This function's
    output only ever renders into a checklist — a pure read — so reading the
    authored mirror is safe and necessary.

    Legacy capitalized-layer element files (pre-substance projects) are still
    picked up via `load_spine`, deduped by key. Malformed files are skipped
    (a broken file must not abort the checklist); archived items and
    superseded versions are excluded, matching the live read's filters.
    """
    from cp_engine.spine import load_spine
    from cp_engine.substance import parse_substance

    out: list[CloseElement] = []
    seen: set[str] = set()
    spine_root = workdir / "spine"
    if spine_root.is_dir():
        for md in sorted(spine_root.glob("*/*.md")):
            parts = md.relative_to(spine_root).parts
            # Skip project context and frozen snapshots — but NOT _authored/
            # (see docstring), which is why is_skipped_spine_dir isn't used.
            if parts[0] == "_context" or any(
                p.endswith(".snapshots") for p in parts
            ):
                continue
            try:
                item = parse_substance(md)
            except Exception:  # noqa: BLE001 — malformed file: skip, don't abort
                continue
            if item.archived or item.est_item_id in seen:
                continue
            try:
                live = item.live_version()
            except ValueError:
                continue
            seen.add(item.est_item_id)
            out.append(
                CloseElement(
                    key=item.est_item_id,
                    title=live.framing or item.est_item_id,
                    layer=item.layer or "Deliverables",
                    body_len=len((live.body or "").strip()),
                    serves=item.serves,
                    scope=None,  # scope is MC-2-only metadata; unknown offline
                )
            )
    # Legacy capitalized-layer element files (old projects / transition trees).
    for el in load_spine(workdir):
        ce = element_from_spine(el)
        if ce.key not in seen:
            seen.add(ce.key)
            out.append(ce)
    return out


# ──────────────────────────────────────────────────────────────────────
#  Working-dir resolution (live OR parked)
# ──────────────────────────────────────────────────────────────────────


def find_close_workdir(tenant_root: Path, code: str) -> tuple[Path, bool]:
    """Resolve a project's working dir, INCLUDING inactive parking lots.

    `find_spine_dir` deliberately skips `inactive/` bins — but close-out
    targets are usually already parked. Returns ``(path, parked)`` where
    ``parked`` is True when the dir was found under an ``inactive/`` bin
    (per-account `1p/<company>/inactive/<dir>/`, or scope-level
    `<scope>/inactive/<account-or-dir>/`).

    Raises SpineDirNotFound when nothing matches anywhere.
    """
    from cp_engine.spine import SpineDirNotFound, find_spine_dir
    from cp_engine.sync import (
        _SCOPE_DIRS,
        _find_project_dir,
        _project_parent_dirs,
        scope_root,
    )

    try:
        return find_spine_dir(tenant_root, code), False
    except SpineDirNotFound:
        pass

    for scope in _SCOPE_DIRS:
        # Per-account inactive bins: 1p/<company>/inactive/<dir>/
        for parent in _project_parent_dirs(tenant_root, scope):
            hit = _find_project_dir(parent / INACTIVE_DIR_NAME, code)
            if hit is not None:
                return hit, True
        # Scope-level inactive bin: <scope>/inactive/<dir>/ directly (non-
        # nested scopes) or <scope>/inactive/<account>/<dir>/ (whole parked
        # accounts under an account-nested scope).
        scope_inactive = scope_root(tenant_root, scope) / INACTIVE_DIR_NAME
        if not scope_inactive.is_dir():
            continue
        hit = _find_project_dir(scope_inactive, code)
        if hit is not None:
            return hit, True
        for child in scope_inactive.iterdir():
            if child.is_dir():
                hit = _find_project_dir(child, code)
                if hit is not None:
                    return hit, True

    raise SpineDirNotFound(
        f"No working dir for '{code}' under {', '.join(_SCOPE_DIRS)}/ "
        f"(searched live dirs and inactive/ bins)"
    )


# ──────────────────────────────────────────────────────────────────────
#  Live reads (status, spine, commitments)
# ──────────────────────────────────────────────────────────────────────


def fetch_item_status(client: Any, code: str) -> tuple[str, str | None] | None:
    """Resolve `code` to ``(kind, status)`` from MC-2, or None if unknown.

    Same code grammar as `commitments.resolve_commitment_owner`: engagement
    codes carry a trailing number (→ `projects.mc_status`), bare slugs are
    initiatives (→ `initiatives.status`) with a repo fallback
    (→ `repos.status`).

    A projects-miss on the number branch FALLS THROUGH to the initiative and
    repo lookups rather than returning None: repo slugs whose second dash-
    segment is numeric (`mc-2`) parse as engagement numbers, and a short-
    circuit there would make such repos permanently unresolvable.
    """
    from cp_engine.clickup_routing import engagement_number
    from cp_engine.mc2_db import Tables

    number = engagement_number(code)
    if number is not None:
        rows = (
            client.table(Tables.PROJECTS)
            .select("id, number, mc_status")
            .eq("number", number)
            .execute()
            .data
        ) or []
        if rows:
            return "project", rows[0].get("mc_status")

    rows = (
        client.table(Tables.INITIATIVES)
        .select("id, code, status")
        .eq("code", code)
        .execute()
        .data
    ) or []
    if rows:
        return "initiative", rows[0].get("status")

    rows = (
        client.table(Tables.REPOS)
        .select("id, repo_name, status")
        .eq("repo_name", code)
        .execute()
        .data
    ) or []
    if rows:
        return "repo", rows[0].get("status")
    return None


def is_active_like(kind: str, status: str | None) -> bool:
    """True when this status means "still being worked" for its kind."""
    if kind == "project":
        return is_active_status(status)
    if kind == "initiative":
        return is_active_initiative_status(status)
    if kind == "repo":
        return status == "Active"
    return False


def fetch_live_spine_rows(client: Any, code: str) -> list[dict]:
    """One live `spine_substance` row per element, #113-deduped.

    `code` must be the DIR-SLUG (`spine_substance.project_code` — e.g.
    `ibx-5153-ai-campaign`, or the bare initiative slug), which is exactly
    the resolved working dir's name — pass `workdir.name`, not the short
    engagement code.
    """
    from cp_engine.mc2_db import Tables
    from cp_engine.project_sources import _one_live_per_element

    rows = (
        client.table(Tables.SPINE_SUBSTANCE)
        .select(
            "id, est_item_id, framing, layer, status, version_label, "
            "version_date, body, serves, scope, archived"
        )
        .eq("project_code", code)
        .eq("status", "live")
        .execute()
        .data
    ) or []
    return _one_live_per_element([r for r in rows if not r.get("archived")])


def fetch_open_commitments(client: Any, code: str) -> list[dict] | None:
    """The project's open commitments, or None when the code doesn't resolve
    to a commitments owner (standalone repos can't own commitments)."""
    from cp_engine.commitments import list_commitments, resolve_commitment_owner

    owner = resolve_commitment_owner(client, code)
    if owner is None:
        return None
    return list_commitments(client, owner, status="open")


# ──────────────────────────────────────────────────────────────────────
#  Checklist selectors
# ──────────────────────────────────────────────────────────────────────


def synthesis_targets(elements: list[CloseElement]) -> list[CloseElement]:
    """Live Synthesis/Output elements — suggested `derives_from` targets."""
    return [e for e in elements if e.layer in SYNTHESIS_TARGET_LAYERS]


def thin_stubs(elements: list[CloseElement]) -> list[CloseElement]:
    """Retire *candidates*: short body AND nothing served. Standing elements
    (Brief/Agreement) are exempt — short by design, never retired."""
    return [
        e
        for e in elements
        if e.body_len < THIN_STUB_BODY_LEN
        and not e.serves
        and e.layer not in STANDING_LAYERS
    ]


def promotion_candidates(elements: list[CloseElement]) -> list[CloseElement]:
    """Stakeholder/Synthesis elements not already account-scoped."""
    return [
        e
        for e in elements
        if e.layer in PROMOTION_LAYERS and e.scope != "account"
    ]


# ──────────────────────────────────────────────────────────────────────
#  Checklist rendering
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CloseChecklistInputs:
    """Everything the checklist renders — assembled by the CLI verb."""

    code: str
    status_label: str  # e.g. "Closed (engagement)" or "unverified — MC-2 unreachable"
    elements: list[CloseElement] = field(default_factory=list)
    # False when `elements` came from the on-disk mirror (MC-2 down or the
    # live spine read failed) rather than a live MC-2 read. The renderer
    # stamps the provenance into the file and never renders a pre-checked
    # `[x]` empty state from mirror data — same None-vs-empty discipline as
    # `commitments` below.
    spine_verified: bool = True
    # None = commitments unreadable (MC-2 down / repo); [] = verified empty.
    commitments: list[dict] | None = None
    workdir_root_files: list[str] = field(default_factory=list)


def _el_line(e: CloseElement, extra: str = "") -> str:
    tail = f" {extra}" if extra else ""
    return f"- [ ] `{e.key}` — {e.title}{tail}"


def _commitment_line(row: dict) -> str:
    due = row.get("due_date") or "undated"
    owner = row.get("owner_name") or row.get("owner_email") or "unowned"
    direction = row.get("direction") or "?"
    desc = str(row.get("description") or "").strip()
    return f"- [ ] resolve/drop — {desc} ({direction}; owner: {owner}; due: {due})"


def build_close_checklist(
    inputs: CloseChecklistInputs, today: date | None = None
) -> str:
    """Render the close-out checklist markdown (the whole working file)."""
    today = today or date.today()
    code = inputs.code
    lines: list[str] = [
        "---",
        f"Project: {code}",
        f"Provenance: Version 01 | {today.isoformat()}",
        f"Filename: close-out-{code}-{today.isoformat()}.md",
        "Author: cp-engine",
        "---",
        "",
        f"# Close-out ritual — {code}",
        "",
        f"Status at scaffold time: **{inputs.status_label}**. Generated by "
        "`cp close` — a checklist, not an action log. Every box is a human/"
        "agent judgment call; nothing below has been done automatically.",
        "",
    ]
    if not inputs.spine_verified:
        lines.extend(
            [
                "> **Spine UNVERIFIED** — element data below came from the "
                "on-disk mirror (MC-2 unreachable at scaffold time), which "
                "may lag the live spine. Re-check with "
                "`list_spine_elements` before retiring or promoting "
                "anything.",
                "",
            ]
        )
    lines.extend([
        "## 1 · Final synthesis spine element",
        "",
        "- [ ] Author ONE terminal synthesis element (`create_spine_element`, "
        "type `synthesis`): what was made, what worked, what the client "
        "valued, reusable artifacts.",
        "- [ ] Wire `derives_from` edges (`create_spine_relation`) to the "
        "load-bearing cards. Suggested targets (live Synthesis/Output "
        "elements):",
    ])
    targets = synthesis_targets(inputs.elements)
    if targets:
        lines.extend(f"  {_el_line(e)}" for e in targets)
    else:
        lines.append(
            "  - (no live Synthesis/Output elements found — pick the "
            "load-bearing cards by hand)"
        )

    lines.extend(
        [
            "",
            "## 2 · Exec Summary → terminal form",
            "",
            "- [ ] Rewrite the `cp.md` Exec Summary between the exec-summary "
            "markers to the terminal shape below. Delete Next up / Blockers "
            "— nothing is next.",
            "",
            "```markdown",
            f"## Exec Summary  ·  updated {today.isoformat()}",
            "",
            "**Objective (past tense):** <what this engagement set out to do>",
            "**Outcome:** <what actually shipped / how it landed>",
            "**Deliverables live at:** <Dropbox path(s)>",
            "",
            "- <≤3 bullets: the durable takeaways>",
            "```",
            "",
            "## 3 · Open commitments — resolve or drop",
            "",
        ]
    )
    if inputs.commitments is None:
        lines.append(
            "- (commitments unavailable — MC-2 unreachable or this item "
            "cannot own commitments; re-check with `list_commitments` before "
            "closing)"
        )
    elif not inputs.commitments:
        lines.append("- [x] No open commitments — verified live.")
    else:
        lines.extend(_commitment_line(r) for r in inputs.commitments)
        lines.append("")
        lines.append(
            "Close each with `resolve_commitment(code, key, outcome)` — "
            "`done` or `dropped`; never leave open rows on a closed project."
        )

    lines.extend(
        [
            "",
            "## 4 · Thin-stub retire candidates — VERIFY PER CARD",
            "",
            f"Candidates = live body < {THIN_STUB_BODY_LEN} chars AND serving "
            "nothing. **Do not batch-retire**: pull each card first "
            "(`pull_spine_element`) — the #112 lesson: 2 of 3 \"redundant\" "
            "cards were load-bearing.",
            "",
        ]
    )
    stubs = thin_stubs(inputs.elements)
    if stubs:
        lines.extend(
            _el_line(e, f"({e.body_len} chars, serves nothing)") for e in stubs
        )
    elif inputs.spine_verified:
        lines.append("- [x] No thin-stub candidates found — verified live.")
    else:
        lines.append(
            "- [ ] No thin-stub candidates in the on-disk mirror — "
            "UNVERIFIED; re-check with `list_spine_elements` before closing."
        )

    lines.extend(
        [
            "",
            "## 5 · Account-promotion candidates",
            "",
            "Anything account-durable (stakeholder dossiers, client-voice "
            "syntheses) compounds across future engagements with this client. "
            "Promote with `set_element_account_scope(code, key, account=true)`:",
            "",
        ]
    )
    promos = promotion_candidates(inputs.elements)
    if promos:
        lines.extend(_el_line(e, f"[{e.layer}]") for e in promos)
    elif inputs.spine_verified:
        lines.append(
            "- [x] No promotion candidates (or all already promoted) — "
            "verified live."
        )
    else:
        lines.append(
            "- [ ] No promotion candidates in the on-disk mirror — "
            "UNVERIFIED (mirror lacks account-scope metadata); re-check "
            "with `list_spine_elements` before closing."
        )

    lines.extend(
        [
            "",
            "## 6 · Working-dir hygiene",
            "",
            "- [ ] Move superseded drafts to `work/` (or delete where Dropbox "
            "holds the original). Root keeps `cp.md` + final build notes.",
            "- [ ] Confirm binaries live in Dropbox, not the repo "
            "(per `.gitignore`).",
        ]
    )
    if inputs.workdir_root_files:
        lines.append("")
        lines.append("Root files at scaffold time:")
        lines.extend(f"- `{name}`" for name in sorted(inputs.workdir_root_files))
    lines.append("")
    return "\n".join(lines)
