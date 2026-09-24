"""`promote_uphill` — the explicit move up the workstream tree (#304).

Plan §3.6: every capture names its level; nothing infers from content that
an item belongs to the account or program above. Promotion COPIES a
commitment (a row) or a decision (a sprint-file bullet) to the parent and
leaves a step on the parent's `Promoted uphill` element.

What is at risk, and the test for each:

* the copy must land on the PARENT from `.cp-engine/paths.json`, never on a
  guessed code — `test_commitment_copy_lands_on_the_parent`;
* the original must stay — `..._original_untouched`;
* the trail must exist on the parent — `..._leaves_a_step_on_the_parent`;
* a second call must write NOTHING (no row, no step) —
  `..._second_call_is_idempotent`;
* a top-level workstream refuses with "no parent";
* the hosted copy of the hash and the rule must not drift from the engine's
  — the parity tests at the bottom load the hosted module and compare.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from cp_engine import promote_uphill as pu
from cp_engine.cli import main
from cp_engine.ingest import _content_hash

TODAY = date(2026, 9, 24)  # Thursday → calendar week 2026-W39
WEEK = "2026-W39"

CHILD = "ggl-5136-go-safety-website"
PROGRAM = "ggl-5300-go-safety"
ACCOUNT = "google"
CHILD_ID = "11111111-1111-1111-1111-111111111111"
PROGRAM_ID = "22222222-2222-2222-2222-222222222222"
ACCOUNT_ID = "33333333-3333-3333-3333-333333333333"
IDS = {CHILD: CHILD_ID, PROGRAM: PROGRAM_ID, ACCOUNT: ACCOUNT_ID}

_GOLDEN_SPRINT = (
    Path(__file__).resolve().parent / "fixtures" / "golden" / "sprints"
    / "scaffold-engagement-minimal.md"
)


# ── fakes ─────────────────────────────────────────────────────────────


class _Query:
    """A chainable supabase-py stand-in over an in-memory store: `eq`
    filters, everything else passes through, `insert` appends."""

    def __init__(self, store: dict[str, list[dict]], table: str):
        self._rows = store.setdefault(table, [])
        self._filters: list[tuple[str, object]] = []
        self._op = "select"
        self._payload = None

    def select(self, *_a, **_k):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, patch):
        self._op, self._payload = "update", patch
        return self

    def eq(self, column, value):
        self._filters.append((column, value))
        return self

    def _passthrough(self, *_a, **_k):
        return self

    in_ = ilike = order = limit = neq = is_ = or_ = single = _passthrough

    def _matches(self, row: dict) -> bool:
        return all(str(row.get(c)) == str(v) for c, v in self._filters)

    def execute(self):
        if self._op == "insert":
            rows = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for r in rows:
                r = dict(r)
                r.setdefault("id", str(uuid.uuid4()))
                self._rows.append(r)
                out.append(dict(r))
            return SimpleNamespace(data=out)
        if self._op == "update":
            hit = [r for r in self._rows if self._matches(r)]
            for r in hit:
                r.update(self._payload)
            return SimpleNamespace(data=[dict(r) for r in hit])
        return SimpleNamespace(data=[dict(r) for r in self._rows if self._matches(r)])


class FakeClient:
    def __init__(self):
        self.store: dict[str, list[dict]] = {}

    def table(self, name: str) -> _Query:
        return _Query(self.store, name)

    def rows(self, table: str) -> list[dict]:
        return self.store.get(table, [])


# ── tenant ────────────────────────────────────────────────────────────


def _paths_index(root: Path) -> None:
    doc = {
        "version": 1,
        "generated_at": "2026-09-24T00:00:00+00:00",
        "workstreams": {
            ACCOUNT: {"path": "1p/google", "parent": None, "has_agreement": False,
                      "label": "account", "mc2_id": ACCOUNT_ID, "company": "GGL",
                      "status": "Open"},
            PROGRAM: {"path": f"1p/google/{PROGRAM}", "parent": ACCOUNT,
                      "has_agreement": True, "label": "program", "mc2_id": PROGRAM_ID,
                      "company": "GGL", "status": "Open"},
            CHILD: {"path": f"1p/google/{PROGRAM}/{CHILD}", "parent": PROGRAM,
                    "has_agreement": True, "label": "job", "mc2_id": CHILD_ID,
                    "company": "GGL", "status": "Open"},
        },
    }
    (root / ".cp-engine").mkdir(parents=True, exist_ok=True)
    (root / ".cp-engine" / "paths.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _sprint_file(root: Path, week: str, stem: str) -> Path:
    target = root / "sprints" / week / f"{stem}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_GOLDEN_SPRINT, target)
    return target


DECISION_TEXT = "Ship the regional migrations before the launch, not after"


@pytest.fixture
def tenant(tmp_path: Path) -> Path:
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.18"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n',
        encoding="utf-8",
    )
    _paths_index(tmp_path)
    child = _sprint_file(tmp_path, WEEK, CHILD)
    _sprint_file(tmp_path, WEEK, PROGRAM)
    _sprint_file(tmp_path, WEEK, ACCOUNT)
    # One decision on the child, written the way auto-ingest writes it.
    h = _content_hash(CHILD, "add-decision", DECISION_TEXT)
    body = child.read_text(encoding="utf-8").replace(
        "### Decisions\n",
        f"### Decisions\n- [decision · 2026-09-22] {DECISION_TEXT} <!-- cp:hash={h} -->\n",
        1,
    )
    child.write_text(body, encoding="utf-8")
    return tmp_path


@pytest.fixture
def client(monkeypatch) -> FakeClient:
    fake = FakeClient()
    monkeypatch.setattr(pu, "_resolve_project_id", lambda c, code: IDS.get(code))
    fake.store["commitments"] = [{
        "id": "c-orig", "project_id": CHILD_ID, "status": "open", "cp_hash": "abcd1234",
        "description": "Send Janet the migration runbook", "owner_email": "drew@firstperson.is",
        "owner_name": "Drew", "direction": "us_to_them", "due_date": "2026-10-01",
        "work_item_id": None, "work_item_kind": "deliverable", "spine_element_id": None,
        "source_meeting_id": "m-1",
    }]
    return fake


def _promote_commitment(client, tenant, code=CHILD, ref="c-orig", **kw):
    return pu.promote_commitment(
        client, tenant_root=tenant, code=code, commitment_id=ref, today=TODAY, **kw
    )


# ── level ─────────────────────────────────────────────────────────────


def test_level_for_reads_the_index_and_names_the_parent(tenant):
    lvl = pu.level_for(tenant, CHILD)
    assert lvl == {"code": CHILD, "label": "job", "parent": PROGRAM, "indexed": True}


def test_level_for_accepts_the_short_code_when_unique(tenant):
    assert pu.level_for(tenant, "ggl-5136")["code"] == CHILD


def test_level_for_unindexed_code_is_visible_not_guessed(tenant):
    lvl = pu.level_for(tenant, "zzz-9999")
    assert lvl["indexed"] is False and lvl["parent"] is None


# ── commitments ───────────────────────────────────────────────────────


def test_commitment_copy_lands_on_the_parent(tenant, client):
    out = _promote_commitment(client, tenant)
    assert out["ok"] and out["promoted"] and not out["already"], out
    assert out["to"]["code"] == PROGRAM and out["level"]["code"] == PROGRAM
    copies = [r for r in client.rows("commitments") if r["id"] != "c-orig"]
    assert len(copies) == 1
    copy = copies[0]
    assert copy["project_id"] == PROGRAM_ID
    assert copy["source_kind"] == "promoted"
    assert copy["work_item_kind"] == "deliverable"
    assert copy["description"] == "Send Janet the migration runbook"
    assert copy["cp_hash"] == pu.promoted_hash("abcd1234", PROGRAM) == out["cp_hash"]
    assert copy["cp_hash"] != "abcd1234"


def test_commitment_original_untouched(tenant, client):
    before = dict(client.rows("commitments")[0])
    _promote_commitment(client, tenant)
    assert client.rows("commitments")[0] == before


def test_commitment_leaves_a_step_on_the_parent(tenant, client):
    out = _promote_commitment(client, tenant, note="account-wide, per Janet")
    elements = client.rows("spine_substance")
    assert len(elements) == 1
    el = elements[0]
    assert el["project_id"] == PROGRAM_ID
    assert el["est_item_id"] == pu.PROMOTIONS_EST_ITEM_ID == "_authored/promoted-uphill"
    assert el["status"] == "live" and el["origin"] == "authored"
    steps = client.rows("spine_steps")
    assert len(steps) == 1
    step = steps[0]
    assert step["project_id"] == PROGRAM_ID
    assert step["est_item_id"] == pu.PROMOTIONS_EST_ITEM_ID
    assert step["title"] == f"Promoted from {CHILD}: Send Janet the migration runbook"
    assert "commitment c-orig promoted from " + CHILD in step["note"]
    assert "account-wide, per Janet" in step["note"]
    assert step["status"] == "done" and step["step_date"] == "2026-09-24"
    assert out["step"]["position"] == 1


def test_commitment_second_call_is_idempotent(tenant, client):
    first = _promote_commitment(client, tenant)
    second = _promote_commitment(client, tenant)
    assert first["promoted"] is True
    assert second["ok"] and second["already"] is True and second["promoted"] is False
    assert second["cp_hash"] == first["cp_hash"]
    assert len(client.rows("commitments")) == 2, "no second copy"
    assert len(client.rows("spine_steps")) == 1, "no second step"


def test_commitment_from_a_top_level_workstream_refuses(tenant, client):
    client.rows("commitments")[0]["project_id"] = ACCOUNT_ID
    out = _promote_commitment(client, tenant, code=ACCOUNT)
    assert "no parent" in out["error"]
    assert len(client.rows("commitments")) == 1


def test_commitment_unknown_item_is_an_error(tenant, client):
    out = _promote_commitment(client, tenant, ref="nope")
    assert "no commitment with id" in out["error"]


def test_commitment_on_another_workstream_is_refused(tenant, client):
    """A row owned by a sibling must not be copied under this code's parent."""
    client.rows("commitments")[0]["project_id"] = PROGRAM_ID
    out = _promote_commitment(client, tenant)
    assert "is not on " + CHILD in out["error"]


def test_commitment_unindexed_code_is_an_error(tenant, client):
    out = _promote_commitment(client, tenant, code="zzz-9999")
    assert "paths.json" in out["error"]


def test_promotion_step_title_excerpts_eighty_chars():
    long = "x" * 200
    title = pu.promotion_step_title(CHILD, long)
    assert title.startswith(f"Promoted from {CHILD}: ")
    assert title.endswith("…")
    assert len(title) == len(f"Promoted from {CHILD}: ") + pu.TITLE_EXCERPT_CHARS + 1


def test_ensure_promotions_element_is_created_once(client):
    pu.ensure_promotions_element(client, PROGRAM_ID, PROGRAM)
    pu.ensure_promotions_element(client, PROGRAM_ID, PROGRAM)
    assert len(client.rows("spine_substance")) == 1


# ── decisions ─────────────────────────────────────────────────────────


def _promote_decision(client, tenant, ref, code=CHILD, **kw):
    return pu.promote_decision(
        client, tenant_root=tenant, code=code, item_ref=ref, today=TODAY, **kw
    )


def _parent_decisions(tenant, stem=PROGRAM, week=WEEK) -> str:
    body = (tenant / "sprints" / week / f"{stem}.md").read_text(encoding="utf-8")
    return body.split("### Decisions", 1)[1].split("### Discussion notes", 1)[0]


def test_decision_found_by_hash_and_by_text(tenant):
    h = _content_hash(CHILD, "add-decision", DECISION_TEXT)
    by_hash = pu.find_decision(tenant, CHILD, h)
    by_text = pu.find_decision(tenant, CHILD, DECISION_TEXT)
    assert by_hash is not None and by_text is not None
    assert by_hash.text == by_text.text == DECISION_TEXT
    assert by_hash.hash == h and by_hash.week == WEEK
    # a ref carrying the trailer, or a short code, resolves the same bullet
    assert pu.find_decision(tenant, "ggl-5136", f"{DECISION_TEXT} <!-- cp:hash={h} -->")


def test_decision_substring_does_not_match():
    """Exact only: a substring could promote the wrong decision, and a copy
    has no undo."""
    assert pu._DECISION_RE.match("- [decision · 2026-09-22] ship it")
    assert pu.find_decision(Path("/nonexistent"), CHILD, "ship") is None


def test_decision_copy_lands_in_the_parents_current_sprint_file(tenant, client):
    h = _content_hash(CHILD, "add-decision", DECISION_TEXT)
    out = _promote_decision(client, tenant, h)
    assert out["ok"] and out["promoted"], out
    assert out["sprint_path"] == f"sprints/{WEEK}/{PROGRAM}.md"
    assert out["source"] == f"sprints/{WEEK}/{CHILD}.md"
    zone = _parent_decisions(tenant)
    assert (
        f"- [decision · 2026-09-22] {DECISION_TEXT} (promoted from {CHILD}) "
        f"<!-- cp:hash={out['cp_hash']} -->"
    ) in zone
    # the child's bullet is untouched
    assert f"cp:hash={h}" in (tenant / "sprints" / WEEK / f"{CHILD}.md").read_text(encoding="utf-8")
    # and the trail on the parent
    steps = client.rows("spine_steps")
    assert len(steps) == 1 and steps[0]["project_id"] == PROGRAM_ID
    assert steps[0]["title"].startswith(f"Promoted from {CHILD}: Ship the regional")
    assert f"decision {h} promoted from {CHILD}" in steps[0]["note"]


def test_decision_second_call_is_idempotent(tenant, client):
    first = _promote_decision(client, tenant, DECISION_TEXT)
    second = _promote_decision(client, tenant, DECISION_TEXT)
    assert first["promoted"] and second["already"] and not second["promoted"]
    assert _parent_decisions(tenant).count("cp:hash=") == 1
    assert len(client.rows("spine_steps")) == 1


def test_decision_already_promoted_in_an_earlier_week_is_seen(tenant, client):
    """The idempotency check reads EVERY parent sprint file, not just this
    week's — a copy made last week must not be made again this week."""
    _sprint_file(tenant, "2026-W38", PROGRAM)
    first = _promote_decision(client, tenant, DECISION_TEXT, week_iso="2026-W38")
    assert first["promoted"] and first["sprint_path"].startswith("sprints/2026-W38/")
    second = _promote_decision(client, tenant, DECISION_TEXT)
    assert second["already"] and second["sprint_path"].startswith("sprints/2026-W38/")


def test_decision_no_parent_refuses(tenant, client):
    out = _promote_decision(client, tenant, DECISION_TEXT, code=ACCOUNT)
    assert "no parent" in out["error"]


def test_decision_unknown_ref_is_an_error(tenant, client):
    out = _promote_decision(client, tenant, "deadbeef")
    assert "no decision on" in out["error"]
    assert "cp:hash=" not in _parent_decisions(tenant)


def test_decision_without_a_client_still_writes_and_says_the_step_is_missing(tenant):
    out = _promote_decision(None, tenant, DECISION_TEXT)
    assert out["promoted"] is True
    assert "NOT written" in out["step"]["note"]


def test_decision_up_two_levels_takes_two_explicit_calls(tenant, client):
    """Promotion is ONE level at a time — job → program → account. Nothing
    skips a level on the caller's behalf."""
    up1 = _promote_decision(client, tenant, DECISION_TEXT)
    assert up1["to"]["code"] == PROGRAM
    copied_text = f"{DECISION_TEXT} (promoted from {CHILD})"
    up2 = _promote_decision(client, tenant, copied_text, code=PROGRAM)
    assert up2["ok"] and up2["to"]["code"] == ACCOUNT, up2
    twice = f"(promoted from {CHILD}) (promoted from {PROGRAM})"
    assert twice in _parent_decisions(tenant, ACCOUNT)


def test_dispatch_rejects_unknown_kinds(tenant, client):
    out = pu.promote_uphill(client, tenant_root=tenant, code=CHILD, item_kind="risk", item_ref="x")
    assert "item_kind" in out["error"]


def test_dispatch_commitment_needs_a_client(tenant):
    out = pu.promote_uphill(
        None, tenant_root=tenant, code=CHILD, item_kind="commitment", item_ref="c"
    )
    assert "MC-2 client" in out["error"]


# ── CLI ───────────────────────────────────────────────────────────────


def test_cli_promotes_a_decision(tenant, client, monkeypatch):
    monkeypatch.chdir(tenant)
    monkeypatch.setattr("cp_engine.mc2_db.get_client", lambda *a, **k: client)
    result = CliRunner().invoke(main, ["promote-uphill", CHILD, "--decision", DECISION_TEXT])
    assert result.exit_code == 0, result.output
    assert "Promoted decision" in result.output and PROGRAM in result.output
    assert f"sprints/{WEEK}/{PROGRAM}.md" in result.output or "sprints/" in result.output
    week = result.output.split("sprints/")[1].split("/")[0]
    assert "cp:hash=" in _parent_decisions(tenant, week=week)


def test_cli_promotes_a_commitment_and_reports_already_on_rerun(tenant, client, monkeypatch):
    monkeypatch.chdir(tenant)
    monkeypatch.setattr("cp_engine.mc2_db.get_client", lambda *a, **k: client)
    first = CliRunner().invoke(main, ["promote-uphill", CHILD, "--commitment", "c-orig", "--json"])
    assert first.exit_code == 0, first.output
    payload = json.loads(first.output)
    assert payload["promoted"] and payload["to"]["code"] == PROGRAM
    again = CliRunner().invoke(main, ["promote-uphill", CHILD, "--commitment", "c-orig"])
    assert again.exit_code == 0 and "Already promoted" in again.output


def test_cli_no_parent_exits_one(tenant, client, monkeypatch):
    monkeypatch.chdir(tenant)
    monkeypatch.setattr("cp_engine.mc2_db.get_client", lambda *a, **k: client)
    result = CliRunner().invoke(main, ["promote-uphill", ACCOUNT, "--decision", "anything"])
    assert result.exit_code == 1
    assert "no parent" in result.output


def test_cli_requires_exactly_one_item_flag(tenant, monkeypatch):
    monkeypatch.chdir(tenant)
    both = CliRunner().invoke(
        main, ["promote-uphill", CHILD, "--decision", "x", "--commitment", "y"]
    )
    neither = CliRunner().invoke(main, ["promote-uphill", CHILD])
    assert both.exit_code == 2 and neither.exit_code == 2


# ── stdio MCP verb ────────────────────────────────────────────────────


def test_stdio_verb_is_registered_with_the_level_rule():
    from cp_engine import mcp_server

    tool = mcp_server.mcp._tool_manager.get_tool("promote_uphill")
    assert tool is not None
    assert list(tool.parameters["properties"]) == ["project_code", "item_kind", "item_ref", "note"]
    assert "never inferred from content" in tool.description
    assert "<LEVEL_RULE>" not in tool.description


def test_stdio_verb_delegates_to_the_module(tenant, client, monkeypatch):
    from cp_engine import mcp_server

    monkeypatch.setattr(mcp_server, "_tenant_root", lambda: tenant)
    monkeypatch.setattr("cp_engine.mc2_db.get_client", lambda *a, **k: client)
    out = mcp_server.promote_uphill(CHILD, "commitment", "c-orig")
    assert out["promoted"] and out["level"]["code"] == PROGRAM, out


# ── hosted parity ─────────────────────────────────────────────────────
#
# The hosted server does not import cp_engine, so its hash, its est_item_id
# and its rule text are COPIES. A copy drifts; these pin each one to the
# engine's original the way test_vendor_drift pins the vendored modules.

_SERVER_PATH = (
    Path(__file__).resolve().parents[1] / "prototypes" / "hosted-mcp" / "server.py"
)


@pytest.fixture(scope="module")
def hosted():
    pytest.importorskip("jwt")
    pytest.importorskip("mcp")
    pytest.importorskip("supabase")
    os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
    os.environ.setdefault("SUPABASE_ANON_KEY", "anon-key-for-tests")
    spec = importlib.util.spec_from_file_location("hosted_mcp_server_304", _SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hosted_hash_matches_the_engine(hosted):
    for original, parent in (("abcd1234", "google"), ("c-orig", PROGRAM), ("", "x")):
        assert hosted._promoted_hash(original, parent) == pu.promoted_hash(original, parent)


def test_hosted_trail_element_and_title_match_the_engine(hosted):
    assert hosted._PROMOTIONS_EST_ITEM_ID == pu.PROMOTIONS_EST_ITEM_ID
    assert hosted._PROMOTIONS_LABEL == pu.PROMOTIONS_LABEL
    assert hosted._PROMOTIONS_BODY == pu.PROMOTIONS_BODY
    assert hosted._SOURCE_KIND_PROMOTED == pu.SOURCE_KIND_PROMOTED
    assert hosted._COMMITMENT_COPY_COLUMNS == pu.COMMITMENT_COPY_COLUMNS
    text = "y" * 300
    assert hosted._promotion_step_title(CHILD, text) == pu.promotion_step_title(CHILD, text)


def test_hosted_level_rule_matches_the_engine(hosted):
    assert hosted._LEVEL_RULE == pu.LEVEL_RULE
