"""Workstream code resolution: hosted uses the engine's resolvers (arch plan step 1b).

The tenant protocol promises a short code (`ggl-5188`) resolves everywhere.
Hosted resolved it for its own echo (`level`) through a COPY of
`promote_uphill.level_for`, found working dirs through a COPY of the state
walkers, and then forwarded the caller's raw short code upstream — so
`capture_project_state(project_code='ggl-5188')` 404'd at the webhook with
"no working dir for code" while the same response's `level` named
`ggl-5188-calendar-maintenance` (#345).

Two halves:
  * PARITY — hosted `_level_for` / `find_project_dir` answer exactly what the
    engine answers over the same tree;
  * #345 — every upstream writer forwards the resolved FULL code.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cp_engine.promote_uphill import level_for as engine_level_for  # noqa: E402
from cp_engine.state import load_paths_index  # noqa: E402

FULL = "ggl-5188-calendar-maintenance"
SHORT = "ggl-5188"
ACCOUNT = "ggl-5216-google"


@pytest.fixture(scope="module")
def server():
    mp = pytest.MonkeyPatch()
    mp.setenv("SUPABASE_URL", "http://example.invalid")
    mp.setenv("SUPABASE_ANON_KEY", "x")
    mp.setenv("MC2_API_BASE", "http://mc2.invalid")
    import server as mod

    yield mod
    mp.undo()


def _write_tree(root: Path) -> Path:
    rows = {
        ACCOUNT: {"path": "1p/google", "parent": None, "label": "account"},
        FULL: {"path": f"1p/google/{FULL}", "parent": ACCOUNT, "label": "job"},
        # Two codes sharing a short-code head that is NOT a `<code>-` boundary.
        "ggl-5300-go-safety": {"path": "1p/google/ggl-5300-go-safety", "parent": ACCOUNT,
                               "label": "program"},
        "ggl-5301-site": {"path": "1p/google/ggl-5300-go-safety/ggl-5301-site",
                          "parent": "ggl-5300-go-safety", "label": "job"},
        "1pi-9005-mission-control": {"path": "firstpersonsf/1pi-9005-mission-control",
                                     "parent": None, "label": "initiative"},
    }
    for row in rows.values():
        d = root / row["path"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "cp.md").write_text("# cp\n", encoding="utf-8")
        row.update({"has_agreement": False, "mc2_id": None, "company": "GGL", "status": "Open"})
    (root / ".cp-engine").mkdir(exist_ok=True)
    (root / ".cp-engine" / "paths.json").write_text(
        json.dumps({"version": 1, "generated_at": "2026-09-30T00:00:00+00:00",
                    "workstreams": rows}),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def tree(server, monkeypatch, tmp_path):
    root = _write_tree(tmp_path)
    monkeypatch.setattr(server, "tree_available", lambda: (True, ""))
    monkeypatch.setattr(server, "tree_root", lambda: root)
    return root


# ── parity ────────────────────────────────────────────────────────────

_CODES = [FULL, SHORT, ACCOUNT, "ggl-5300", "ggl-530", "ggl-5301", "1pi-9005",
          "1pi-9005-mission-control", "ggl-9999", "", "GGL-5188"]


@pytest.mark.parametrize("code", _CODES)
def test_level_for_is_the_engine_level_for(server, tree, code):
    hosted = server._level_for(code)
    engine = engine_level_for(tree, code)
    for key in ("code", "label", "parent", "indexed"):
        assert hosted[key] == engine[key], (code, key, hosted, engine)
    assert ("warning" in hosted) is (not engine["indexed"])


@pytest.mark.parametrize("code", [FULL, SHORT, "GGL-5188", ACCOUNT, "ggl-5301",
                                  "1pi-9005", "ggl-9999", "ggl-530"])
def test_find_project_dir_matches_the_engine_index(server, tree, code):
    """Whatever the engine's level_for resolves, find_project_dir lands on the
    dir the engine's index names; what it cannot resolve, it does not find."""
    index = load_paths_index(tree)
    level = engine_level_for(tree, code.lower())
    got = server.find_project_dir(tree, code)
    if level["indexed"]:
        assert got == tree / index[level["code"]].path
    else:
        assert got is None


def test_find_project_dir_walks_when_the_index_is_stale(server, tree):
    """A dir moved after the index was written: the engine's name walk finds it."""
    moved = tree / "1p" / "google" / "ggl-5300-go-safety" / FULL
    (tree / "1p" / "google" / FULL).rename(moved)
    assert server.find_project_dir(tree, SHORT) == moved
    assert server.find_project_dir(tree, FULL) == moved


def test_slug_and_level_rule_are_the_engines(server):
    from cp_engine import promote_uphill, state

    assert server._slug_full_job_name is state.slug_full_job_name
    assert server._LEVEL_RULE == promote_uphill.LEVEL_RULE
    assert server._PATHS_INDEX_REL == state.PATHS_INDEX_REL
    assert server._SCOPE_DIRS == state.SCOPE_DIRS


def test_canonical_project_code_alerts_instead_of_printing(server, monkeypatch, capsys):
    """The engine's canonical_spine_code with the hosted alerting hook: a failed
    lookup reaches observability.capture, not stderr."""
    captured = []
    monkeypatch.setattr(server.observability, "capture",
                        lambda exc, area=None: captured.append(area))

    class Boom:
        def table(self, _name):
            raise RuntimeError("postgrest said no")

    assert server.canonical_project_code(Boom(), "pid", "ggl-5188") == "ggl-5188"
    assert captured == ["canonical_project_code", "canonical_project_code"]
    assert "[warn]" not in capsys.readouterr().err


# ── #345: upstream writers forward the FULL code ───────────────────────


@pytest.fixture
def upstream(server, monkeypatch):
    """Capture every httpx.post the upstream helpers make."""
    posts: list[dict] = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"ok": True, "changed": [], "commit": "abc"}

    def fake_post(url, headers=None, json=None, timeout=None):
        posts.append({"url": url, "json": json})
        return _Resp()

    monkeypatch.setattr(server.httpx, "post", fake_post)
    monkeypatch.setattr(server, "caller_jwt", lambda: "jwt")
    monkeypatch.setattr(server, "user_client", lambda: object())
    monkeypatch.setattr(server, "audit", lambda *a, **k: None)
    monkeypatch.setattr(server, "resolve_write_scope",
                        lambda c, code: {"id": "p", "kind": "project", "project_code": code})
    monkeypatch.setattr(server, "_stale_omitted_exec_fields", lambda *a, **k: None)
    monkeypatch.setattr(server, "_project_codes_for_lint", lambda *a: None, raising=False)
    return posts


def _sent_code(posts: list[dict], path: str) -> str:
    hits = [p for p in posts if p["url"].endswith(path)]
    assert hits, f"nothing was POSTed to {path}: {posts}"
    return hits[-1]["json"]["project_code"]


def test_capture_project_state_forwards_the_full_code(server, tree, upstream):
    """THE #345 CONTROL — the 2026-09-30 call, in shape."""
    server.capture_project_state(SHORT, status="In build", objective="Keep calendars",
                                 where_it_stands=["a"], next_up=["b"], blockers=["None"])
    assert _sent_code(upstream, "/api/project-state/capture") == FULL


def test_capture_session_forwards_the_full_code(server, tree, upstream):
    server.capture_session(SHORT, "A real session narrative, long enough to pass.")
    assert _sent_code(upstream, "/api/sessions/capture") == FULL


def test_rotate_word_count_forwards_the_full_code(server, tree, upstream):
    server.rotate_word_count(SHORT)
    assert _sent_code(upstream, "/api/word-count/rotate") == FULL


def test_promote_uphill_decision_forwards_the_full_code(server, tree, upstream):
    server.promote_uphill(SHORT, "decision", "cp:abcd1234", "account-level")
    assert _sent_code(upstream, "/api/promote-uphill") == FULL


def test_without_the_tree_the_db_canonical_code_is_forwarded(server, monkeypatch, upstream):
    """Tree unreachable: the DB-side canonical code from resolve_write_scope
    (slugified full_job_name — the dir's name) is still better than a short one."""
    monkeypatch.setattr(server, "tree_available", lambda: (False, "no TENANT_REPO"))
    monkeypatch.setattr(server, "resolve_write_scope",
                        lambda c, code: {"id": "p", "kind": "project", "project_code": FULL})
    server.rotate_word_count(SHORT)
    assert _sent_code(upstream, "/api/word-count/rotate") == FULL


def test_a_full_code_is_forwarded_unchanged(server, tree, upstream):
    server.rotate_word_count(FULL)
    assert _sent_code(upstream, "/api/word-count/rotate") == FULL
