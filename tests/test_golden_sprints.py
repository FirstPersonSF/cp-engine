"""Golden-markdown tests for `cp_engine.sprints` (arch-phase-3, issue #26).

Locks the full sprint-file scaffolds (both engagement and initiative
shapes), the carry-forward rendering through `scaffold_from_prior`, the
dashboard current-sprint block, the per-week sprint index, and the
parse→dict round-trip shape.

Regenerate after an intentional template change:

    UPDATE_GOLDENS=1 uv run pytest tests/test_golden_sprints.py
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from cp_engine.sprints import (
    parse_sprint_file,
    render_current_sprint_block,
    render_sprint_index,
    render_sprint_scaffold,
    scaffold_from_prior,
    sprint_file_to_dict,
)
from cp_engine.state import (
    CarryForward,
    ClientAsk,
    HorizonItem,
    Issue,
    ProjectState,
    Risk,
    SprintCommit,
)
from tests.golden_utils import GOLDEN_DIR, assert_matches_golden

_TOUCHED = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


def _engagement() -> ProjectState:
    return ProjectState(
        code="peb-5100",
        name="Pebble Foods Activation",
        source="engagement",
        company_kind="client",
        company_code="PEB",
        company_name="Pebble Foods",
        status="Deal",
        is_internal=False,
        owner="Drew",
        last_touched=_TOUCHED,
        deadline=None,
        deal_stage="Negotiation",
        budget=45000.0,
        contacts=(
            {"name": "Maria Mraz", "role": "Director"},
            {"name": "Sam Ito", "role": "Legal"},
        ),
    )


def _initiative() -> ProjectState:
    return ProjectState(
        code="mission-control",
        name="Mission Control",
        source="initiative",
        company_kind="self-fpsf",
        company_code="1PI",
        company_name="First Person",
        status="Active",
        is_internal=True,
        owner="Tony",
        last_touched=_TOUCHED,
        deadline=None,
    )


def _carry_forward() -> CarryForward:
    return CarryForward(
        asks=(
            ClientAsk(
                text="Volume forecast from ops team",
                asked_date="2026-05-04",
                status="open",
                who="Maria",
            ),
        ),
        risks=(
            Risk(
                text="Legal turnaround may slip past May 22",
                severity="escalated",
                category="contract",
                raised_date="2026-05-04",
                why_it_matters="pushes contract into next sprint",
            ),
        ),
        horizon=(
            HorizonItem(
                text="Whether to staff a third on Pebble for Q3",
                bucket="decision",
                target_date="by W21",
            ),
        ),
    )


def _scaffold_kwargs() -> dict:
    return dict(
        week_iso="2026-W20",
        week_label="W20",
        week_start="2026-05-11",
        week_end="2026-05-17",
        prior_sprint="2026-W19",
        last_sprint_hours_line="Drew 6.5h · Tony 2h",
        sessions_this_week=3,
        last_session_date="2026-05-12",
        last_session_who="Drew",
        last_session_summary="Reconciled §4.2 redlines with Sam.",
        recent_commits=(
            SprintCommit(sha_short="ab12cd3", subject="pricing model v2",
                         author="Drew", when_short="05-12"),
            SprintCommit(sha_short="ef45ab6", subject="tier-3 ramp widened",
                         author="Tony", when_short="05-11"),
        ),
        open_issues=(
            Issue(number=42, title="Auth bug", status="Open", owner="drew",
                  updated=datetime(2026, 5, 6, tzinfo=timezone.utc)),
        ),
        carry_forward=_carry_forward(),
        meetings_this_sprint=2,
    )


# ──────────────────────────────────────────────────────────────────────
#  scaffold goldens — both shapes
# ──────────────────────────────────────────────────────────────────────


def test_golden_sprint_scaffold_engagement(golden_clock) -> None:
    out = render_sprint_scaffold(project=_engagement(), **_scaffold_kwargs())
    assert "## Client communication" in out  # shape sanity before goldening
    assert_matches_golden("sprints/scaffold-engagement.md", out)


def test_golden_sprint_scaffold_initiative(golden_clock) -> None:
    out = render_sprint_scaffold(project=_initiative(), **_scaffold_kwargs())
    assert "## Team communication" in out
    assert "## Client communication" not in out
    assert_matches_golden("sprints/scaffold-initiative.md", out)


def test_golden_sprint_scaffold_minimal(golden_clock) -> None:
    """First-ever sprint: no prior week, no sessions, no commits, no
    issues, empty carry-forward — every optional block suppressed."""
    out = render_sprint_scaffold(
        project=_engagement(),
        week_iso="2026-W20",
        week_label="W20",
        week_start="2026-05-11",
        week_end="2026-05-17",
        prior_sprint=None,
        last_sprint_hours_line=None,
        sessions_this_week=0,
        last_session_date=None,
        last_session_who=None,
        last_session_summary=None,
        recent_commits=(),
        open_issues=(),
        carry_forward=CarryForward(asks=(), risks=(), horizon=()),
        meetings_this_sprint=0,
    )
    assert_matches_golden("sprints/scaffold-engagement-minimal.md", out)


# ──────────────────────────────────────────────────────────────────────
#  scaffold_from_prior — carry-forward from a realistic prior week
# ──────────────────────────────────────────────────────────────────────

# The prior-week file is a committed INPUT fixture (not regenerated by
# UPDATE_GOLDENS) so the carry-forward parse side stays pinned too.
_PRIOR_INPUT = GOLDEN_DIR / "inputs" / "prior-week-peb-5100.md"


def test_golden_scaffold_from_prior(golden_clock, tmp_path: Path) -> None:
    sprints_root = tmp_path / "sprints"
    prior = sprints_root / "2026-W19" / "peb-5100.md"
    prior.parent.mkdir(parents=True)
    shutil.copy(_PRIOR_INPUT, prior)

    result = scaffold_from_prior(
        tenant_root=tmp_path,
        project_code="peb-5100",
        target_week_iso="2026-W20",
    )
    assert result is not None
    assert_matches_golden("sprints/scaffold-from-prior.md", result.read_text())

    # And the output must round-trip: carry-forward reflects the prior file.
    sf = parse_sprint_file(result)
    assert sf.prior_sprint == "2026-W19"
    assert len(sf.carry_forward.asks) == 1
    assert sf.carry_forward.asks[0].who == "Maria"


# ──────────────────────────────────────────────────────────────────────
#  current-sprint block + sprint index
# ──────────────────────────────────────────────────────────────────────


def _parsed_scaffold(golden_name: str, tmp_path: Path):
    """Parse a committed scaffold golden back into a SprintFile."""
    f = tmp_path / "sprint.md"
    f.write_text((GOLDEN_DIR / golden_name).read_text())
    return parse_sprint_file(f)


def test_golden_current_sprint_block(golden_clock, tmp_path: Path) -> None:
    sf = _parsed_scaffold("sprints/scaffold-engagement.md", tmp_path)
    out = render_current_sprint_block(sf, link_path="../../sprints/2026-W20/peb-5100.md")
    assert_matches_golden("sprints/current-sprint-block.md", out)


def test_golden_sprint_index(golden_clock, tmp_path: Path) -> None:
    sf_peb = _parsed_scaffold("sprints/scaffold-engagement.md", tmp_path)
    sf_mc = _parsed_scaffold("sprints/scaffold-initiative.md", tmp_path)
    out = render_sprint_index(
        week_iso="2026-W20",
        week_dates="May 11 – May 17",
        sprint_files=[sf_peb, sf_mc],
    )
    assert_matches_golden("sprints/sprint-index.md", out)


# ──────────────────────────────────────────────────────────────────────
#  parse → dict round-trip (locks the parsed shape, not just the render)
# ──────────────────────────────────────────────────────────────────────


def test_golden_roundtrip_parsed_scaffold_json(golden_clock, tmp_path: Path) -> None:
    """Parsing the engagement scaffold golden must yield a stable dict.
    Catches parser drift (a renderer change that silently stops
    round-tripping) that the .md goldens alone can't see."""
    sf = _parsed_scaffold("sprints/scaffold-engagement.md", tmp_path)
    out = json.dumps(sprint_file_to_dict(sf), indent=2, sort_keys=True, default=str)
    assert_matches_golden("sprints/roundtrip-engagement.json", out + "\n")
