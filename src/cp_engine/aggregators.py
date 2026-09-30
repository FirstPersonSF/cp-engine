"""Projections of sprint-file content into durable surfaces.

Phase 1.2 (v0.8.5) introduced the engine-managed strip regions on per-project
`cp.md` files and the cross-cutting rollup. All of them aggregate sprint-file
handwritten content into durable, projectable views.

Two aggregator functions, separated by scope:

1. ``aggregate_project_strips(project_code, sprint_files, today)`` — projects
   per-project content (Inbound, Decisions, Open asks, Stakeholders) into
   the four new regions on a project's ``cp.md``.

2. ``aggregate_subtree_strips(root_code, sprint_files, themes, today,
   by_code)`` — projects cross-cutting decisions, themes and carry-forward
   for the SUBTREE rooted at ``root_code`` (``None`` = the whole tenant).
   The tenant call feeds ``master-cp.md``'s ``agenda`` region and the prep
   agenda header; the per-node calls feed an account node's or a program's
   sprint file ``carry-forward`` region (cp-engine #303, plan §3.5).
   ``aggregate_tenant_strips`` is the tenant-rooted spelling.

The existing ``_compute_agenda_rollup`` in ``render.py`` shares its core logic
with ``aggregate_tenant_strips``'s carry-forward output — same parser, same
data, different render template. The shared helper is ``_carry_forward_rollup``
below.

Time windows are pragmatic, not load-bearing:
- Project strips: last 4 weeks (28 days) for inbound + decisions; all-time
  for open asks (until closed) and stakeholders (dedupe by name).
- Tenant strips: last 2 weeks for themes; last 4 weeks for cross-cutting
  decisions; carry-forward is severity/age-gated, not time-gated.

Tunable later if these defaults turn out wrong on real tenant data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping

from cp_engine.snooze import active_snooze
from cp_engine.state import (
    ClientAsk,
    DecisionEntry,
    InboundUpdate,
    ProjectState,
    Risk,
    SprintFile,
    Stakeholder,
    Theme,
    descendants_of,
)

# Project-strip window: last 4 weeks for recent-decisions + inbound.
_PROJECT_RECENCY_DAYS = 28

# Tenant-strip windows.
_TENANT_DECISIONS_DAYS = 28
_TENANT_THEMES_DAYS = 14

# Stale-ask threshold: matches the existing `agenda` rule in render.py.
_STALE_ASK_DAYS = 7


# ──────────────────────────────────────────────────────────────────────
#  Open asks — the one definition every surface counts and lists
# ──────────────────────────────────────────────────────────────────────

_HASH_RE = re.compile(r"cp:hash=([0-9a-f]{8})")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _ask_key(ask: ClientAsk) -> str:
    """Identity for de-duplicating an ask across a file's two regions: its
    cp:hash when it has one, else its visible text."""
    return _bullet_key(ask.text)


def open_client_asks(sf: SprintFile) -> list[ClientAsk]:
    """Every ask still open in ``sf``: this week's own `### Open asks`
    bullets with status ``open``, then the ones carried forward from earlier
    weeks, de-duplicated (cp-engine #323).

    Surfaces used to disagree about what "open asks" meant. The cp.md
    ``current-sprint`` header counted ``len(sf.client_open_asks)`` — every
    bullet in the section, answered and dropped ones included, and none of
    the carried ones — while the list beneath it fell back to carry-forward
    asks. So a project whose only open ask had been carried rendered
    "Open client asks (0)" above a one-item list, and closing an ask in its
    own week never moved the count. The ``open-asks-strip`` and the master
    agenda read only the own section and said "No open asks" beside it.

    One function, used for the count AND the list on every surface, so
    they can't drift again. Carried asks are already status-filtered by
    ``compute_carry_forward``, and the region is re-derived from the prior
    week on every render — a resolution in the owning week drops the ask
    here on the next sync. The live section wins a duplicate (a hand-
    restated ask may carry updated wording), mirroring ``_active_risks``.

    That includes a restatement that CLOSES it (#326): an ask marked
    ``closed``/``answered`` in this week's own section suppresses its carried
    copy, the same newest-statement-wins rule ``compute_carry_forward``
    applies across earlier weeks — otherwise the count would say open while
    next week's carry-forward had already let it go.

    Stale asks (first raised more than ``CARRY_FORWARD_MAX_AGE_WEEKS`` ago)
    are NOT in this list: carry-forward rolls them into one summary line
    rather than carrying them as bullets, so every surface built on this
    function counts live asks only. The current-sprint header names the
    stale count separately (``sf.carry_forward.stale_count("asks")``).
    """
    out: list[ClientAsk] = []
    seen: set[str] = set()
    for ask in sf.client_open_asks:
        key = _ask_key(ask)
        if key in seen:
            continue
        seen.add(key)
        if ask.status != "open":
            continue
        out.append(ask)
    for ask in sf.carry_forward.asks:
        key = _ask_key(ask)
        if key in seen:
            continue
        seen.add(key)
        out.append(ask)
    return out


# ──────────────────────────────────────────────────────────────────────
#  Inbound reconciliation against the live source store
# ──────────────────────────────────────────────────────────────────────

# The announcement ``ingest.announce_new_sources`` writes into `### Inbound`:
#   [<date> · source ingest] **New source ingested:** <title> (<type>)
#       [— **N reviewer comments inside**] — full text via `pull_project_source`.
# The title is greedy up to the LAST parenthesised type before the tail, so a
# title that itself contains parentheses still parses whole.
SOURCE_INGEST_WHO = "source ingest"
_ANNOUNCEMENT_RE = re.compile(
    r"^\*\*New source ingested:\*\* (?P<title>.+) \([^()]*\)"
    r"(?: — \*\*\d+ reviewer comments inside\*\*)? — full text via"
)
# Announcements written since cp-engine #329 also carry the asset's id, AFTER
# the cp:hash marker and outside the hashed text — so the id never changed a
# hash and pre-#329 bullets (title only) stay byte-identical and idempotent.
ASSET_MARKER_FMT = "<!-- cp:asset={} -->"
_ASSET_MARKER_RE = re.compile(r"<!-- cp:asset=([\w-]+) -->")


def announced_source_title(ib: InboundUpdate) -> str | None:
    """The source title a new-source announcement names, or ``None`` when
    ``ib`` is not one (a meeting, a hand-written inbound)."""
    if ib.who.strip() != SOURCE_INGEST_WHO:
        return None
    m = _ANNOUNCEMENT_RE.match(ib.text.strip())
    return m.group("title").strip() if m else None


def normalize_source_title(title: str | None) -> str:
    """Compare titles the way the announcement wrote them: the writer
    collapses whitespace (``ingest._sanitize_inline_text``) before hashing
    and writing, so a double space in the stored title must not read as a
    different document."""
    return " ".join((title or "").split())


def announced_asset_id(ib: InboundUpdate) -> str | None:
    """The asset id a (post-#329) announcement carries, or ``None``."""
    m = _ASSET_MARKER_RE.search(ib.text)
    return m.group(1) if m else None


@dataclass(frozen=True)
class LiveSources:
    """What the source store says is live for one project, for reconciling
    the inbound strip's new-source announcements (#323, #329).

    - ``by_id``: live asset id → its CURRENT title. Every field here comes
      from the manifest pass's ``list_sources`` result — no extra query.
    - ``lineage``: optional ``rag_assets`` rows of ANY status for the
      project — ``{id, title, prev_asset_id, supersedes_asset_id}`` — from
      ``project_sources.fetch_asset_lineage``, the one batched query sync
      makes only when some announcement matches neither a live id nor a
      live title. Empty = no lineage this run (not needed, or the query
      failed): an unresolved announcement then drops, exactly as in #323.
    """

    by_id: Mapping[str, str]
    lineage: tuple[dict, ...] = ()

    @classmethod
    def from_assets(cls, assets: list) -> "LiveSources":
        return cls(by_id={
            str(a.get("id")): a.get("title") or ""
            for a in assets if isinstance(a, dict) and a.get("id")
        })

    @property
    def titles(self) -> frozenset[str]:
        return frozenset(normalize_source_title(t) for t in self.by_id.values())

    def with_lineage(self, rows: list) -> "LiveSources":
        return LiveSources(by_id=self.by_id, lineage=tuple(
            r for r in rows if isinstance(r, dict) and r.get("id")
        ))


def _resolves_directly(ib: InboundUpdate, live: LiveSources, titles: frozenset[str]) -> bool:
    """True when an announcement matches a live asset without lineage: its
    carried id is live, or its title is. Non-announcements are always True."""
    title = announced_source_title(ib)
    if title is None:
        return True
    asset_id = announced_asset_id(ib)
    if asset_id is not None and asset_id in live.by_id:
        return True
    return normalize_source_title(title) in titles


def needs_lineage(
    sprint_files: tuple[SprintFile, ...], live: LiveSources
) -> bool:
    """Whether any announcement in ``sprint_files`` needs the supersede chain
    to decide renamed-vs-gone — sync's gate for the one lineage query."""
    titles = live.titles
    return any(
        not _resolves_directly(ib, live, titles)
        for sf in sprint_files for ib in sf.client_inbound
    )


def _live_descendant(
    start_ids: list[str], live: LiveSources
) -> str | None:
    """Walk ``prev_asset_id`` / ``supersedes_asset_id`` forward from
    ``start_ids`` to the first LIVE asset, or ``None`` when no live
    descendant exists (the source is gone). Both pointers live on the NEWER
    row and name the older one; a visited set bounds a malformed cycle."""
    children: dict[str, list[str]] = {}
    for row in live.lineage:
        for parent in (row.get("prev_asset_id"), row.get("supersedes_asset_id")):
            if parent:
                children.setdefault(str(parent), []).append(str(row["id"]))
    frontier = list(start_ids)
    seen: set[str] = set()
    while frontier:
        aid = frontier.pop(0)
        if aid in seen:
            continue
        seen.add(aid)
        if aid in live.by_id:
            return aid
        frontier.extend(children.get(aid, ()))
    return None


def _reconcile_announcements(
    inbound: list[InboundUpdate], live: LiveSources
) -> list[InboundUpdate]:
    """Keep an announcement while its source is still live, under whatever
    title it now carries; drop it when no live asset answers it (#323, #329).

    Resolution order, cheapest first:
      1. the carried asset id is live → keep. A source renamed in place
         (``rename_project_source`` updates ``rag_assets.title`` on the same
         row) is found here, and the bullet shows its current title;
      2. the announced title is live → keep (the #323 rule; also covers a
         same-title re-ingest, whose ``prev_asset_id`` chain keeps the title);
      3. the id, or any retired row bearing the title, has a live descendant
         in ``live.lineage`` → keep under the descendant's title — UNLESS that
         descendant is announced elsewhere in the strip, which already
         represents the source (showing both would list one document twice);
      4. otherwise → gone; drop.

    A pre-#329 bullet carries no id, and ``rename_project_source`` records no
    title history, so a legacy bullet for a source renamed IN PLACE cannot be
    told from a deleted one and still drops. It ages out of the 28-day window
    within four weeks; every announcement written since carries the id.
    """
    titles = live.titles
    # Which live assets the strip already announces — by id or by title.
    announced_live: set[str] = set()
    title_to_live = {normalize_source_title(t): aid for aid, t in live.by_id.items()}
    for ib in inbound:
        if announced_source_title(ib) is None:
            continue
        aid = announced_asset_id(ib)
        if aid in live.by_id:
            announced_live.add(aid)
        hit = title_to_live.get(normalize_source_title(announced_source_title(ib)))
        if hit:
            announced_live.add(hit)

    out: list[InboundUpdate] = []
    for ib in inbound:
        title = announced_source_title(ib)
        if title is None:
            out.append(ib)
            continue
        aid = announced_asset_id(ib)
        if aid is not None and aid in live.by_id:
            out.append(_retitled(ib, title, live.by_id[aid]))
            continue
        if normalize_source_title(title) in titles:
            out.append(ib)
            continue
        if not live.lineage:
            continue  # no chain to consult: not live = gone (#323)
        norm = normalize_source_title(title)
        starts = ([aid] if aid else []) + [
            str(r["id"]) for r in live.lineage
            if normalize_source_title(r.get("title")) == norm
            and str(r["id"]) not in live.by_id
        ]
        heir = _live_descendant(starts, live)
        if heir is None or heir in announced_live:
            continue
        announced_live.add(heir)
        out.append(_retitled(ib, title, live.by_id[heir]))
    return out


def _retitled(ib: InboundUpdate, old: str, current: str) -> InboundUpdate:
    """``ib`` naming its source's current title when that title changed."""
    if not current or normalize_source_title(current) == normalize_source_title(old):
        return ib
    head = f"**New source ingested:** {old}"
    text = ib.text.strip()
    if not text.startswith(head):
        return ib
    new_head = f"**New source ingested:** {current} (renamed from “{old}”)"
    return InboundUpdate(date=ib.date, who=ib.who, text=new_head + text[len(head):])


def _bullet_key(text: str) -> str:
    """Identity for de-duplicating a bullet seen in more than one sprint
    file: its cp:hash when it has one, else its visible text."""
    m = _HASH_RE.search(text)
    if m:
        return m.group(1)
    return " ".join(_HTML_COMMENT_RE.sub(" ", text).split()).lower()


def _newest_first_unique(rows: list[tuple[date, int, object, str]]) -> list:
    """Order ``(date, recency, item, key)`` rows newest first and drop repeat
    keys, keeping the newest copy. ``recency`` breaks same-day ties: a later
    sprint week, then a later position in its file, is newer."""
    rows = sorted(rows, key=lambda r: (r[0], r[1]), reverse=True)
    out, seen = [], set()
    for _d, _r, item, key in rows:
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# ──────────────────────────────────────────────────────────────────────
#  Project strips (project cp.md regions)
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProjectStrips:
    """Aggregated content for one project's four cp.md engine regions.

    Each field maps 1:1 to a region:
    - ``inbound`` → ``inbound-strip``
    - ``recent_decisions`` → ``recent-decisions-strip``
    - ``open_asks`` → ``open-asks-strip``
    - ``stakeholders`` → ``stakeholders-strip``

    ``inbound_overflow`` / ``decisions_overflow`` count the in-window items
    left out by ``INBOUND_STRIP_CAP`` / ``DECISIONS_STRIP_CAP`` (the renderer
    names the remainder).
    """

    inbound: tuple[InboundUpdate, ...]
    recent_decisions: tuple[DecisionEntry, ...]
    open_asks: tuple[dict, ...]  # {text, asked_date, who, aged_days, snoozed_until}
    stakeholders: tuple[Stakeholder, ...]
    inbound_overflow: int = 0
    decisions_overflow: int = 0


# The windowed strips (inbound, recent decisions) list at most this many
# items each, newest first (cp-engine #328). Four weeks of a busy engagement
# is a lot: slt-5196 carried 68 in-window inbound bullets on 2026-09-29, 55 of
# them from one week of pre-interviews. The sprint files hold the rest, and
# each strip says how many it left out. The caps differ because the strips
# read differently: inbound is a feed to skim (25), while each decision is a
# multi-clause line that has to be read to be useful, so ten is what a reader
# actually takes in before the strip stops being a summary.
INBOUND_STRIP_CAP = 25
DECISIONS_STRIP_CAP = 10


def aggregate_project_strips(
    project_code: str,
    sprint_files: tuple[SprintFile, ...],
    today: date,
    live_source_titles: "frozenset[str] | None" = None,
    *,
    window_files: tuple[SprintFile, ...] = (),
    live_sources: "LiveSources | None" = None,
) -> ProjectStrips:
    """Aggregate per-project content from this project's sprint files.

    Only sprint files matching ``project_code`` contribute.

    ``window_files`` are the project's EARLIER sprint weeks inside the
    28-day window (cp-engine #328). They feed only the two strips whose
    heading promises "last 4 weeks" — inbound and recent decisions. Sync
    used to hand this function the current week alone, so a strip headed
    "last 4 weeks" showed one week. Open asks and stakeholders are NOT
    widened: the current week's carry-forward already holds every ask still
    open (an older week's copy may since have been answered), and the
    stakeholders heading makes no time promise. Both windowed strips are
    de-duplicated across weeks by cp:hash (a bullet re-recorded in a later
    week appears once), ordered newest first by bullet date, and capped at
    ``INBOUND_STRIP_CAP`` and ``DECISIONS_STRIP_CAP`` respectively.

    ``live_sources`` reconciles the inbound strip against the source store
    (cp-engine #323, #329; see ``_reconcile_announcements``). A new-source
    announcement is an EVENT — "this was ingested" — written once into the
    sprint file, and the strip kept listing it after the source was
    archived, superseded or deleted ("COPY ME" template copies, "- Copy"
    dupes). An announcement whose source no longer resolves to a live asset
    is left out of the strip; the sprint file still records it. ``None``
    means "the store wasn't reachable this run" and keeps every
    announcement — an unreachable MC-2 must never blank the strip.
    Hand-written and meeting inbound are never filtered.
    ``live_source_titles`` is the #323 spelling: a bare set of normalized
    live titles, reconciled by title alone.

    Open asks come from ``open_client_asks`` (own + carried, de-duplicated),
    and each carries ``snoozed_until`` — the in-force snooze date or
    ``None`` — so the renderer can mark it (see ``cp_engine.snooze``).
    """
    if live_sources is None and live_source_titles is not None:
        live_sources = LiveSources(by_id={t: t for t in live_source_titles})
    relevant = sorted(
        (sf for sf in sprint_files if sf.project_code == project_code),
        key=lambda s: s.week_start,
        reverse=True,
    )
    windowed = sorted(
        {id(sf): sf for sf in (*relevant, *window_files)
         if sf.project_code == project_code}.values(),
        key=lambda s: s.week_start,
        reverse=True,
    )
    cutoff = today - timedelta(days=_PROJECT_RECENCY_DAYS)

    inbound_rows: list[tuple] = []
    decision_rows: list[tuple] = []
    open_asks: list[dict] = []
    stakeholder_by_name: dict[str, Stakeholder] = {}

    for week_rank, sf in enumerate(reversed(windowed)):
        # Inbound: filter by date within window. Parser may emit empty `date`
        # for malformed brackets; skip those rather than guess.
        for pos, ib in enumerate(sf.client_inbound):
            d = _parse_iso_date(ib.date)
            if d is None or d < cutoff:
                continue
            inbound_rows.append((d, (week_rank, pos), ib, _bullet_key(ib.text)))

        # Recent decisions (bracket-formatted, v0.8.5+).
        for pos, dec in enumerate(sf.decisions):
            d = _parse_iso_date(dec.date)
            if d is None or d < cutoff:
                continue
            decision_rows.append((d, (week_rank, pos), dec, _bullet_key(dec.text)))

    for sf in relevant:
        # Open asks: keep all open asks regardless of age; the rendered
        # surface highlights stale ones via aged_days. Same set the
        # current-sprint strip counts — own AND carried (#323).
        for ask in open_client_asks(sf):
            asked = _parse_iso_date(ask.asked_date)
            aged_days = (today - asked).days if asked else None
            open_asks.append(
                {
                    "text": ask.text,
                    "asked_date": ask.asked_date,
                    "who": ask.who,
                    "aged_days": aged_days,
                    "snoozed_until": active_snooze(ask.text, today),
                }
            )

        # Stakeholders: dedupe by name across all sprints. Most-recent
        # mention wins for role + context (since we iterate newest first
        # and only insert when the name is new).
        for sh in sf.stakeholders:
            if sh.name not in stakeholder_by_name:
                stakeholder_by_name[sh.name] = sh

    inbound = _newest_first_unique(inbound_rows)
    if live_sources is not None:
        inbound = _reconcile_announcements(inbound, live_sources)
    decisions = _newest_first_unique(decision_rows)

    return ProjectStrips(
        inbound=tuple(inbound[:INBOUND_STRIP_CAP]),
        recent_decisions=tuple(decisions[:DECISIONS_STRIP_CAP]),
        open_asks=tuple(open_asks),
        stakeholders=tuple(stakeholder_by_name.values()),
        inbound_overflow=max(0, len(inbound) - INBOUND_STRIP_CAP),
        decisions_overflow=max(0, len(decisions) - DECISIONS_STRIP_CAP),
    )


# ──────────────────────────────────────────────────────────────────────
#  Subtree strips (tenant root, account nodes, programs)
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TenantStrips:
    """Aggregated cross-cutting content for one subtree of the tree.

    - ``cross_cutting_decisions``: DecisionEntry rows flagged
      ``cross_cutting=True`` across the subtree's sprint files in the window.
    - ``themes``: parsed from ``sprints/<W##>/_week.md`` ``## Themes``
      sections in the window (tenant-wide by nature; the caller passes them).
    - ``carry_forward``: open asks aged > 7 days, escalated risks, decisions
      due — the same rollup ``master-cp.md``'s ``agenda`` region renders.
    - ``root_code``: the subtree root, ``None`` for the tenant.
    - ``workstream_count``: how many sprint files fed the rollup.

    The class keeps its tenant-era name: every consumer reads the same
    three fields whether the root is the tenant or one node.
    """

    cross_cutting_decisions: tuple[dict, ...]  # {project_code, text, date}
    themes: tuple[Theme, ...]
    carry_forward: dict  # {escalated_risks, stale_asks, decisions_due}
    root_code: str | None = None
    workstream_count: int = 0


def aggregate_subtree_strips(
    root_code: str | None,
    sprint_files: tuple[SprintFile, ...],
    themes: tuple[Theme, ...],
    today: date,
    by_code: "Mapping[str, ProjectState] | None" = None,
) -> TenantStrips:
    """Aggregate cross-cutting content for the subtree rooted at ``root_code``.

    ``None`` is the tenant: every sprint file contributes. Otherwise only
    the files of ``root_code`` and its descendants (via ``parent_code`` in
    ``by_code``) contribute — an account node's rollup is its jobs and
    programs, a program's is its jobs. A root with no roster entry rolls up
    only its own file.

    ``themes`` is passed in (rather than discovered here) because the caller
    knows the tenant root and can collect ``sprints/<W##>/_week.md`` files
    across recent weeks. Aggregator filters by recency.
    """
    if root_code is not None:
        in_scope = {root_code} | {p.code for p in descendants_of(root_code, by_code or {})}
        sprint_files = tuple(sf for sf in sprint_files if sf.project_code in in_scope)

    cutoff_decisions = today - timedelta(days=_TENANT_DECISIONS_DAYS)
    cutoff_themes = today - timedelta(days=_TENANT_THEMES_DAYS)

    cross_cutting: list[dict] = []
    for sf in sprint_files:
        for dec in sf.decisions:
            if not dec.cross_cutting:
                continue
            d = _parse_iso_date(dec.date)
            if d is None or d < cutoff_decisions:
                continue
            cross_cutting.append(
                {
                    "project_code": sf.project_code,
                    "text": dec.text,
                    "date": dec.date,
                }
            )

    filtered_themes = tuple(
        t for t in themes
        if (d := _parse_iso_date(t.date)) is not None and d >= cutoff_themes
    )

    carry_forward = _carry_forward_rollup(sprint_files, today)

    return TenantStrips(
        cross_cutting_decisions=tuple(cross_cutting),
        themes=filtered_themes,
        carry_forward=carry_forward,
        root_code=root_code,
        workstream_count=len({sf.project_code for sf in sprint_files}),
    )


def aggregate_tenant_strips(
    sprint_files: tuple[SprintFile, ...],
    themes: tuple[Theme, ...],
    today: date,
) -> TenantStrips:
    """The tenant-rooted subtree: ``aggregate_subtree_strips(None, …)``."""
    return aggregate_subtree_strips(None, sprint_files, themes, today)


# ──────────────────────────────────────────────────────────────────────
#  Shared carry-forward rollup (master-cp.md agenda + subtree carry-forward)
# ──────────────────────────────────────────────────────────────────────


def carry_forward_rollup(
    sprint_files: tuple[SprintFile, ...],
    today: date,
) -> dict:
    """Public entry point for the rollup used by master-cp.md's `agenda`
    region and a parent node's sprint-file `carry-forward` region.

    Both consumers want the same three lists; only the render template
    differs. Sharing the parser means a bug fix (or a threshold change)
    lands in one place.
    """
    return _carry_forward_rollup(sprint_files, today)


def _carry_forward_rollup(
    sprint_files: tuple[SprintFile, ...],
    today: date,
) -> dict:
    escalated_risks: list[dict] = []
    stale_asks: list[dict] = []
    decisions_due: list[dict] = []

    monday = today - timedelta(days=today.weekday())
    # ISO 8601 week number (v0.10.0+); was `%W` previously, which produced
    # ISO_week - 1 for all of 2026 and disagreed with the rest of cp.
    current_week_num = monday.isocalendar().week

    for sf in sprint_files:
        # An escalation surface: a snoozed risk or ask is omitted until its
        # date (the contract in cp_engine.snooze).
        for risk in sf.risks:
            if risk.severity == "escalated" and active_snooze(risk.text, today) is None:
                escalated_risks.append(
                    {"project_code": sf.project_code, "text": risk.text}
                )

        # Own AND carried open asks (#323) — a carried ask is by construction
        # the oldest one, so reading only the own section hid exactly the
        # asks this rollup exists to surface.
        for ask in open_client_asks(sf):
            if active_snooze(ask.text, today) is not None:
                continue
            asked = _parse_iso_date(ask.asked_date)
            if asked is None:
                continue
            aged_days = (today - asked).days
            if aged_days > _STALE_ASK_DAYS:
                stale_asks.append(
                    {
                        "project_code": sf.project_code,
                        "text": ask.text,
                        "aged_days": aged_days,
                    }
                )

        for h in sf.horizon:
            if h.bucket != "decision" or not h.is_open:
                continue
            target_week = _parse_week_target(h.target_date)
            # Non-week targets (empty, "TBD", literal dates) → over-surface
            # (None case). Week targets → only surface if +1 or +2 sprints
            # from current (matches render.py's existing agenda behavior).
            if target_week is None or (target_week - current_week_num) in (1, 2):
                decisions_due.append(
                    {
                        "project_code": sf.project_code,
                        "text": h.text,
                        "target_date": h.target_date or "",
                    }
                )

    return {
        "escalated_risks": escalated_risks,
        "stale_asks": stale_asks,
        "decisions_due": decisions_due,
    }


# ──────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────


def _parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _parse_week_target(target: str | None) -> int | None:
    """Parse `W19`, `by W19`, `2026-W19` etc. into the week number.

    Returns None for non-week formats (TBD, literal date strings) so the
    caller can decide to over-surface rather than silently drop.
    """
    if not target:
        return None
    import re
    m = re.search(r"W(\d{1,2})", target)
    if not m:
        return None
    return int(m.group(1))
