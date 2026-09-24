"""The recursive tree layout + `.cp-engine/paths.json` index (cp-engine #302).

Plan §3.3, decisions D5 (the account node's dir is the existing
`1p/<company>/`) and D10 (sync writes a path index; every resolver reads it
first and walks second, skipping `inactive/`).
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cp_engine import sync_tenant
from cp_engine.state import (
    PATHS_INDEX_REL,
    ProjectState,
    dir_name_for,
    inactive_path_for,
    load_paths_index,
    parent_path_for,
    path_for,
)
from cp_engine.sync import _deactivate_stale_cps, _find_project_dir, find_working_dir
from tests.test_sync import FakeBackend, make_config, make_state

_NOW = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)


def _ws(
    code: str,
    *,
    label: str | None,
    parent_code: str | None = None,
    has_agreement: bool = False,
    company_kind: str = "client",
    company_code: str = "GGL",
    company_name: str = "Google",
    mc2_id: str | None = None,
    status: str = "Open",
) -> ProjectState:
    return replace(
        make_state(
            code=code,
            has_agreement=has_agreement,
            company_kind=company_kind,
            company_code=company_code,
            company_name=company_name,
            status=status,
        ),
        label=label,
        parent_code=parent_code,
        mc2_id=mc2_id or f"uuid-{code}",
    )


# One client tree: account → program → job, plus a job directly under the
# account; one internal workstream.
ACCOUNT = _ws("ggl-5216-google", label="account")
PROGRAM = _ws("ggl-5300-go-safety", label="program", parent_code=ACCOUNT.code)
JOB_IN_PROGRAM = _ws(
    "ggl-5136-go-safety-website", label="job", parent_code=PROGRAM.code, has_agreement=True
)
JOB_UNDER_ACCOUNT = _ws(
    "ggl-5168-activation", label="job", parent_code=ACCOUNT.code, has_agreement=True
)
INITIATIVE = _ws(
    "1pi-9005-mission-control",
    label="initiative",
    company_kind="self-fpsf",
    company_code="1PI",
    company_name="First Person",
)
ROSTER = (ACCOUNT, PROGRAM, JOB_IN_PROGRAM, JOB_UNDER_ACCOUNT, INITIATIVE)
BY_CODE = {p.code: p for p in ROSTER}


# ──────────────────────────────────────────────────────────────────────
#  path_for — every label shape
# ──────────────────────────────────────────────────────────────────────


def test_path_for_account_node_is_the_existing_account_dir() -> None:
    # D5: the account CP that already renders there becomes the node's cp.md.
    assert path_for(ACCOUNT, BY_CODE) == "1p/google"
    assert parent_path_for(ACCOUNT, BY_CODE) == "1p"
    assert dir_name_for(ACCOUNT, BY_CODE) == "google"


def test_path_for_job_under_account_is_todays_layout() -> None:
    assert path_for(JOB_UNDER_ACCOUNT, BY_CODE) == "1p/google/ggl-5168-activation"


def test_path_for_program_and_its_child_nest() -> None:
    assert path_for(PROGRAM, BY_CODE) == "1p/google/ggl-5300-go-safety"
    assert (
        path_for(JOB_IN_PROGRAM, BY_CODE)
        == "1p/google/ggl-5300-go-safety/ggl-5136-go-safety-website"
    )
    assert parent_path_for(JOB_IN_PROGRAM, BY_CODE) == "1p/google/ggl-5300-go-safety"


def test_path_for_job_with_held_back_parent_does_not_move() -> None:
    # The parent names an account node that is not in the roster: today's
    # `1p/<company>/<code>` layout, so nothing moves for the job.
    orphan = _ws("ibx-5153-ai-campaign", label="job", parent_code="ibx-5217-infoblox",
                 has_agreement=True, company_code="IBX", company_name="Infoblox")
    assert path_for(orphan, {}) == "1p/infoblox/ibx-5153-ai-campaign"
    assert path_for(orphan, BY_CODE) == "1p/infoblox/ibx-5153-ai-campaign"


def test_path_for_self_company_top_level() -> None:
    assert path_for(INITIATIVE, BY_CODE) == "firstpersonsf/1pi-9005-mission-control"
    canonic = _ws("cnc-9004-storyos", label="initiative", company_kind="self-canonic",
                  company_code="CNC", company_name="Canonic")
    assert path_for(canonic, {}) == "canonic/cnc-9004-storyos"


def test_path_for_self_company_program_child_nests() -> None:
    prog = _ws("1pi-9010-platform", label="program", company_kind="self-fpsf",
               company_code="1PI", company_name="First Person")
    child = _ws("1pi-9011-cp-engine", label="initiative", parent_code=prog.code,
                company_kind="self-fpsf", company_code="1PI", company_name="First Person")
    by = {prog.code: prog, child.code: child}
    assert path_for(child, by) == "firstpersonsf/1pi-9010-platform/1pi-9011-cp-engine"


def test_inactive_path_is_a_sibling_bin_of_the_live_dir() -> None:
    # An account's inactive jobs stay at 1p/google/inactive/<code>, as today.
    assert inactive_path_for(JOB_UNDER_ACCOUNT, BY_CODE) == "1p/google/inactive/ggl-5168-activation"
    assert (
        inactive_path_for(JOB_IN_PROGRAM, BY_CODE)
        == "1p/google/ggl-5300-go-safety/inactive/ggl-5136-go-safety-website"
    )
    assert inactive_path_for(ACCOUNT, BY_CODE) == "1p/inactive/google"
    assert inactive_path_for(INITIATIVE, BY_CODE) == "firstpersonsf/inactive/1pi-9005-mission-control"


def test_path_for_refuses_a_parent_cycle() -> None:
    a = _ws("ggl-1000-a", label="program", parent_code="ggl-1001-b")
    b = _ws("ggl-1001-b", label="program", parent_code="ggl-1000-a")
    with pytest.raises(ValueError, match="cycle"):
        path_for(a, {a.code: a, b.code: b})


# ──────────────────────────────────────────────────────────────────────
#  sync: the tree on disk, the index, the moves
# ──────────────────────────────────────────────────────────────────────


def _sync(root: Path, roster: tuple[ProjectState, ...], now: datetime = _NOW):
    return sync_tenant(
        make_config(root), backend_factory=lambda _: FakeBackend(roster), now=now
    )


def test_sync_lays_out_the_tree_and_folds_the_account_node(tmp_path: Path) -> None:
    result = _sync(tmp_path, ROSTER)

    # The account node's cp.md IS the account CP, stamped with its MC-id so
    # the uuid-first lookup anchors on it from now on.
    account_cp = tmp_path / "1p" / "google" / "cp.md"
    assert account_cp.is_file()
    body = account_cp.read_text()
    assert "Account CP" in body
    assert f"MC-id: {ACCOUNT.mc2_id}" in body
    # #303: the one template — the account CP carries the full region set
    # plus `children`; no agreement, so no `envelope-strip`.
    assert "<!-- cp-engine:start project-facts -->" in body
    assert "<!-- cp-engine:start current-sprint -->" in body
    assert "<!-- cp-engine:start children -->" in body
    assert "<!-- cp-engine:start envelope-strip -->" not in body
    assert "| **Type** | Account |" in body
    assert "[→](ggl-5300-go-safety/cp.md)" in body
    # The program (no agreement in this roster) carries `children` but no
    # envelope; its grandchild-depth job links through path_for.
    program_cp = (tmp_path / "1p/google/ggl-5300-go-safety/cp.md").read_text()
    assert "<!-- cp-engine:start children -->" in program_cp
    assert "<!-- cp-engine:start envelope-strip -->" not in program_cp
    assert "[→](ggl-5136-go-safety-website/cp.md)" in program_cp
    # The job below it has an agreement and no children: envelope, no
    # children region.
    job_cp = (tmp_path / "1p/google/ggl-5300-go-safety/ggl-5136-go-safety-website/cp.md").read_text()
    assert "<!-- cp-engine:start envelope-strip -->" in job_cp
    assert "<!-- cp-engine:start children -->" not in job_cp

    assert (tmp_path / "1p/google/ggl-5168-activation/cp.md").is_file()
    assert (tmp_path / "1p/google/ggl-5300-go-safety/cp.md").is_file()
    assert (tmp_path / "1p/google/ggl-5300-go-safety/ggl-5136-go-safety-website/cp.md").is_file()
    assert (tmp_path / "firstpersonsf/1pi-9005-mission-control/cp.md").is_file()
    # No stray `1p/ggl-5216-google/` engagement dir for the account node.
    assert not (tmp_path / "1p" / "ggl-5216-google").exists()

    # master-cp (#303): the account node heads its company's block in the
    # tree at depth 0; the jobs indent below it, to any depth.
    master = (tmp_path / "master-cp.md").read_text()
    assert "<!-- cp-engine:start active-tree -->" in master
    assert "### Google" in master
    assert "| `ggl-5216-google` | ggl-5216-google | Account |" in master  # name == code in this roster
    assert "| └─ `ggl-5168-activation` |" in master
    assert "| &nbsp;&nbsp;└─ `ggl-5136-go-safety-website` |" in master
    assert "1p/google/ggl-5300-go-safety/ggl-5136-go-safety-website/cp.md" in master

    # D8: the account node has a sprint file from the one sprint template;
    # as a parent its where-it-stands lists its children and its
    # carry-forward is the subtree rollup. Not in the per-week index.
    week_dir = next((tmp_path / "sprints").iterdir())
    account_sprint = (week_dir / "ggl-5216-google.md").read_text()
    assert "Project CP](../../1p/google/cp.md)" in account_sprint
    assert "### Workstreams" in account_sprint
    assert "- **ggl-5168-activation** —" in account_sprint
    assert "- **ggl-5300-go-safety** —" in account_sprint
    assert "subtree rollup (" in account_sprint
    assert "| Stage |" not in account_sprint  # no agreement → no Stage row
    assert "_none_" in account_sprint  # deliverable-cards without an agreement
    job_sprint = (week_dir / "ggl-5136-go-safety-website.md").read_text()
    assert "Project CP](../../1p/google/ggl-5300-go-safety/ggl-5136-go-safety-website/cp.md)" in job_sprint
    index_md = (week_dir / "README.md").read_text()
    assert "ggl-5216-google" not in index_md
    assert "ggl-5168-activation" in index_md
    assert not result.no_op


def test_master_cp_render_exposes_the_tree() -> None:
    from cp_engine.render import render_master_cp
    from cp_engine.render import _project_view  # noqa: F401 — sanity import

    # The `active_tree` view the template renders (#303): companies in
    # order, rows depth-first with their depth.
    import cp_engine.render as render_mod

    captured: dict = {}
    real_env = render_mod._env

    class _Tpl:
        def render(self, **kw):
            captured.update(kw)
            return ""

    class _Env:
        def get_template(self, name):
            return _Tpl()

    render_mod._env = lambda: _Env()
    try:
        render_master_cp(make_config(Path("/tmp/x")), ROSTER, last_sync=_NOW)
    finally:
        render_mod._env = real_env
    tree = captured["active_tree"]
    assert [c["name"] for c in tree["companies"]] == ["Google", "First Person"]
    google = tree["companies"][0]["rows"]
    assert [(r["code"], r["depth"]) for r in google] == [
        ("ggl-5216-google", 0),
        ("ggl-5168-activation", 1),
        ("ggl-5300-go-safety", 1),
        ("ggl-5136-go-safety-website", 2),
    ]
    assert google[0]["scope"] == "1p"
    assert google[0]["dir_slug"] == "google"
    assert google[0]["label_word"] == "Account"
    assert "ggl-5216-google" not in {v["code"] for v in captured["active_groups"]["pipeline"]}


def test_paths_index_is_written_and_byte_stable_across_syncs(tmp_path: Path) -> None:
    _sync(tmp_path, ROSTER, now=_NOW)
    index_path = tmp_path / PATHS_INDEX_REL
    first = index_path.read_bytes()
    doc = json.loads(first)
    assert doc["version"] == 1
    assert first.endswith(b"\n")
    ws = doc["workstreams"]
    assert list(ws) == sorted(ws)
    assert ws["ggl-5216-google"] == {
        "path": "1p/google", "parent": None, "has_agreement": False,
        "label": "account", "mc2_id": ACCOUNT.mc2_id, "company": "GGL", "status": "Open",
    }
    assert ws["ggl-5136-go-safety-website"]["path"] == (
        "1p/google/ggl-5300-go-safety/ggl-5136-go-safety-website"
    )
    assert ws["ggl-5136-go-safety-website"]["parent"] == "ggl-5300-go-safety"
    assert ws["1pi-9005-mission-control"]["path"] == "firstpersonsf/1pi-9005-mission-control"

    # Second sync, a later clock, nothing changed: identical bytes — the
    # generated_at stamp must not advance on its own.
    later = _NOW.replace(hour=15)
    result = _sync(tmp_path, ROSTER, now=later)
    assert index_path.read_bytes() == first
    assert index_path not in result.files_written

    # load_paths_index round-trips.
    loaded = load_paths_index(tmp_path)
    assert loaded["ggl-5300-go-safety"].path == "1p/google/ggl-5300-go-safety"
    assert loaded["ggl-5300-go-safety"].label == "program"


def test_paths_index_is_not_written_on_dry_run(tmp_path: Path) -> None:
    sync_tenant(
        make_config(tmp_path),
        backend_factory=lambda _: FakeBackend(ROSTER),
        now=_NOW,
        dry_run=True,
    )
    assert not (tmp_path / PATHS_INDEX_REL).exists()


def test_generated_gitignore_commits_the_index(tmp_path: Path) -> None:
    _sync(tmp_path, ROSTER)
    ignore = (tmp_path / ".gitignore").read_text()
    assert ".cp-engine/*" in ignore
    assert "!.cp-engine/paths.json" in ignore


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(root: Path) -> None:
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "t", cwd=root)


def test_program_insertion_moves_the_job_subtree_with_git_mv(tmp_path: Path) -> None:
    """A job that was directly under its account gains a program above it:
    the whole working dir moves under the program and git records a rename,
    so history follows. Its sprint files keep their name (the code did not
    change)."""
    _init_repo(tmp_path)
    flat_job = replace(JOB_IN_PROGRAM, parent_code=ACCOUNT.code)
    _sync(tmp_path, (ACCOUNT, flat_job))
    old_dir = tmp_path / "1p/google/ggl-5136-go-safety-website"
    (old_dir / "notes.md").write_text("hand-written\n")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-qm", "before", cwd=tmp_path)

    _sync(tmp_path, (ACCOUNT, PROGRAM, JOB_IN_PROGRAM))

    new_dir = tmp_path / "1p/google/ggl-5300-go-safety/ggl-5136-go-safety-website"
    assert not old_dir.exists()
    assert (new_dir / "cp.md").is_file()
    assert (new_dir / "notes.md").read_text() == "hand-written\n"
    # Staged as a rename by `git mv` — the index already knows the new path.
    staged = _git("diff", "--cached", "--name-status", "-M", cwd=tmp_path).stdout
    assert "R" in staged and "ggl-5300-go-safety/ggl-5136-go-safety-website/notes.md" in staged
    # And nothing landed in an inactive bin.
    assert not (tmp_path / "1p/google/inactive").exists()
    # The index describes the tree as it now is.
    assert load_paths_index(tmp_path)["ggl-5136-go-safety-website"].path == (
        "1p/google/ggl-5300-go-safety/ggl-5136-go-safety-website"
    )


def test_program_removal_moves_the_job_back_up(tmp_path: Path) -> None:
    _sync(tmp_path, (ACCOUNT, PROGRAM, JOB_IN_PROGRAM))
    nested = tmp_path / "1p/google/ggl-5300-go-safety/ggl-5136-go-safety-website"
    assert nested.is_dir()

    flat_job = replace(JOB_IN_PROGRAM, parent_code=ACCOUNT.code)
    result = _sync(tmp_path, (ACCOUNT, flat_job))

    assert (tmp_path / "1p/google/ggl-5136-go-safety-website/cp.md").is_file()
    assert not nested.exists()
    # The program itself dropped out of the roster → parked in the account's bin.
    assert (tmp_path / "1p/google/inactive/ggl-5300-go-safety/cp.md").is_file()
    assert tmp_path / "1p/google/inactive/ggl-5300-go-safety" in result.files_deactivated


def test_a_stale_job_under_a_program_is_swept_into_the_programs_bin(tmp_path: Path) -> None:
    _sync(tmp_path, (ACCOUNT, PROGRAM, JOB_IN_PROGRAM))
    result = _sync(tmp_path, (ACCOUNT, PROGRAM))
    parked = tmp_path / "1p/google/ggl-5300-go-safety/inactive/ggl-5136-go-safety-website"
    assert (parked / "cp.md").is_file()
    assert parked in result.files_deactivated
    # The program's own subdirs are not workstreams and were not swept.
    assert (tmp_path / "1p/google/ggl-5300-go-safety/cp.md").is_file()


def test_reactivation_finds_a_job_parked_in_the_accounts_bin_before_the_program_existed(
    tmp_path: Path,
) -> None:
    flat_job = replace(JOB_IN_PROGRAM, parent_code=ACCOUNT.code)
    _sync(tmp_path, (ACCOUNT, flat_job))
    _sync(tmp_path, (ACCOUNT,))
    assert (tmp_path / "1p/google/inactive/ggl-5136-go-safety-website/cp.md").is_file()

    _sync(tmp_path, (ACCOUNT, PROGRAM, JOB_IN_PROGRAM))
    assert (tmp_path / "1p/google/ggl-5300-go-safety/ggl-5136-go-safety-website/cp.md").is_file()
    assert not (tmp_path / "1p/google/inactive/ggl-5136-go-safety-website").exists()


def test_sweep_never_parks_the_account_layer_and_skips_job_subdirs(tmp_path: Path) -> None:
    _sync(tmp_path, (ACCOUNT, JOB_UNDER_ACCOUNT))
    (tmp_path / "1p/google/ggl-5168-activation/spine/Brief").mkdir(parents=True)
    moved = _deactivate_stale_cps(tmp_path, {("1p/google", JOB_UNDER_ACCOUNT.code, "")})
    assert moved == []
    assert (tmp_path / "1p/google/ggl-5168-activation/spine/Brief").is_dir()
    # Even with NOTHING live, the account layer stays (as before #302).
    moved = _deactivate_stale_cps(tmp_path, set())
    assert (tmp_path / "1p/google/cp.md").is_file()
    assert moved == [tmp_path / "1p/google/inactive/ggl-5168-activation"]


# ──────────────────────────────────────────────────────────────────────
#  Resolvers: index first, recursive walk second, inactive skipped
# ──────────────────────────────────────────────────────────────────────


def _make_tree(root: Path) -> Path:
    """A depth-3 dir with a cp.md under an account + program, no index."""
    deep = root / "1p/google/ggl-5300-go-safety/ggl-5136-go-safety-website"
    for d in (root / "1p/google", root / "1p/google/ggl-5300-go-safety", deep):
        d.mkdir(parents=True, exist_ok=True)
        (d / "cp.md").write_text("---\nProject: x\n---\n# x\n")
    (root / "1p/google/inactive/ggl-5136-go-safety-website").mkdir(parents=True)
    (root / "1p/google/inactive/ggl-5136-go-safety-website/cp.md").write_text("# parked\n")
    return deep


def _old_depth2_walk(root: Path, code: str) -> Path | None:
    """The pre-#302 hosted/webhook walk: depth 1 and depth 2 only. The
    CONTROL: it must miss a depth-3 dir, or the new walk proves nothing."""
    for scope in ("1p", "firstpersonsf", "canonic"):
        scope_dir = root / scope
        if not scope_dir.is_dir():
            continue
        for child in scope_dir.iterdir():
            if not child.is_dir() or child.name == "inactive":
                continue
            if child.name == code:
                return child
            for grandchild in child.iterdir():
                if grandchild.is_dir() and grandchild.name == code:
                    return grandchild
    return None


def test_control_old_depth2_walk_misses_a_depth3_dir(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    assert _old_depth2_walk(tmp_path, "ggl-5136-go-safety-website") is None


def _write_index(root: Path, code: str, path: str) -> None:
    (root / ".cp-engine").mkdir(exist_ok=True)
    (root / PATHS_INDEX_REL).write_text(json.dumps({
        "version": 1, "generated_at": "2026-09-24T00:00:00+00:00",
        "workstreams": {code: {"path": path, "parent": None, "has_agreement": True,
                               "label": "job", "mc2_id": None, "company": "GGL", "status": "Open"}},
    }) + "\n")


def test_find_working_dir_resolves_depth3_via_walk_and_via_index(tmp_path: Path) -> None:
    deep = _make_tree(tmp_path)
    # Walk (no index on disk): recursive, and the inactive copy never wins.
    assert find_working_dir(tmp_path, "ggl-5136-go-safety-website") == deep
    assert find_working_dir(tmp_path, "ggl-5136") == deep  # prefix form still works
    # Index: named directly, no walk needed even for a name the walk can't match.
    _write_index(tmp_path, "renamed-code", "1p/google/ggl-5300-go-safety/ggl-5136-go-safety-website")
    assert find_working_dir(tmp_path, "renamed-code") == deep
    # A stale index entry (dir gone) falls through to the walk.
    _write_index(tmp_path, "ggl-5136-go-safety-website", "1p/google/nowhere")
    assert find_working_dir(tmp_path, "ggl-5136-go-safety-website") == deep


def test_find_project_dir_under_expected_parent_is_recursive(tmp_path: Path) -> None:
    deep = _make_tree(tmp_path)
    assert _find_project_dir(tmp_path / "1p" / "google", "ggl-5136-go-safety-website") == deep
    # Shallower wins at equal rank: a same-named dir directly under the
    # parent beats the nested one.
    shallow = tmp_path / "1p/google/ggl-5136-go-safety-website"
    shallow.mkdir()
    assert _find_project_dir(tmp_path / "1p" / "google", "ggl-5136-go-safety-website") == shallow


def test_spine_find_spine_dir_reaches_depth3(tmp_path: Path) -> None:
    from cp_engine.spine import find_spine_dir

    deep = _make_tree(tmp_path)
    assert find_spine_dir(tmp_path, "ggl-5136-go-safety-website") == deep


def test_close_out_finds_a_dir_parked_under_a_programs_bin(tmp_path: Path) -> None:
    from cp_engine.close_out import find_close_workdir

    parked = tmp_path / "1p/google/ggl-5300-go-safety/inactive/ggl-5136-go-safety-website"
    parked.mkdir(parents=True)
    (parked / "cp.md").write_text("# parked\n")
    (tmp_path / "1p/google/ggl-5300-go-safety/cp.md").write_text("# program\n")
    assert find_close_workdir(tmp_path, "ggl-5136") == (parked, True)


def test_ingest_resolver_reaches_depth3(tmp_path: Path) -> None:
    from cp_engine.ingest import _resolve_project_cp_path

    deep = _make_tree(tmp_path)
    assert _resolve_project_cp_path(tmp_path, "ggl-5136-go-safety-website") == deep / "cp.md"


def test_transcript_resolver_prefers_live_over_parked(tmp_path: Path) -> None:
    from cp_engine.plan_from_transcript import _find_project_dir as find_tr

    deep = _make_tree(tmp_path)
    assert find_tr(tmp_path, "ggl-5136-go-safety-website") == deep
    # Only a parked copy: still found (a transcript for a just-parked
    # project has a home).
    only_parked = tmp_path / "1p/google/inactive/ggl-9999-old"
    only_parked.mkdir()
    assert find_tr(tmp_path, "ggl-9999-old") == only_parked


def test_webhook_resolver_reads_index_then_walks_depth3(tmp_path: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webhook"))
    from routers.sessions import _SPARSE_PATHS, _resolve_working_dir

    deep = _make_tree(tmp_path)
    assert _resolve_working_dir(tmp_path, "ggl-5136-go-safety-website") == deep
    _write_index(tmp_path, "renamed-code", "1p/google/ggl-5300-go-safety/ggl-5136-go-safety-website")
    assert _resolve_working_dir(tmp_path, "renamed-code") == deep
    # The sparse clone must materialise the index dir, or nothing reads it.
    assert ".cp-engine" in _SPARSE_PATHS


def test_hosted_find_project_dir_reads_index_then_walks_depth3(tmp_path: Path) -> None:
    import importlib.util

    server_path = Path(__file__).resolve().parents[1] / "prototypes/hosted-mcp/server.py"
    src = server_path.read_text(encoding="utf-8")
    # Load only the resolver trio — the module's import-time surface needs
    # the hosted runtime; the functions under test are pure filesystem code.
    import ast

    tree = ast.parse(src)
    wanted = {"_indexed_project_dir", "_iter_workstream_dirs", "find_project_dir"}
    consts = {"_PATHS_INDEX_REL", "_PATHS_INDEX_VERSION", "_SCOPE_DIRS"}
    nodes = [
        n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in wanted)
        or (isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id in consts)
    ]
    assert len(nodes) == len(wanted) + len(consts)
    ns: dict = {"Path": Path, "json": json}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(server_path), "exec"), ns)
    find_project_dir = ns["find_project_dir"]

    deep = _make_tree(tmp_path)
    assert find_project_dir(tmp_path, "ggl-5136-go-safety-website") == deep
    assert find_project_dir(tmp_path, "GGL-5136") == deep
    _write_index(tmp_path, "renamed-code", "1p/google/ggl-5300-go-safety/ggl-5136-go-safety-website")
    assert find_project_dir(tmp_path, "renamed-code") == deep
    assert _old_depth2_walk(tmp_path, "ggl-5136-go-safety-website") is None
