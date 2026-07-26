# tests/test_cli_brief.py — `cp brief <code>`, the composed Mode-2 context pack
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from cp_engine.cli import main
from cp_engine.render import EXEC_SUMMARY_END, EXEC_SUMMARY_START


def _cp_md(where_bullets: int = 7) -> str:
    where = "\n".join(
        f"- Where bullet {i} with a few words." for i in range(1, where_bullets + 1)
    )
    return (
        "# Test project\n\n"
        "<!-- cp-engine:start project-facts -->\n"
        "## Facts\n\n"
        "| | |\n|---|---|\n"
        "| **Code** | `ibx-5153-ai-campaign` |\n"
        "| **Status** | Open |\n"
        "| **Budget** | $38k |\n"
        "<!-- cp-engine:end project-facts -->\n\n"
        f"{EXEC_SUMMARY_START}\n"
        "## Exec Summary  ·  updated 2026-07-25\n\n"
        "**Last session:** _2026-07-23 14:00 (Drew) — built the deck_\n"
        "**Objective:** Ship the campaign.\n"
        "**Status:** Deck built; awaiting the pillar ruling.\n"
        "**Where it stands:**\n"
        f"{where}\n"
        "**Next up:**\n"
        "- Hand the deck to Geoff.\n"
        "- Confirm the metrics.\n"
        "**Blockers:**\n"
        "- Pillar-name ruling.\n"
        "**Updates:**\n"
        "- 2026-07-23 — built two decks.\n"
        "- 2026-07-21 — swept the punchlist.\n"
        f"{EXEC_SUMMARY_END}\n"
    )


def _tenant(tmp_path: Path, *, sessions: bool = True) -> Path:
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.18"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n',
        encoding="utf-8",
    )
    proj = tmp_path / "1p" / "infoblox" / "ibx-5153-ai-campaign"
    proj.mkdir(parents=True)
    (proj / "cp.md").write_text(_cp_md(), encoding="utf-8")
    if sessions:
        sess = proj / "sessions"
        sess.mkdir()
        (sess / "2026-07-20-0900-drew.md").write_text("## Session\n", encoding="utf-8")
        (sess / "2026-07-23-1400-drew.md").write_text("## Session\n", encoding="utf-8")
    # A standalone-repo working dir: no spine, no commitments store.
    repo = tmp_path / "firstpersonsf" / "cp-engine"
    repo.mkdir(parents=True)
    (repo / "cp.md").write_text(
        "# cp-engine\n\n"
        "<!-- cp-engine:start project-facts -->\n"
        "## Facts\n\n| **Code** | `cp-engine` |\n"
        "<!-- cp-engine:end project-facts -->\n",
        encoding="utf-8",
    )
    return proj


# ── fake MC-2 ─────────────────────────────────────────────────────────


class _FakeQuery:
    def __init__(self, data):
        self._data = data

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def execute(self):
        return SimpleNamespace(data=self._data)


class _FakeClient:
    """Just enough Supabase: table-name → canned rows."""

    def __init__(self, rows_by_table):
        self._rows = rows_by_table

    def table(self, name):
        return _FakeQuery(self._rows.get(name, []))


_SPINE_ROWS = [
    {
        "est_item_id": "_authored/inputs-briefing",
        "framing": "Inputs & Briefing",
        "body": "### Objective\nRestructure the pitch.\n\n### Key dates\n7/31 deck.",
        "status": "live",
        "archived": False,
        "scope": "project",
        "project_id": "proj-1",
        "version_label": "v2",
        "version_date": "2026-07-25",
    },
    {
        "est_item_id": "_authored/some-synthesis",
        "framing": "A synthesis card",
        "body": "not the brief",
        "status": "live",
        "archived": False,
        "scope": "project",
        "project_id": "proj-1",
        "version_label": "v1",
        "version_date": "2026-07-01",
    },
]

_COMMITMENT_ROWS = [
    {
        "id": "c-1",
        "description": "Send the v09 deck to Geoff",
        "owner_name": "Drew Fiero",
        "owner_email": "drew@firstperson.is",
        "due_date": "2026-07-31",
        "status": "open",
    },
    {
        "id": "c-2",
        "description": "Chase Mehul's pains list",
        "owner_name": None,
        "owner_email": None,
        "due_date": None,
        "status": "open",
    },
]


def _full_client() -> _FakeClient:
    return _FakeClient(
        {
            "spine_substance": _SPINE_ROWS,
            "projects": [{"id": "proj-1", "number": 5153}],
            "commitments": _COMMITMENT_ROWS,
        }
    )


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(
        "cp_engine.mc2_db.get_client", lambda config=None, **kw: client
    )


# ── the full pack ─────────────────────────────────────────────────────


def test_brief_emits_all_five_sections(tmp_path, monkeypatch):
    _tenant(tmp_path)
    _patch_client(monkeypatch, _full_client())
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["brief", "ibx-5153"])
    assert result.exit_code == 0, result.output
    out = result.output
    for heading in (
        "# Brief — ibx-5153",
        "## Facts",
        "## Exec Summary (trimmed)",
        "## Inputs & Briefing",
        "## Open commitments",
        "## Last session",
    ):
        assert heading in out
    # Facts region content, sans its own heading.
    assert "| **Budget** | $38k |" in out
    assert "\n## Facts\n\n## Facts" not in out
    # Verbatim fields.
    assert "**Status:** Deck built; awaiting the pillar ruling." in out
    assert "- Hand the deck to Geoff." in out
    assert "- Pillar-name ruling." in out
    # Brief element body from MC-2.
    assert "Restructure the pitch." in out
    # Commitments: description — owner, due date; unowned/undated degrade.
    assert "- Send the v09 deck to Geoff — Drew Fiero, due 2026-07-31" in out
    assert "- Chase Mehul's pains list — unassigned, due undated" in out
    # Last-session pointer: the cp.md line + the NEWEST capture.
    assert "**Last session:** _2026-07-23 14:00 (Drew) — built the deck_" in out
    assert "sessions/2026-07-23-1400-drew.md" in out
    assert "2026-07-20-0900-drew.md" not in out


def test_brief_trims_where_it_stands_and_drops_updates(tmp_path, monkeypatch):
    _tenant(tmp_path)
    _patch_client(monkeypatch, _full_client())
    monkeypatch.chdir(tmp_path)
    out = CliRunner().invoke(main, ["brief", "ibx-5153"]).output
    # 7 bullets in cp.md → 5 kept + a trim note.
    assert "- Where bullet 5" in out
    assert "- Where bullet 6" not in out
    assert "2 more Where-it-stands bullet(s) trimmed" in out
    # Updates history is dropped entirely.
    assert "built two decks" not in out
    assert "**Updates:**" not in out


def test_brief_is_deterministic(tmp_path, monkeypatch):
    _tenant(tmp_path)
    _patch_client(monkeypatch, _full_client())
    monkeypatch.chdir(tmp_path)
    first = CliRunner().invoke(main, ["brief", "ibx-5153"]).output
    second = CliRunner().invoke(main, ["brief", "ibx-5153"]).output
    assert first == second


# ── graceful degradation ──────────────────────────────────────────────


def test_brief_standalone_repo_degrades_per_section(tmp_path, monkeypatch):
    """cp-engine: working dir exists but no spine, no commitments store,
    no exec summary, no sessions — every section prints an absence note."""
    _tenant(tmp_path)
    _patch_client(monkeypatch, _FakeClient({}))  # MC-2 up, but empty
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["brief", "cp-engine"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "# Brief — cp-engine" in out
    assert "| **Code** | `cp-engine` |" in out
    assert "_cp.md has no exec-summary region._" in out
    assert "_No live spine for 'cp-engine'._" in out
    assert "owns no commitments store" in out
    assert "_No Last-session line and no sessions/ captures._" in out


def test_brief_offline_mc2_degrades(tmp_path, monkeypatch):
    _tenant(tmp_path)
    _patch_client(monkeypatch, None)  # get_client(required=False) → None
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["brief", "ibx-5153"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "MC-2 unreachable — Inputs & Briefing not read." in out
    assert "MC-2 unreachable — commitments not read." in out
    # Offline half still composes.
    assert "**Status:** Deck built; awaiting the pillar ruling." in out


def test_brief_open_commitments_empty_is_a_real_answer(tmp_path, monkeypatch):
    _tenant(tmp_path)
    client = _FakeClient(
        {
            "spine_substance": _SPINE_ROWS,
            "projects": [{"id": "proj-1", "number": 5153}],
            "commitments": [],
        }
    )
    _patch_client(monkeypatch, client)
    monkeypatch.chdir(tmp_path)
    out = CliRunner().invoke(main, ["brief", "ibx-5153"]).output
    assert "_No open commitments._" in out


def test_brief_unknown_code_exits_nonzero(tmp_path, monkeypatch):
    _tenant(tmp_path)
    _patch_client(monkeypatch, _FakeClient({}))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["brief", "nope-9999"])
    assert result.exit_code == 1
    assert "nothing to brief" in result.output


def test_fetch_briefing_body_falls_back_to_dir_slug_code():
    """spine_substance keys on the DIR-SLUG project_code — a short-form code
    finds nothing directly and must fall back to the working dir's name."""
    from cp_engine.brief import fetch_briefing_body

    class _FilteringQuery(_FakeQuery):
        def eq(self, column, value):
            if column == "project_code":
                self._data = [
                    r for r in self._data if r.get("project_code") == value
                ]
            return self

    class _FilteringClient:
        def table(self, name):
            rows = [dict(r, project_code="ibx-5153-ai-campaign")
                    for r in _SPINE_ROWS]
            return _FilteringQuery(rows if name == "spine_substance" else [])

    body, note = fetch_briefing_body(
        _FilteringClient(), "ibx-5153", alt_code="ibx-5153-ai-campaign"
    )
    assert note is None
    assert "Restructure the pitch." in body
    # Without the fallback the short form finds nothing.
    body, note = fetch_briefing_body(_FilteringClient(), "ibx-5153")
    assert body is None and "No live spine" in note


def test_brief_section_fetch_failure_degrades(tmp_path, monkeypatch):
    """A fetcher that raises degrades its section, never the pack."""
    _tenant(tmp_path)

    class _Boom:
        def table(self, name):
            raise RuntimeError("supabase down mid-call")

    _patch_client(monkeypatch, _Boom())
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["brief", "ibx-5153"])
    assert result.exit_code == 0, result.output
    assert "Inputs & Briefing read failed" in result.output
    assert "Commitments read failed" in result.output
    assert "## Facts" in result.output
