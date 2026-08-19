"""Auto-ingest wrote to `sprints/<week>/<code>.md` using the plan's project
key verbatim. Plans name projects however the model wrote them — often the
SHORT CODE (`slt-5196`) rather than the directory SLUG the sprint file uses
(`slt-5196-brand-campaign-26`). The path did not exist, `scaffold_from_prior`
failed identically for the same reason, and the whole per-project plan was
dropped after the model had already computed it.

123 runs between 2026-05-14 and 2026-08-19 discarded 1,375 bullets this way.
The cleanest proof is a single 08-19 batch where `ggl-5197` failed while
`ggl-5197-go-readiness-2026` succeeded in the same webhook invocation.
"""

import tempfile
from pathlib import Path

import pytest

from cp_engine.sprints import resolve_sprint_code


@pytest.fixture
def sprints_root():
    root = Path(tempfile.mkdtemp()) / "sprints"
    for week in ("2026-W34", "2026-W35"):
        (root / week).mkdir(parents=True)
        for stem in (
            "slt-5196-brand-campaign-26",
            "ggl-5197-go-readiness-2026",
            "mission-control",
        ):
            (root / week / f"{stem}.md").write_text("# scaffold\n")
    return root


def test_short_code_resolves_to_slug(sprints_root):
    """The incident: slt-5196 -> slt-5196-brand-campaign-26."""
    assert (
        resolve_sprint_code(sprints_root, "slt-5196")
        == "slt-5196-brand-campaign-26"
    )


def test_exact_stem_wins_outright(sprints_root):
    """An initiative whose code IS the stem must not be prefix-matched away."""
    assert resolve_sprint_code(sprints_root, "mission-control") == "mission-control"


def test_full_slug_passes_through(sprints_root):
    """The spelling that always worked keeps working."""
    assert (
        resolve_sprint_code(sprints_root, "ggl-5197-go-readiness-2026")
        == "ggl-5197-go-readiness-2026"
    )


def test_ambiguous_prefix_is_left_alone(sprints_root):
    """Two candidates -> refuse to guess. Writing a meeting's decisions into
    the wrong project is worse than the drop this fixes; the caller's
    missing-file error still fires."""
    for week in ("2026-W34", "2026-W35"):
        (sprints_root / week / "ggl-5197-second-thing.md").write_text("# x\n")

    assert resolve_sprint_code(sprints_root, "ggl-5197") == "ggl-5197"


def test_unknown_code_passes_through(sprints_root):
    assert resolve_sprint_code(sprints_root, "zzz-9999") == "zzz-9999"


def test_missing_sprints_root_is_safe(tmp_path):
    assert resolve_sprint_code(tmp_path / "nope", "slt-5196") == "slt-5196"


def test_underscore_files_are_not_candidates(sprints_root):
    """_week.md / _planning.md must never be a resolution target."""
    (sprints_root / "2026-W35" / "_week.md").write_text("# week\n")
    assert resolve_sprint_code(sprints_root, "_week") == "_week"


# --- end-to-end: a short-code plan must land its bullets -------------------


def test_short_code_plan_writes_to_the_slug_file(tmp_path):
    """The whole bug, end to end: a plan keyed by short code used to write
    nothing and report an error. It must now find the slug file and land its
    content there."""
    from datetime import date

    from cp_engine.ingest import execute_plan

    tenant = tmp_path
    week = tenant / "sprints" / "2026-W20"
    week.mkdir(parents=True)
    sprint = week / "slt-5196-brand-campaign-26.md"
    sprint.write_text(
        "# slt-5196-brand-campaign-26 — Sprint W20\n\n"
        "## Client communication\n\n"
        "### Inbound\n"
        "<!-- <what they told us — `[date · who]` prefix> -->\n\n"
        "### Slack digest\n",
        encoding="utf-8",
    )

    plan = {
        "projects": {
            # short code — the spelling that silently dropped everything
            "slt-5196": {
                "inbound": [
                    {
                        "text": "Both Q4 estimates approved and billed",
                        "date": "2026-05-12",
                        "who": "Janet",
                    }
                ]
            }
        }
    }

    result = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))

    assert result.errors == []
    body = sprint.read_text(encoding="utf-8")
    assert "Both Q4 estimates approved and billed" in body
    assert "cp:hash=" in body


def test_unresolvable_code_still_reports_the_error(tmp_path):
    """The fix must not swallow genuine misses — and the message should show
    both spellings so the next reader sees the shape immediately."""
    from datetime import date

    from cp_engine.ingest import execute_plan

    tenant = tmp_path
    (tenant / "sprints" / "2026-W20").mkdir(parents=True)

    plan = {"projects": {"zzz-9999": {"inbound": [
        {"text": "x", "date": "2026-05-12", "who": "Someone"}]}}}

    result = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))

    assert len(result.errors) == 1
    assert "sprint file missing for zzz-9999" in result.errors[0]
