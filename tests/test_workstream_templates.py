"""Template collapse, the `active-tree` region, subtree rollup strips
(cp-engine #303; plan §3.4–3.5, decision D9).

Four things this file pins:

1. **Region-set migration.** A cp.md scaffolded by the retired account or
   initiative templates carries the OLD region set; sync must bring it to
   the unified set in place — insert what is missing at the template's
   position, retire `account-facts` / `projects`, and leave every byte of
   hand-written text where it was. Same for a master-cp.md still carrying
   the five per-scope active tables: they go, `active-tree` lands where
   `active-1p` was. Tested on a COPY of the committed `master-cp-full`
   golden from before the collapse, never on the live tenant.
2. **Tree ordering and indentation.** Clients by name, then First Person,
   then Canonic; the account node at depth 0; `└─` per level.
3. **Envelope-strip states.** Unknown flags render "—", not "none".
4. **Subtree rollup.** `aggregate_subtree_strips` scopes to a root's
   descendants; the tenant root sees everything; sync writes a parent's
   rollup from its children's files and the parser never reads it back as
   the parent's own asks/risks.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from cp_engine import sync_tenant
from cp_engine.aggregators import aggregate_subtree_strips, aggregate_tenant_strips
from cp_engine.render import (
    PROJECT_CP_RETIRED_REGIONS,
    _envelope_view,
    _tree_prefix,
    ensure_regions,
    has_region,
    migrate_regions,
    project_cp_regions,
    region_block,
    render_master_cp,
    render_project_cp,
    retire_regions,
    splice_managed_region,
)
from cp_engine.sprints import parse_sprint_file
from cp_engine.state import (
    CarryForward,
    ClientAsk,
    DecisionEntry,
    ProjectState,
    Risk,
    SprintFacts,
    SprintFile,
    WhereItStands,
    children_of,
    descendants_of,
    display_name,
    effective_label,
    tree_depth,
)
from tests.golden_utils import GOLDEN_DIR
from tests.test_sync import FakeBackend, make_config, make_state

_NOW = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)
_TODAY = date(2026, 9, 24)


def _ws(
    code: str,
    *,
    name: str | None = None,
    label: str | None = None,
    parent_code: str | None = None,
    has_agreement: bool = False,
    company_kind: str = "client",
    company_code: str = "GGL",
    company_name: str = "Google",
    budget: float | None = None,
    status: str = "Open",
    summary: str | None = None,
    mc2_id: str | None = None,
) -> ProjectState:
    return replace(
        make_state(
            code=code,
            name=name,
            has_agreement=has_agreement,
            company_kind=company_kind,
            company_code=company_code,
            company_name=company_name,
            status=status,
            summary=summary,
        ),
        label=label,
        parent_code=parent_code,
        budget=budget,
        mc2_id=mc2_id or f"uuid-{code}",
    )


ACCOUNT = _ws("ggl-5216-google", name="Google", label="account", budget=300000.0)
PROGRAM = _ws(
    "ggl-5300-go-safety", name="Go Safety", label="program", parent_code=ACCOUNT.code,
    has_agreement=True, budget=150000.0,
)
JOB_IN_PROGRAM = _ws(
    "ggl-5136-go-safety-website", name="go/safety website", label="job",
    parent_code=PROGRAM.code, has_agreement=True, budget=90000.0,
    summary="Launch slipped to W22.",
)
JOB_NO_BUDGET = _ws(
    "ggl-5188-calendar", name="Calendar", label="job", parent_code=PROGRAM.code,
    has_agreement=True,
)
JOB_UNDER_ACCOUNT = _ws(
    "ggl-5168-activation", name="Activation", label="job", parent_code=ACCOUNT.code,
    has_agreement=True, budget=80000.0, summary="Storyboards in flight.",
)
INITIATIVE = _ws(
    "1pi-9005-mission-control", name="Mission Control", label="initiative",
    company_kind="self-fpsf", company_code="1PI", company_name="First Person",
)
ROSTER = (ACCOUNT, PROGRAM, JOB_IN_PROGRAM, JOB_NO_BUDGET, JOB_UNDER_ACCOUNT, INITIATIVE)
BY_CODE = {p.code: p for p in ROSTER}


# ──────────────────────────────────────────────────────────────────────
#  state helpers: display_name, children, depth
# ──────────────────────────────────────────────────────────────────────


def test_display_name_is_name_alone_except_for_jobs() -> None:
    assert display_name(ACCOUNT, BY_CODE) == "Google"
    assert display_name(PROGRAM, BY_CODE) == "Go Safety"
    assert display_name(INITIATIVE, BY_CODE) == "Mission Control"
    assert display_name(JOB_UNDER_ACCOUNT, BY_CODE) == "ggl-5168 Activation"
    # Without a roster the label falls back on shape; a job still reads as one.
    assert display_name(JOB_UNDER_ACCOUNT) == "ggl-5168 Activation"


def test_effective_label_fallback_reads_the_agreement_before_the_account_rule() -> None:
    # A parentless client row WITH an agreement is a job (its parent is
    # simply not in hand), never an account node.
    loose_job = replace(make_state(code="ggl-5168"), label=None)
    assert loose_job.parent_code is None and loose_job.has_agreement
    assert effective_label(loose_job, {}) == "job"
    # Without an agreement and without a parent it IS the account shape.
    assert effective_label(replace(loose_job, has_agreement=False), {}) == "account"
    # Children make a program whatever else is true.
    prog = replace(loose_job, code="ggl-5300")
    kid = replace(loose_job, code="ggl-5301", parent_code="ggl-5300")
    assert effective_label(prog, {prog.code: prog, kid.code: kid}) == "program"


def test_children_descendants_and_depth() -> None:
    assert [c.code for c in children_of(ACCOUNT.code, BY_CODE)] == [
        "ggl-5168-activation", "ggl-5300-go-safety",
    ]
    assert [d.code for d in descendants_of(ACCOUNT.code, BY_CODE)] == [
        "ggl-5168-activation",
        "ggl-5300-go-safety",
        "ggl-5136-go-safety-website",
        "ggl-5188-calendar",
    ]
    assert tree_depth(ACCOUNT, BY_CODE) == 0
    assert tree_depth(PROGRAM, BY_CODE) == 1
    assert tree_depth(JOB_IN_PROGRAM, BY_CODE) == 2
    # A parent cycle terminates.
    a = _ws("ggl-1", parent_code="ggl-2")
    b = _ws("ggl-2", parent_code="ggl-1")
    assert tree_depth(a, {a.code: a, b.code: b}) >= 1
    assert descendants_of("ggl-1", {a.code: a, b.code: b}) == (b,)


# ──────────────────────────────────────────────────────────────────────
#  region ensure / retire / migrate (pure)
# ──────────────────────────────────────────────────────────────────────

_RENDERED = (
    "# Title\n\n"
    "<!-- cp-engine:start alpha -->\nA\n<!-- cp-engine:end alpha -->\n\n"
    "<!-- cp-engine:start beta -->\nB\n<!-- cp-engine:end beta -->\n\n"
    "<!-- cp-engine:start gamma -->\nG\n<!-- cp-engine:end gamma -->\n\n"
    "## Hand\n\ntext\n"
)
_ORDER = ("alpha", "beta", "gamma")


def test_region_block_returns_markers_and_body() -> None:
    assert region_block(_RENDERED, "beta") == (
        "<!-- cp-engine:start beta -->\nB\n<!-- cp-engine:end beta -->"
    )
    with pytest.raises(ValueError):
        region_block(_RENDERED, "delta")


def test_ensure_regions_inserts_missing_region_after_its_predecessor() -> None:
    body = (
        "# Title\n\n"
        "<!-- cp-engine:start alpha -->\nold A\n<!-- cp-engine:end alpha -->\n\n"
        "<!-- cp-engine:start gamma -->\nold G\n<!-- cp-engine:end gamma -->\n\n"
        "## Hand\n\ntext\n"
    )
    out = ensure_regions(body, _RENDERED, _ORDER)
    assert out == (
        "# Title\n\n"
        "<!-- cp-engine:start alpha -->\nold A\n<!-- cp-engine:end alpha -->\n\n"
        "<!-- cp-engine:start beta -->\nB\n<!-- cp-engine:end beta -->\n\n"
        "<!-- cp-engine:start gamma -->\nold G\n<!-- cp-engine:end gamma -->\n\n"
        "## Hand\n\ntext\n"
    )
    # Idempotent.
    assert ensure_regions(out, _RENDERED, _ORDER) == out


def test_ensure_regions_inserts_before_successor_when_nothing_precedes() -> None:
    body = (
        "# Title\n\n"
        "<!-- cp-engine:start gamma -->\nold G\n<!-- cp-engine:end gamma -->\n\n"
        "## Hand\n"
    )
    out = ensure_regions(body, _RENDERED, _ORDER)
    assert out.index("start alpha") < out.index("start beta") < out.index("start gamma")
    assert out.endswith("## Hand\n")


def test_ensure_regions_appends_when_no_region_exists() -> None:
    out = ensure_regions("# Title\n\nprose\n", _RENDERED, ("alpha",))
    assert out == "# Title\n\nprose\n\n<!-- cp-engine:start alpha -->\nA\n<!-- cp-engine:end alpha -->\n"


def test_retire_regions_removes_block_and_one_blank_line() -> None:
    out = retire_regions(_RENDERED, ("beta",))
    assert out == (
        "# Title\n\n"
        "<!-- cp-engine:start alpha -->\nA\n<!-- cp-engine:end alpha -->\n\n"
        "<!-- cp-engine:start gamma -->\nG\n<!-- cp-engine:end gamma -->\n\n"
        "## Hand\n\ntext\n"
    )
    assert retire_regions(out, ("beta",)) == out
    # Retiring a region that sits last before hand text keeps the hand text.
    assert "## Hand\n\ntext\n" in retire_regions(_RENDERED, ("gamma",))


def test_migrate_regions_then_splice_round_trips_hand_text() -> None:
    body = (
        "# Title\n\n"
        "<!-- cp-engine:start legacy -->\nL\n<!-- cp-engine:end legacy -->\n\n"
        "<!-- cp-engine:start alpha -->\nold A\n<!-- cp-engine:end alpha -->\n\n"
        "## Hand\n\nsacred\n"
    )
    out = migrate_regions(body, _RENDERED, _ORDER, ("legacy",))
    assert not has_region(out, "legacy")
    for r in _ORDER:
        assert has_region(out, r)
    out = splice_managed_region(out, "alpha", "A")
    assert out == (
        "# Title\n\n"
        "<!-- cp-engine:start alpha -->\nA\n<!-- cp-engine:end alpha -->\n\n"
        "<!-- cp-engine:start beta -->\nB\n<!-- cp-engine:end beta -->\n\n"
        "<!-- cp-engine:start gamma -->\nG\n<!-- cp-engine:end gamma -->\n\n"
        "## Hand\n\nsacred\n"
    )


def test_project_cp_regions_is_derived_per_node() -> None:
    base = (
        "project-facts", "current-sprint", "tracked-issues", "inbound-strip",
        "recent-decisions-strip", "open-asks-strip", "stakeholders-strip",
        "exec-summary",
    )
    assert project_cp_regions(INITIATIVE, BY_CODE) == base
    assert project_cp_regions(ACCOUNT, BY_CODE) == ("project-facts", "children", *base[1:])
    assert project_cp_regions(PROGRAM, BY_CODE) == (
        "project-facts", "envelope-strip", "children", *base[1:]
    )
    assert project_cp_regions(JOB_IN_PROGRAM, BY_CODE) == (
        "project-facts", "envelope-strip", *base[1:]
    )
    assert PROJECT_CP_RETIRED_REGIONS == ("account-facts", "projects")


# ──────────────────────────────────────────────────────────────────────
#  region migration through sync (old account cp.md, old initiative cp.md)
# ──────────────────────────────────────────────────────────────────────

_OLD_ACCOUNT_CP = """\
---
Project: Google (account)
Provenance: Version 0.8.16.6 | 2026-05-22
Filename: 1p/google/cp.md
Author: cp-engine (initial scaffold)
---

# Google — Account CP

> Account-level CP for Google. Per-project status lives in each
> project's `cp.md`; this file holds the across-project view.

<!-- cp-engine:start account-facts -->
## Facts

| | |
|---|---|
| **Account** | Google |
| **Active projects** | 11 |
<!-- cp-engine:end account-facts -->

<!-- cp-engine:start projects -->
## Projects

### Active
| Code | Project | Stage | Owner | Last touched |
|---|---|---|---|---|
| [`ggl-5168-activation`](ggl-5168-activation/cp.md) | GGL 5168 Activation | Won | Brandon Grande | 2026-09-24 |
<!-- cp-engine:end projects -->

## Quick Resume

Google is eleven live jobs; EHS is the anchor relationship.

## Stakeholders

- Brandon Grande — the buyer of record.

## Account-wide decisions / context

_<durable truths about the relationship>_
"""

_OLD_INITIATIVE_CP = """\
---
Project: Mission Control
Provenance: Version 0.123.1 | 2026-09-24
Filename: cp.md
MC-id: uuid-1pi-9005-mission-control
Author: Claude
---

# Mission Control — Initiative CP

> _First Person's internal ops app._

<!-- cp-engine:start project-facts -->
## Facts

| | |
|---|---|
| **Code** | `1pi-9005-mission-control` |
| **Type** | Initiative |
<!-- cp-engine:end project-facts -->

<!-- cp-engine:start current-sprint -->
_No active sprint file._
<!-- cp-engine:end current-sprint -->

<!-- cp-engine:start recent-decisions-strip -->
## Recent decisions (auto-aggregated from sprint files, last 4 weeks)

- _No structured decisions captured in the last 4 weeks._
<!-- cp-engine:end recent-decisions-strip -->

<!-- cp-engine:start open-asks-strip -->
## Open client asks (auto-aggregated from sprint files)

- _No open asks._
<!-- cp-engine:end open-asks-strip -->

<!-- cp-engine:start exec-summary -->
## Exec Summary  ·  updated 2026-09-23

**Last session:** 2026-09-23
**Objective:** Keep MC-2 running.
**Status:** Tenant skills now reach hosted sessions.
<!-- cp-engine:end exec-summary -->

## Current Work

Real prose about the workstream.

## Team

- Drew, Tony.
"""


def _sync(root: Path, roster: tuple[ProjectState, ...], now: datetime = _NOW):
    return sync_tenant(
        make_config(root), backend_factory=lambda _: FakeBackend(roster), now=now
    )


def test_sync_migrates_an_old_account_cp_to_the_unified_region_set(tmp_path: Path) -> None:
    account_dir = tmp_path / "1p" / "google"
    account_dir.mkdir(parents=True)
    (account_dir / "cp.md").write_text(_OLD_ACCOUNT_CP)

    _sync(tmp_path, ROSTER)

    body = (account_dir / "cp.md").read_text()
    # Retired regions gone, unified set present, in template order.
    assert not has_region(body, "account-facts")
    assert not has_region(body, "projects")
    positions = [body.index(f"<!-- cp-engine:start {r} -->") for r in project_cp_regions(ACCOUNT, BY_CODE)]
    assert positions == sorted(positions)
    assert not has_region(body, "envelope-strip")
    # The engine regions carry live data, not the old table.
    assert "| **Type** | Account |" in body
    assert "| **Active projects** | 4 |" in body
    assert "[→](ggl-5300-go-safety/cp.md)" in body
    # The old projects table (with its owner cell) is gone; the name only
    # survives in the hand-written Stakeholders section BELOW the regions.
    engine_part = body[: body.index("<!-- cp-engine:end exec-summary -->")]
    assert "Brandon Grande" not in engine_part
    assert body.index("end exec-summary") < body.index("## Quick Resume")
    # Hand-written text is byte-for-byte where it was.
    assert "Google is eleven live jobs; EHS is the anchor relationship.\n" in body
    assert "## Stakeholders\n\n- Brandon Grande — the buyer of record.\n" in body
    assert body.startswith("---\nProject: Google (account)\n")
    assert "# Google — Account CP\n\n> Account-level CP for Google." in body
    # And the MC-id stamp lands so the uuid lookup can anchor on it.
    assert f"MC-id: {ACCOUNT.mc2_id}" in body
    # A second sync is a no-op on the file.
    before = (account_dir / "cp.md").read_bytes()
    _sync(tmp_path, ROSTER)
    assert (account_dir / "cp.md").read_bytes() == before


def test_sync_migrates_an_old_initiative_cp_and_keeps_its_exec_summary(tmp_path: Path) -> None:
    init_dir = tmp_path / "firstpersonsf" / "1pi-9005-mission-control"
    init_dir.mkdir(parents=True)
    (init_dir / "cp.md").write_text(_OLD_INITIATIVE_CP)

    _sync(tmp_path, ROSTER)

    body = (init_dir / "cp.md").read_text()
    for r in ("tracked-issues", "inbound-strip", "stakeholders-strip"):
        assert has_region(body, r), r
    order = [body.index(f"<!-- cp-engine:start {r} -->") for r in project_cp_regions(INITIATIVE, BY_CODE)]
    assert order == sorted(order)
    # tracked-issues went between current-sprint and the strips, where the
    # template puts it; the authored exec summary is untouched.
    assert body.index("start current-sprint") < body.index("start tracked-issues") < body.index("start inbound-strip")
    assert "**Status:** Tenant skills now reach hosted sessions.\n" in body
    assert "## Current Work\n\nReal prose about the workstream.\n" in body
    assert not has_region(body, "envelope-strip")
    assert not has_region(body, "children")


def test_sync_does_not_migrate_a_marker_less_cp_md(tmp_path: Path) -> None:
    """A hand-crafted cp.md with no engine marker is not brought to the
    unified set by the project pass (as before #303: it is not treated as
    a schema boundary). The sprint pass still seeds its `current-sprint`
    and strip markers at the end, exactly as it did before — the hand text
    stays first and untouched."""
    init_dir = tmp_path / "firstpersonsf" / "1pi-9005-mission-control"
    init_dir.mkdir(parents=True)
    (init_dir / "cp.md").write_text("# Hand-crafted\n\nNo engine markers here.\n")
    _sync(tmp_path, ROSTER)
    body = (init_dir / "cp.md").read_text()
    assert body.startswith("# Hand-crafted\n\nNo engine markers here.\n")
    assert not has_region(body, "project-facts")
    assert not has_region(body, "tracked-issues")
    assert not has_region(body, "exec-summary")


def test_sync_drops_children_region_when_the_children_leave(tmp_path: Path) -> None:
    _sync(tmp_path, ROSTER)
    program_cp = tmp_path / "1p/google/ggl-5300-go-safety/cp.md"
    assert has_region(program_cp.read_text(), "children")
    # The program's jobs are archived away → not a parent any more.
    without_kids = tuple(p for p in ROSTER if p.parent_code != PROGRAM.code)
    _sync(tmp_path, without_kids)
    body = program_cp.read_text()
    assert not has_region(body, "children")
    assert has_region(body, "envelope-strip")  # still has an agreement
    assert "| **Children (sum)** | — |" in body


# ──────────────────────────────────────────────────────────────────────
#  master-cp.md: old five active regions → active-tree
# ──────────────────────────────────────────────────────────────────────

_OLD_MASTER_REGIONS = (
    "active-1p", "active-fpsf", "active-fpsf-initiatives",
    "active-canonic", "active-canonic-initiatives",
)


def _old_master_cp_body() -> str:
    """The committed `master-cp-full` golden as it was before #303 — the
    five per-scope active tables — with a hand-written area added below
    the closed-recent block, as the live tenant's D8 areas will be."""
    golden = (GOLDEN_DIR / "render" / "master-cp-full.md").read_text()
    assert has_region(golden, "active-tree")
    pipeline_end = "<!-- cp-engine:end active-pipeline -->"
    tree_start = golden.index("<!-- cp-engine:start active-tree -->")
    tree_end = golden.index("<!-- cp-engine:end active-tree -->") + len("<!-- cp-engine:end active-tree -->")
    old_tables = "\n\n".join(
        f"<!-- cp-engine:start {r} -->\n## old {r} table\n\n| Code |\n|---|\n| `x-{r}` |\n<!-- cp-engine:end {r} -->"
        for r in _OLD_MASTER_REGIONS
    )
    body = golden[:tree_start] + old_tables + golden[tree_end:]
    assert pipeline_end in body
    return body + "\n## Quick Resume\n\nHand-written: where we are this week.\n"


def test_sync_retires_the_five_active_tables_and_inserts_active_tree(tmp_path: Path) -> None:
    master = tmp_path / "master-cp.md"
    master.write_text(_old_master_cp_body())

    _sync(tmp_path, ROSTER)

    body = master.read_text()
    for r in _OLD_MASTER_REGIONS:
        assert not has_region(body, r), r
        assert f"x-{r}" not in body
    assert has_region(body, "active-tree")
    # Inserted where active-1p was: right after the pipeline block.
    assert body.index("end active-pipeline") < body.index("start active-tree") < body.index("start last-week-workload")
    assert "### Google" in body and "| `ggl-5216-google` | Google | Account |" in body
    # The hand-written area below the engine regions is intact.
    assert body.endswith("## Quick Resume\n\nHand-written: where we are this week.\n")
    # Second sync: no further change.
    before = master.read_bytes()
    _sync(tmp_path, ROSTER)
    assert master.read_bytes() == before


def test_sync_dry_run_does_not_migrate_master(tmp_path: Path) -> None:
    master = tmp_path / "master-cp.md"
    master.write_text(_old_master_cp_body())
    sync_tenant(
        make_config(tmp_path), backend_factory=lambda _: FakeBackend(ROSTER),
        now=_NOW, dry_run=True,
    )
    assert master.read_text() == _old_master_cp_body()


# ──────────────────────────────────────────────────────────────────────
#  active-tree: ordering + indentation
# ──────────────────────────────────────────────────────────────────────


def test_tree_prefix_per_depth() -> None:
    assert _tree_prefix(0) == ""
    assert _tree_prefix(1) == "└─ "
    assert _tree_prefix(2) == "&nbsp;&nbsp;└─ "
    assert _tree_prefix(3) == "&nbsp;&nbsp;&nbsp;&nbsp;└─ "


def _tree_region(projects: tuple[ProjectState, ...]) -> str:
    out = render_master_cp(make_config(Path("/tmp/x")), projects, last_sync=_NOW, today=_TODAY)
    start = out.index("<!-- cp-engine:start active-tree -->")
    end = out.index("<!-- cp-engine:end active-tree -->")
    return out[start:end]


def test_tree_orders_clients_by_name_then_first_person_then_canonic() -> None:
    canonic = _ws("cnc-9004-storyos", label="initiative", company_kind="self-canonic",
                  company_code="CNC", company_name="Canonic")
    zeta = _ws("zet-5001-thing", label="job", has_agreement=True, parent_code="zet-5000-zeta",
               company_code="ZET", company_name="Zeta")
    alpha = _ws("alp-5001-thing", label="job", has_agreement=True, parent_code="alp-5000-alpha",
                company_code="ALP", company_name="Alpha")
    region = _tree_region((canonic, zeta, INITIATIVE, alpha, *ROSTER))
    headings = [line for line in region.splitlines() if line.startswith("### ")]
    assert headings == ["### Alpha", "### Google", "### Zeta", "### First Person", "### Canonic"]


def test_tree_rows_nest_under_the_account_with_indentation() -> None:
    region = _tree_region(ROSTER)
    codes = [
        line.split("|")[1].strip()
        for line in region.splitlines()
        if line.startswith("| ") and "`" in line.split("|")[1]
    ]
    assert codes == [
        "`ggl-5216-google`",
        "└─ `ggl-5168-activation`",
        "└─ `ggl-5300-go-safety`",
        "&nbsp;&nbsp;└─ `ggl-5136-go-safety-website`",
        "&nbsp;&nbsp;└─ `ggl-5188-calendar`",
        "`1pi-9005-mission-control`",
    ]
    assert "| Google | Account | drew | $300k |" in region
    assert "| Go Safety | Program |" in region
    assert "| ggl-5168 Activation | Job |" in region


def test_tree_keeps_deal_rows_in_the_pipeline_and_shows_the_label_word() -> None:
    deal = _ws("ggl-5210-new", label="job", parent_code=ACCOUNT.code, has_agreement=True,
               status="Deal")
    deal = replace(deal, deal_stage="Inquiry")
    out = render_master_cp(make_config(Path("/tmp/x")), (*ROSTER, deal), last_sync=_NOW, today=_TODAY)
    pipeline = out[out.index("start active-pipeline"):out.index("end active-pipeline")]
    tree = out[out.index("start active-tree"):out.index("end active-tree")]
    assert "ggl-5210-new" in pipeline
    assert "ggl-5210-new" not in tree
    assert "| Initiative |" in tree and "| Program |" in tree and "| Job |" in tree


def test_tree_inactive_account_still_heads_its_block_when_a_child_is_active() -> None:
    holding_account = replace(ACCOUNT, status="Holding")
    region = _tree_region((holding_account, JOB_UNDER_ACCOUNT))
    assert "| `ggl-5216-google` | Google | Account |" in region
    assert "| └─ `ggl-5168-activation` |" in region


def test_tree_omits_inactive_account_with_nothing_below_it() -> None:
    region = _tree_region((replace(ACCOUNT, status="Holding"), INITIATIVE))
    assert "ggl-5216-google" not in region
    assert "### Google" not in region


# ──────────────────────────────────────────────────────────────────────
#  envelope-strip states
# ──────────────────────────────────────────────────────────────────────


def test_envelope_unknown_flags_render_dash_not_none() -> None:
    view = _envelope_view(PROGRAM, BY_CODE, None)
    assert view["flag_line"] == "—"
    assert view["budget_short"] == "$150k"
    assert view["children_sum_short"] == "$90k across 1 of 2"
    assert view["children_without_budget_line"] == "1 child without budget"


def test_envelope_no_open_flags_is_none() -> None:
    assert _envelope_view(PROGRAM, BY_CODE, [])["flag_line"] == "none"


def test_envelope_flag_as_parent_names_the_children() -> None:
    flags = [
        {"project_id": JOB_IN_PROGRAM.mc2_id, "parent_id": PROGRAM.mc2_id,
         "kind": "over_envelope", "excess": "15000"},
        {"project_id": JOB_NO_BUDGET.mc2_id, "parent_id": PROGRAM.mc2_id,
         "kind": "over_envelope", "excess": "15000"},
    ]
    line = _envelope_view(PROGRAM, BY_CODE, flags)["flag_line"]
    assert line == "⚠️ over envelope by $15k: `ggl-5136-go-safety-website`, `ggl-5188-calendar`"


def test_envelope_flag_as_child_reports_the_parent_breach() -> None:
    flags = [
        {"project_id": JOB_IN_PROGRAM.mc2_id, "parent_id": PROGRAM.mc2_id,
         "kind": "over_envelope", "excess": 15000.0},
    ]
    view = _envelope_view(JOB_IN_PROGRAM, BY_CODE, flags)
    assert view["flag_line"] == "⚠️ over parent envelope by $15k"
    assert view["children_sum_short"] == "—"
    assert view["children_without_budget_line"] == "—"


def test_envelope_flag_both_ways_joins_the_two() -> None:
    flags = [
        {"project_id": JOB_IN_PROGRAM.mc2_id, "parent_id": PROGRAM.mc2_id,
         "kind": "over_envelope", "excess": 15000.0},
        {"project_id": PROGRAM.mc2_id, "parent_id": ACCOUNT.mc2_id,
         "kind": "over_envelope", "excess": 5000.0},
    ]
    line = _envelope_view(PROGRAM, BY_CODE, flags)["flag_line"]
    assert line == (
        "⚠️ over envelope by $15k: `ggl-5136-go-safety-website` · "
        "⚠️ over parent envelope by $5k"
    )


def test_envelope_strip_renders_only_with_an_agreement() -> None:
    cfg = make_config(Path("/tmp/x"))
    assert has_region(render_project_cp(cfg, PROGRAM, by_code=BY_CODE), "envelope-strip")
    assert has_region(render_project_cp(cfg, JOB_IN_PROGRAM, by_code=BY_CODE), "envelope-strip")
    assert not has_region(render_project_cp(cfg, ACCOUNT, by_code=BY_CODE), "envelope-strip")
    assert not has_region(render_project_cp(cfg, INITIATIVE, by_code=BY_CODE), "envelope-strip")


# ──────────────────────────────────────────────────────────────────────
#  subtree rollup
# ──────────────────────────────────────────────────────────────────────


def _sf(code: str, *, risks=(), asks=(), decisions=()) -> SprintFile:
    return SprintFile(
        project_code=code,
        week_iso="2026-W39",
        week_start="2026-09-21",
        week_end="2026-09-27",
        prior_sprint=None,
        facts=SprintFacts(None, None, None, None, None, 0, 0),
        where_it_stands=WhereItStands(None, None, None, (), ()),
        carry_forward=CarryForward(asks=(), risks=(), horizon=()),
        client_outbound=(),
        client_open_asks=tuple(
            ClientAsk(text=t, asked_date=d, status="open") for t, d in asks
        ),
        client_inbound=(),
        risks=tuple(Risk(text=t, severity="escalated", category="", raised_date="2026-09-01") for t in risks),
        allocation=(),
        deliverables=(),
        definition_of_done="",
        horizon=(),
        meeting_notes=None,
        decisions=tuple(DecisionEntry(text=t, date="2026-09-22", cross_cutting=True) for t in decisions),
    )


def test_subtree_rollup_scopes_to_the_root_and_its_descendants() -> None:
    files = (
        _sf(JOB_IN_PROGRAM.code, risks=("site risk",)),
        _sf(JOB_NO_BUDGET.code, asks=(("cal ask", "2026-09-01"),)),
        _sf(JOB_UNDER_ACCOUNT.code, risks=("activation risk",), decisions=("org-wide",)),
        _sf(INITIATIVE.code, risks=("mc risk",)),
        _sf(PROGRAM.code, risks=("program's own risk",)),
    )
    program = aggregate_subtree_strips(PROGRAM.code, files, (), _TODAY, BY_CODE)
    assert program.root_code == PROGRAM.code
    assert program.workstream_count == 3
    assert [r["text"] for r in program.carry_forward["escalated_risks"]] == [
        "site risk", "program's own risk",
    ]
    assert [a["project_code"] for a in program.carry_forward["stale_asks"]] == [JOB_NO_BUDGET.code]
    assert program.cross_cutting_decisions == ()

    account = aggregate_subtree_strips(ACCOUNT.code, files, (), _TODAY, BY_CODE)
    assert account.workstream_count == 4
    assert {r["text"] for r in account.carry_forward["escalated_risks"]} == {
        "site risk", "activation risk", "program's own risk",
    }
    assert [d["text"] for d in account.cross_cutting_decisions] == ["org-wide"]

    tenant = aggregate_subtree_strips(None, files, (), _TODAY, BY_CODE)
    assert tenant.root_code is None
    assert tenant.workstream_count == 5
    assert "mc risk" in {r["text"] for r in tenant.carry_forward["escalated_risks"]}
    # The tenant spelling is the None root.
    assert aggregate_tenant_strips(files, (), _TODAY) == tenant

    # A root with no roster entry rolls up only its own file.
    lone = aggregate_subtree_strips(INITIATIVE.code, files, (), _TODAY, {})
    assert lone.workstream_count == 1


def test_sync_writes_the_parent_rollup_from_the_children_files(tmp_path: Path) -> None:
    """End to end: the program's and the account's sprint files carry the
    children list and the subtree rollup read from the jobs' files, which
    sync scaffolded first in the same run; the parser reads the rollup
    back as nothing (no bracket prefix), so the tenant agenda counts the
    job's risk once."""
    _sync(tmp_path, ROSTER)
    week_dir = next((tmp_path / "sprints").iterdir())
    job_file = week_dir / f"{JOB_IN_PROGRAM.code}.md"
    body = job_file.read_text()
    body = body.replace(
        "## Dependencies & risks\n",
        "## Dependencies & risks\n\n- [escalated · legal · 2026-09-22] Contract stuck in legal\n",
        1,
    )
    job_file.write_text(body)

    _sync(tmp_path, ROSTER)

    program_sprint = (week_dir / f"{PROGRAM.code}.md").read_text()
    assert "### Workstreams" in program_sprint
    # Summaries come from each child's cp.md Exec Summary (sync re-derives
    # them from disk); nothing is authored here, so both read as empty.
    assert f"- **{JOB_IN_PROGRAM.code}** — _No summary yet._" in program_sprint
    assert f"- **{JOB_NO_BUDGET.code}** — _No summary yet._" in program_sprint
    assert "## Carried over — subtree rollup (2 workstreams)" in program_sprint
    assert f"- **`{JOB_IN_PROGRAM.code}`** risk · escalated — Contract stuck in legal" in program_sprint

    account_sprint = (week_dir / f"{ACCOUNT.code}.md").read_text()
    assert "subtree rollup (4 workstreams)" in account_sprint
    assert "Contract stuck in legal" in account_sprint
    assert f"- **{PROGRAM.code}** —" in account_sprint

    # The parent files parse with an EMPTY carry-forward: no double count.
    for code in (PROGRAM.code, ACCOUNT.code):
        parsed = parse_sprint_file(week_dir / f"{code}.md")
        assert parsed.carry_forward.risks == ()
        assert parsed.carry_forward.asks == ()
    master = (tmp_path / "master-cp.md").read_text()
    assert master.count("Contract stuck in legal") == 1

    # A leaf job's file has no children block and the prior-week form.
    assert "### Workstreams" not in job_file.read_text()
    assert "## Carried over from" in job_file.read_text()
