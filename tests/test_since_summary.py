"""#251: what arrived after the Exec Summary, and owner review of drafts.

The 2026-10-30 case: on `switch to ibx-5153` the summary was 4 days old
while two meetings and seven sources had arrived; rebuilding "what changed"
took git log. On 2026-11-10 the new block showed ibx-5153 had 2 meetings,
9 commitments and 4 decisions since its 11-05 summary.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from cp_engine.since_summary import render_since, since_summary, summary_stamp


class _Q:
    def __init__(self, rows):
        self._rows = list(rows)

    def select(self, _cols):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def gt(self, col, val):
        self._rows = [r for r in self._rows if str(r.get(col) or "") > val]
        return self

    def is_(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) is None]
        return self

    def overlaps(self, col, vals):
        self._rows = [r for r in self._rows if set(r.get(col) or []) & set(vals)]
        return self

    def limit(self, _n):
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})()


class _Client:
    def __init__(self, tables, broken=()):
        self.tables, self.broken = tables, set(broken)

    def table(self, name):
        if name in self.broken:
            raise RuntimeError(f"{name} read failed")
        return _Q(self.tables.get(name, []))


def _client(**over):
    tables = {
        "projects": [{"id": "pid", "code": "IBX-ai-campaign", "number": 5153,
                      "full_job_name": "IBX 5153 AI Campaign", "company_id": "c"}],
        "companies": [{"id": "c", "code": "IBX"}],
        "fathom_meetings": [
            {"id": "m-old", "project_id": "pid", "title": "Kickoff",
             "meeting_date": "2026-10-27T17:00:00+00:00"},
            {"id": "m-new", "project_id": "pid", "title": "Review",
             "meeting_date": "2026-11-06T17:00:00+00:00"},
            {"id": "m-tag", "project_id": None, "project_tags": ["ibx-5153-ai-campaign"],
             "title": "Tagged only", "meeting_date": "2026-11-10T17:00:00+00:00"},
        ],
        "rag_assets": [
            {"project_id": "pid", "status": "active", "title": "Deck v03",
             "source_provider": "dropbox", "created_at": "2026-11-07T10:00:00+00:00"},
            {"project_id": "pid", "status": "active", "title": "Review summary",
             "source_provider": "fathom", "created_at": "2026-11-06T18:00:00+00:00"},
            {"project_id": "pid", "status": "active", "title": "Old brief",
             "source_provider": "drive", "created_at": "2026-10-01T10:00:00+00:00"},
        ],
        "commitments": [
            {"project_id": "pid", "status": "open", "description": "Send recut",
             "created_at": "2026-11-06T12:00:00+00:00", "direction": "us_to_them"},
            {"project_id": "pid", "status": "done", "description": "Closed one",
             "created_at": "2026-11-06T12:00:00+00:00"},
        ],
    }
    tables.update(over)
    return _Client(tables)


def _tree(tmp_path: Path) -> Path:
    week = tmp_path / "sprints" / "2026-W46"
    week.mkdir(parents=True)
    (week / "ibx-5153-ai-campaign.md").write_text(
        "---\nProject: ibx-5153-ai-campaign — IBX 5153 AI Campaign\n"
        "Provenance: Version 0.135.0 | 2026-11-04\n"
        "Filename: sprints/2026-W46/ibx-5153-ai-campaign.md\n"
        "Author: cp-engine (scaffold)\nSprint: 2026-W46\nPriorSprint: 2026-W45\n---\n\n"
        "# ibx-5153 — W46\n\n## Meeting notes & decisions\n\n### Decisions\n\n"
        "- [decision · 2026-11-06] Proceed under the 'intelligence beneath' direction."
        " <!-- cp:hash=abcd1234 -->\n"
        "- [decision · 2026-11-01] Older than the summary.\n",
        encoding="utf-8",
    )
    return tmp_path


def test_summary_stamp_reads_the_heading():
    assert summary_stamp("## Exec Summary  ·  updated 2026-11-05\n\nx") == date(2026, 11, 5)
    assert summary_stamp("## Exec Summary  ·  updated 2026-11-05  ·  drafted by cp") == date(2026, 11, 5)
    assert summary_stamp("no stamp") is None


def test_lists_only_what_arrived_after_the_stamp(tmp_path):
    block = since_summary(_client(), "pid", since=date(2026, 11, 5),
                          tenant_root=_tree(tmp_path), dir_slug="ibx-5153-ai-campaign")
    assert [m["title"] for m in block["meetings"]["items"]] == ["Tagged only", "Review"]
    assert [s["title"] for s in block["sources"]["items"]] == ["Deck v03"]  # fathom excluded
    assert [c["text"] for c in block["commitments"]["items"]] == ["Send recut"]  # open only
    assert block["decisions"]["items"] == [
        {"date": "2026-11-06", "text": "Proceed under the 'intelligence beneath' direction."}]
    assert block["total"] == 5


def test_a_failed_read_is_said_not_shown_as_nothing():
    client = _client()
    client.broken = {"commitments"}
    block = since_summary(client, "pid", since=date(2026, 11, 5))
    assert "error" in block["commitments"]
    assert "could not read" in render_since(block)


def test_brief_adds_the_section_only_when_something_arrived(tmp_path):
    from cp_engine.brief import compose_brief

    args = ("ibx-5153", None, None, None, None, None, None)
    plain = compose_brief(*args)
    assert "Since the summary" not in plain
    assert compose_brief(*args, since={"since": "2026-11-05", "total": 0}) == plain
    block = since_summary(_client(), "pid", since=date(2026, 11, 5),
                          tenant_root=_tree(tmp_path), dir_slug="ibx-5153-ai-campaign")
    text = compose_brief(*args, since=block)
    assert "## Since the summary" in text
    assert text.index("## Since the summary") > text.index("## Exec Summary")
    assert "2026-11-06 — Review" in text and "cp:hash" not in text


def test_master_cp_lists_drafts_by_owner(tmp_path):
    """Drew, 2026-11-10: the workstream's owner reviews its machine draft."""
    import os
    import subprocess
    from datetime import datetime, timezone

    from cp_engine import sync_tenant
    from cp_engine.sync import find_working_dir
    from tests.test_exec_summary_draft import CODE, cp_md
    from tests.test_sync import FakeBackend, make_config, make_state

    root = tmp_path / "t"
    root.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e.com",
               GIT_AUTHOR_DATE="2026-10-05T06:00:00-07:00",
               GIT_COMMITTER_DATE="2026-10-05T06:00:00-07:00")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    other = "tel-5113-2025-collateral"
    states = (make_state(code=CODE, name="Calendar maintenance"),
              make_state(code=other, name="Collateral", company_code="TEL",
                         company_name="Teleflex"))
    now = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)

    def sync() -> str:
        sync_tenant(make_config(root), backend_factory=lambda _: FakeBackend(states), now=now)
        return (root / "master-cp.md").read_text(encoding="utf-8")

    first = sync()
    assert "Drafts to review" not in first
    (find_working_dir(root, CODE, None) / "cp.md").write_text(
        cp_md("2026-10-05", marker="  ·  drafted by cp (from W40 sprint file)"),
        encoding="utf-8")
    (find_working_dir(root, other, None) / "cp.md").write_text(
        cp_md("2026-10-05"), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=root, check=True, env=env)

    master = sync()
    assert "**Drafts to review** (1)" in master
    assert f"- drew: `{CODE}`" in master
    assert f"`{other}`" not in master.split("**Drafts to review**")[1].split("\n\n")[0]


def test_bundle_asks_the_owner_to_review_a_draft(tmp_path):
    from cp_engine import prep_planning
    from tests.test_exec_summary_freshness import (
        _JULY,
        _commit,
        _planning_inputs,
        _region,
        _tenant,
    )

    cp = _tenant(tmp_path)
    text = _region("2026-09-29", **_JULY).replace(
        "updated 2026-09-29", "updated 2026-09-29  ·  drafted by cp (from W40)")
    _commit(tmp_path, cp, text, "2026-09-29T12:00:00-07:00")
    config, state = _planning_inputs(tmp_path)
    block = prep_planning.build_project_block(
        state, config=config, supabase_client=None,
        today=date(2026, 9, 30), week_iso="2026-W40",
    )
    rendered = "\n".join(prep_planning._render_bundle_project_block(block))
    assert "DRAFTED by cp — drew to confirm or rewrite" in rendered
