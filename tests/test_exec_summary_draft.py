# tests/test_exec_summary_draft.py — machine-drafted Exec Summaries (#251)
"""The weekly draft writes the four state fields of a stale summary, from that
workstream's own material, only when every field passes the checks — and the
first human refresh takes the marker away.

Pinned here:

1. A faithful draft writes Status / Where it stands / Next up / Blockers,
   leaves Objective and Updates byte-identical, and stamps `· drafted by cp`.
2. A low-fidelity draft (wrong material), a draft with an invented date or
   name, and an over-budget draft each write NOTHING.
3. A human write through `merge_exec_summary_fields` (the capture_project_state
   path) clears the marker. CONTROL: fails on the pre-#251 merge, whose stamp
   regex rewrote only the date and left the suffix behind.
4. A drafted summary is never flagged as a partial refresh. CONTROL: fails
   without the freshness guard.
5. master-cp.md shows `🤖 _drafted_` on a drafted row only. CONTROL: fails
   before the template change.
6. Only this workstream's material reaches the model; derived regions and
   template hints do not.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from cp_engine import exec_summary_draft as esd
from cp_engine.exec_summary_freshness import partial_refresh
from cp_engine.exec_summary_merge import merge_exec_summary_fields
from cp_engine.render import EXEC_SUMMARY_END, EXEC_SUMMARY_START

TODAY = date(2026, 10, 5)  # a Monday
WEEK = "2026-W41"
CODE = "ggl-5188-calendar-maintenance"

OBJECTIVE = "Keep the Go Safety calendar widgets current."
UPDATES = "- 2026-07-14 — W29 planning deepened."


def cp_md(stamp: str = "2026-07-14", *, marker: str = "",
          status: str = "Widgets built; waiting on Tony's spec.",
          where: str = "Calendar widgets built and working.",
          next_up: str = "Drew: widget updates per Tony's spec by Fri 7/17.",
          blockers: str = "Tony's spec gates the build.") -> str:
    return (
        "# ggl-5188 Calendar maintenance\n\n"
        f"{EXEC_SUMMARY_START}\n"
        f"## Exec Summary  ·  updated {stamp}{marker}\n\n"
        "**Last session:** _<date>_\n"
        f"**Objective:** {OBJECTIVE}\n"
        f"**Status:** {status}\n\n"
        f"**Where it stands:**\n- {where}\n\n"
        f"**Next up:**\n- {next_up}\n\n"
        f"**Blockers:**\n- {blockers}\n\n"
        f"**Updates:**\n{UPDATES}\n"
        f"{EXEC_SUMMARY_END}\n\n"
        "## Decisions\n\n"
        "1. **Maintenance bucket absorbs one-off asks.** (2026-09-14)\n"
        "2. **Old May decision.** (2026-05-08)\n"
    )


SPRINT_W40 = """---
Project: ggl-5188
Sprint: 2026-W40
---

# ggl-5188 — Calendar maintenance · Sprint W40

← [Project CP](../../1p/google/x/cp.md)

<!-- cp-engine:start sprint-facts -->
| Stage | Won | DERIVED-FACTS-SENTINEL |
<!-- cp-engine:end sprint-facts -->

<!-- cp-engine:start carry-forward -->
## Carried over from 2026-W39
- [open · 2026-09-22 · Rina] Rina wants the latest pot balance before Friday. CARRIED-SENTINEL
- 13 stale asks (oldest 2026-05-15) — triage in [2026-W21](../2026-W21/x.md)
<!-- cp-engine:end carry-forward -->

## Client communication

### Outbound
<!-- <message — `[status · date]` prefix> TEMPLATE-HINT-SENTINEL -->

### Inbound
- [2026-09-29 · Tony (internal)] Tony and Brandon agreed to use up what is left in the 5188 maintenance pot on current Triptych asks: banners and reporting. Brandon believes the pot ends around July 2027 and will confirm the end date. <!-- cp:hash=6d54ff06 -->

## Meeting notes & decisions

### Decisions
- [decision · 2026-09-29] Burn down the 5188 maintenance budget first before opening new billing on Triptych banner and reporting work, per Fiona.
"""

FAITHFUL = {
    "status": "Maintenance only. The team will use up what is left in the 5188 "
              "maintenance pot on current Triptych asks, banners and reporting, "
              "before opening new billing.",
    "where_it_stands": [
        "2026-09-29: Tony and Brandon agreed to use up the 5188 maintenance pot on "
        "current Triptych asks: banners and reporting.",
        "Brandon believes the pot ends around July 2027.",
    ],
    "next_up": ["Brandon: confirm the pot end date.",
                "Rina wants the latest pot balance before Friday."],
    "blockers": [],
}

# Well-formed, plausible, and about a different engagement entirely.
FABRICATED = {
    "status": "Concept round three is with the client's creative director for "
              "sign-off, with the brand film storyboard and the paid social "
              "cutdowns scheduled next after legal clearance of the soundtrack.",
    "where_it_stands": [
        "Storyboard frames for the hero film were approved in principle by the "
        "creative director pending legal clearance of the licensed soundtrack.",
        "Paid social cutdowns are sequenced after the hero film edit locks.",
    ],
    "next_up": ["Secure soundtrack licensing and lock the hero film edit."],
    "blockers": ["Soundtrack licensing clearance from legal remains outstanding."],
}


def fake_llm(reply: dict, seen: list | None = None):
    def call(system: str, prompt: str) -> str:
        if seen is not None:
            seen.append(prompt)
        return json.dumps(reply)
    return call


def tenant(tmp_path: Path, cp_text: str | None = None) -> tuple[Path, Path, SimpleNamespace]:
    root = tmp_path / "t"
    work = root / "1p" / "google" / CODE
    work.mkdir(parents=True)
    (work / "cp.md").write_text(cp_text or cp_md(), encoding="utf-8")
    sprint = root / "sprints" / "2026-W40"
    sprint.mkdir(parents=True)
    (sprint / f"{CODE}.md").write_text(SPRINT_W40, encoding="utf-8")
    # A SIBLING workstream's sprint file in the same week: must never reach
    # the prompt.
    (sprint / "ggl-5197-go-readiness-2026.md").write_text(
        "## Client communication\n### Inbound\n- [2026-09-29 · Tony] SIBLING-SENTINEL "
        "5197 is a go.\n", encoding="utf-8")
    (root / "sprints" / WEEK).mkdir()
    meetings = work / "meetings"
    meetings.mkdir()
    (meetings / "2026-09-28-1p-weekly-scrum.md").write_text("x", encoding="utf-8")
    return root, work, SimpleNamespace(code=CODE, name="Calendar maintenance")


def run(root, work, project, llm, *, apply=True, commitments=()):
    return esd.draft_one(project, tenant_root=root, work_dir=work, reason="stale 83d",
                         today=TODAY, current_week=WEEK, llm=llm,
                         commitments=commitments, apply=apply)


# ── 1. a faithful draft ────────────────────────────────────────────────


def test_faithful_draft_writes_four_fields_and_the_marker(tmp_path):
    root, work, project = tenant(tmp_path)
    before = (work / "cp.md").read_text(encoding="utf-8")
    res = run(root, work, project, fake_llm(FAITHFUL))
    assert res.outcome == "written", res.detail
    after = (work / "cp.md").read_text(encoding="utf-8")

    assert "## Exec Summary  ·  updated 2026-10-05  ·  drafted by cp (from W40 sprint file, 1 meeting, 1 decision)" in after
    assert "**Status:** Maintenance only." in after
    assert "- Brandon believes the pot ends around July 2027." in after
    assert "**Blockers:**\n- None recorded." in after
    # Human-owned and log fields survive byte-for-byte.
    assert f"**Objective:** {OBJECTIVE}" in after
    assert UPDATES in after
    assert "**Last session:** _<date>_" in after
    # Nothing outside the region moved.
    assert after.split(EXEC_SUMMARY_END)[1] == before.split(EXEC_SUMMARY_END)[1]
    assert esd.is_drafted(after)


def test_dry_run_writes_nothing(tmp_path):
    root, work, project = tenant(tmp_path)
    before = (work / "cp.md").read_bytes()
    res = run(root, work, project, fake_llm(FAITHFUL), apply=False)
    assert res.outcome == "would-write"
    assert (work / "cp.md").read_bytes() == before


def test_a_returned_objective_is_never_written(tmp_path):
    root, work, project = tenant(tmp_path)
    reply = dict(FAITHFUL, objective="Something the model decided the job is for.")
    res = run(root, work, project, fake_llm(reply))
    assert res.outcome == "written"
    text = (work / "cp.md").read_text(encoding="utf-8")
    assert f"**Objective:** {OBJECTIVE}" in text
    assert "Something the model decided" not in text


# ── 2. what writes nothing ─────────────────────────────────────────────


def test_low_fidelity_draft_writes_nothing(tmp_path):
    root, work, project = tenant(tmp_path)
    before = (work / "cp.md").read_bytes()
    res = run(root, work, project, fake_llm(FABRICATED))
    assert res.outcome == "rejected"
    assert "phrases occur in the source" in res.detail
    assert any(not c.ok and c.score is not None and c.score < 0.05 for c in res.checks)
    assert (work / "cp.md").read_bytes() == before


def test_one_invented_date_writes_nothing(tmp_path):
    """Faithful phrasing, one wrong date: the phrase score passes it, the
    specifics check does not — and one failing field sinks the whole draft."""
    root, work, project = tenant(tmp_path)
    before = (work / "cp.md").read_bytes()
    reply = dict(FAITHFUL, next_up=["Brandon: confirm the pot end date by 2026-10-09."])
    res = run(root, work, project, fake_llm(reply))
    assert res.outcome == "rejected"
    assert "2026-10-09" in res.detail
    assert (work / "cp.md").read_bytes() == before


def test_one_invented_name_writes_nothing(tmp_path):
    root, work, project = tenant(tmp_path)
    before = (work / "cp.md").read_bytes()
    reply = dict(FAITHFUL, next_up=["Brandon and Gretchen: confirm the pot end date."])
    res = run(root, work, project, fake_llm(reply))
    assert res.outcome == "rejected"
    assert "Gretchen" in res.detail
    assert (work / "cp.md").read_bytes() == before


def test_over_budget_draft_writes_nothing(tmp_path):
    root, work, project = tenant(tmp_path)
    before = (work / "cp.md").read_bytes()
    bullet = FAITHFUL["where_it_stands"][0]
    reply = dict(FAITHFUL, where_it_stands=[bullet] * 6)
    res = run(root, work, project, fake_llm(reply))
    assert res.outcome == "rejected"
    assert "Where it stands: 6 bullets" in res.detail
    assert (work / "cp.md").read_bytes() == before


def test_no_material_writes_nothing_and_calls_no_model(tmp_path):
    root, work, project = tenant(tmp_path)
    (root / "sprints" / "2026-W40" / f"{CODE}.md").unlink()
    (work / "cp.md").write_text(cp_md().split("## Decisions")[0], encoding="utf-8")
    before = (work / "cp.md").read_bytes()
    calls: list = []
    res = run(root, work, project, fake_llm(FAITHFUL, calls))
    assert res.outcome == "skipped"
    assert calls == []
    assert (work / "cp.md").read_bytes() == before


def test_model_failure_writes_nothing(tmp_path):
    root, work, project = tenant(tmp_path)
    before = (work / "cp.md").read_bytes()
    res = run(root, work, project, lambda s, p: "not json at all")
    assert res.outcome == "error"
    assert (work / "cp.md").read_bytes() == before


# ── 3. the human refresh clears the marker ─────────────────────────────


def test_human_refresh_clears_the_drafted_marker():
    """CONTROL: the pre-#251 `_STAMP_RE` matched only up to the date, so a
    capture_project_state write advanced the date and KEPT `· drafted by cp`
    — a partner's summary would have read as machine prose."""
    drafted = cp_md("2026-10-05", marker="  ·  drafted by cp (from W40 sprint file)")
    assert esd.is_drafted(drafted)
    merged, changed = merge_exec_summary_fields(
        drafted, {"Status": "Partner-written status."}, today=date(2026, 10, 7))
    assert changed == ("Status",)
    assert "## Exec Summary  ·  updated 2026-10-07\n" in merged
    assert "drafted by cp" not in merged
    assert not esd.is_drafted(merged)


def test_human_updates_append_keeps_the_marker():
    """An Updates entry doesn't touch the four drafted fields, so they are
    still machine-written — the marker stays, the date advances (Drew,
    2026-09-30)."""
    from cp_engine.exec_summary_merge import append_update_entry

    drafted = cp_md("2026-10-05", marker="  ·  drafted by cp (from W40 sprint file)")
    text, changed = append_update_entry(drafted, "Checked with Rina.", today=date(2026, 10, 7))
    assert changed
    assert esd.is_drafted(text)
    assert "updated 2026-10-07" in text


def test_objective_only_human_merge_keeps_the_marker():
    """Objective is never drafted; changing only it leaves the four drafted
    fields machine-written."""
    drafted = cp_md("2026-10-05", marker="  ·  drafted by cp (from W40 sprint file)")
    merged, changed = merge_exec_summary_fields(
        drafted, {"Objective": "A sharper objective."}, today=date(2026, 10, 7))
    assert changed == ("Objective",)
    assert esd.is_drafted(merged)


def test_a_noop_human_merge_leaves_the_draft_marked():
    """Re-sending identical content changes nothing, so it must not claim
    the summary as human-authored either."""
    drafted = cp_md("2026-10-05", marker="  ·  drafted by cp (from W40 sprint file)")
    merged, changed = merge_exec_summary_fields(
        drafted, {"Status": "Widgets built; waiting on Tony's spec."}, today=date(2026, 10, 7))
    assert changed == ()
    assert esd.is_drafted(merged)


# ── 4. a draft is never a partial refresh ──────────────────────────────

pytest_git = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available",
)


def _git(repo: Path, *args: str, when: str | None = None) -> None:
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


@pytest_git
def test_drafted_summary_is_not_flagged_as_partial_refresh(tmp_path):
    """CONTROL: without the freshness guard, a draft whose body fields came
    out identical to July's keeps July's blame dates under an October stamp
    and is re-flagged `partial refresh` the moment it lands."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    f = repo / "cp.md"
    f.write_text(cp_md("2026-07-14"), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "july", when="2026-07-14T12:00:00-07:00")

    # Same body fields, new Status, drafted stamp.
    f.write_text(cp_md("2026-10-05", marker="  ·  drafted by cp (from W40 sprint file)",
                       status="Maintenance only."), encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "draft", when="2026-10-05T06:00:00-07:00")
    assert partial_refresh(f) is None

    # The same content WITHOUT the marker is a partial refresh — the detector
    # still works for human writes.
    f.write_text(cp_md("2026-10-05", status="Maintenance only."), encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "human", when="2026-10-05T07:00:00-07:00")
    assert partial_refresh(f) is not None


# ── 5. master-cp.md ────────────────────────────────────────────────────


@pytest_git
def test_master_cp_marks_a_drafted_row_only(tmp_path):
    """CONTROL: fails before the template change — the row rendered the
    drafted one-liner exactly like a partner's."""
    from cp_engine import sync_tenant
    from cp_engine.sync import find_working_dir
    from tests.test_sync import FakeBackend, make_config, make_state

    root = tmp_path / "t"
    root.mkdir()
    _git(root, "init", "-q")
    other = "tel-5113-2025-collateral"
    states = (make_state(code=CODE, name="Calendar maintenance"),
              make_state(code=other, name="Collateral", company_code="TEL",
                         company_name="Teleflex"))
    now = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)

    def sync() -> str:
        sync_tenant(make_config(root), backend_factory=lambda _: FakeBackend(states), now=now)
        return (root / "master-cp.md").read_text(encoding="utf-8")

    sync()
    drafted_dir = find_working_dir(root, CODE, None)
    human_dir = find_working_dir(root, other, None)
    (drafted_dir / "cp.md").write_text(
        cp_md("2026-10-05", marker="  ·  drafted by cp (from W40 sprint file)"), encoding="utf-8")
    (human_dir / "cp.md").write_text(cp_md("2026-10-05"), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "x", when="2026-10-05T06:00:00-07:00")

    master = sync()
    rows = {c: next(ln for ln in master.splitlines() if f"`{c}`" in ln) for c in (CODE, other)}
    assert "🤖 _drafted_" in rows[CODE]
    assert "🤖 _drafted_" not in rows[other]
    assert master.count("🤖 _drafted_") == 1


# ── 6. grounding ───────────────────────────────────────────────────────


def test_only_this_workstreams_hand_written_material_reaches_the_model(tmp_path):
    root, work, project = tenant(tmp_path)
    seen: list[str] = []
    run(root, work, project, fake_llm(FAITHFUL, seen), apply=False)
    prompt = seen[0]
    assert "burn down the 5188 maintenance budget" in prompt.lower()
    assert "CARRIED-SENTINEL" in prompt           # carry-forward is material
    assert "SIBLING-SENTINEL" not in prompt       # another workstream's file
    assert "DERIVED-FACTS-SENTINEL" not in prompt  # engine-derived region
    assert "TEMPLATE-HINT-SENTINEL" not in prompt  # template field hint
    assert "stale asks" not in prompt             # carry-forward hygiene rollup
    assert "cp:hash" not in prompt
    assert "Old May decision" not in prompt       # decision outside the window
    assert "Maintenance bucket absorbs one-off asks" in prompt


def test_commitments_are_material(tmp_path):
    root, work, project = tenant(tmp_path)
    seen: list[str] = []
    res = run(root, work, project, fake_llm(FAITHFUL, seen), apply=False, commitments=[{
        "description": "Send Rina the pot balance", "direction": "us_to_them",
        "due_date": "2026-10-02", "date_status": "agreed", "owner_name": "Brandon Grande"}])
    assert "[us→them · due 2026-10-02 (agreed) · Brandon Grande] Send Rina the pot balance" in seen[0]
    assert "1 commitment" in res.source_note


# ── scope ──────────────────────────────────────────────────────────────


def test_scope_reason(tmp_path):
    p = tmp_path / "cp.md"
    p.write_text(cp_md("2026-09-21"), encoding="utf-8")
    assert esd.scope_reason(p, today=TODAY) == "stale 14d"
    p.write_text(cp_md("2026-09-22"), encoding="utf-8")
    assert esd.scope_reason(p, today=TODAY) is None  # 13 days: current
    p.write_text("# no exec summary here\n", encoding="utf-8")
    assert esd.scope_reason(p, today=TODAY) is None


def test_unsupported_specifics_reads_possessives_and_sentence_openers():
    src = "Rina asked Tony for the 'GSRS' deck on 2026-09-29 (W40)."
    assert esd.unsupported_specifics("Send Rina's GSRS deck to Tony.", src) == []
    assert esd.unsupported_specifics("Awaiting Rina, due Sep 29 in W40.", src) == []
    assert esd.unsupported_specifics("Awaiting Rina, due Oct 3.", src) == ["Oct 3"]
    assert esd.unsupported_specifics("Rina and Marcus to meet.", src) == ["Marcus"]
    assert esd.unsupported_specifics("NDA review with Rina.", src) == ["NDA"]
