"""Asks live in MC-2 (architecture plan step 4a, decided 2026-10-01).

The defects these pin, all measured on the tenant 2026-10-01:

1. A closure in MC-2 never reached the sprint file — 388 asks read open in
   the files while their commitment was done, dropped or routed.
2. A hand-typed ask never reached MC-2 — 704 open sprint asks had no row.
3. One ask, two hashes: meeting-ingest keyed the SHORT code, the sprint bullet
   the FULL one, and the hosted server wrote a random hash on purpose. Only
   114 of 431 real pairs shared a hash.
4. ``close-ask`` neither redirected to the owning week nor matched a carried
   ``[ask · …]`` bullet, so a Slack "Mark closed" on a carried ask changed
   nothing durable.

CONTROLS. Every test here is written against surfaces that exist on the
pre-4a engine too (``ensure_sprint_file``, ``execute_plan``, the webhook's
``propose_commitments``, ``open_client_asks``), and calls the new sync step
through :func:`_sync`, which is a no-op where it does not exist. Run against
origin/main's ``src`` they FAIL on their assertions — not on an import — which
is what proves each one tests the defect rather than the new code's shape.
"""

from __future__ import annotations

import re
import sys
import uuid
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from cp_engine.aggregators import open_client_asks
from cp_engine.ingest import _write_ask, execute_plan
from cp_engine.sprints import ensure_sprint_file, parse_sprint_file
from tests.test_sprints import _fixture_project

CODE = "slt-5196-brand-campaign-26"
SHORT = "slt-5196"
PID = "11111111-2222-3333-4444-555555555555"
TODAY = date(2026, 9, 30)


# ── a minimal PostgREST-shaped fake ─────────────────────────────────────


class _Q:
    def __init__(self, store, table):
        self.rows = store.setdefault(table, [])
        self.filters: list = []
        self.op, self.payload = "select", None
        self.lo, self.hi = 0, None

    def select(self, *_a, **_k):
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def in_(self, col, vals):
        self.filters.append((col, tuple(vals)))
        return self

    def range(self, lo, hi):
        self.lo, self.hi = lo, hi
        return self

    def limit(self, n):
        self.hi = n - 1
        return self

    def order(self, *_a, **_k):
        return self

    def single(self):
        self.single_row = True
        return self

    def _hit(self, r):
        for col, val in self.filters:
            if isinstance(val, tuple):
                if r.get(col) not in val:
                    return False
            elif str(r.get(col)) != str(val):
                return False
        return True

    def execute(self):
        if self.op == "insert":
            out = []
            for r in (self.payload if isinstance(self.payload, list) else [self.payload]):
                r = {"id": str(uuid.uuid4()), "created_at": "2026-09-30T12:00:00+00:00",
                     "status": "open", "direction": "internal", **r}
                if r.get("cp_hash") and any(x.get("cp_hash") == r["cp_hash"] for x in self.rows):
                    raise RuntimeError("duplicate key value violates commitments_cp_hash_uidx")
                self.rows.append(r)
                out.append(dict(r))
            return SimpleNamespace(data=out)
        hit = [r for r in self.rows if self._hit(r)]
        if self.op == "update":
            for r in hit:
                r.update(self.payload)
            return SimpleNamespace(data=[dict(r) for r in hit])
        hi = len(hit) if self.hi is None else self.hi + 1
        if getattr(self, "single_row", False):
            return SimpleNamespace(data=dict(hit[0]) if hit else None)
        return SimpleNamespace(data=[dict(r) for r in hit[self.lo:hi]])


class FakeMC2:
    def __init__(self, commitments=()):
        self.store = {
            "commitments": [dict(c) for c in commitments],
            "projects": [{"id": PID, "number": 5196,
                          "full_job_name": "SLT 5196 Brand Campaign 26"}],
            "entities": [{"name": "Drew Fiero", "email": "drew@firstperson.is",
                          "archived_at": None, "kind": "staff"}],
        }

    def table(self, name):
        return _Q(self.store, name)

    @property
    def commitments(self):
        return self.store["commitments"]


def _row(desc, *, status="open", cp_hash=None, **kw):
    return {"id": str(uuid.uuid4()), "description": desc, "status": status,
            "cp_hash": cp_hash, "project_id": PID, "owner_name": kw.pop("owner_name", "Morgan Wright"),
            "owner_email": None, "direction": "them_to_us", "due_date": kw.pop("due_date", None),
            "date_status": "proposed", "source_kind": "meeting_ingest",
            "created_at": kw.pop("created_at", "2026-09-22T12:00:00+00:00"),
            "updated_at": None, **kw}


# ── fixtures ────────────────────────────────────────────────────────────


def _project():
    return replace(_fixture_project(code=CODE), mc2_id=PID, company_name="Salesloft")


def _render(tmp_path: Path, week: str, prior: str | None) -> Path:
    return ensure_sprint_file(
        project=_project(), sprint_root=tmp_path / "sprints", week_iso=week,
        week_label=week[-3:], week_start="2026-09-28", week_end="2026-10-04",
        prior_sprint=prior, last_sprint_hours_line=None, sessions_this_week=0,
        last_session_date=None, last_session_who=None, last_session_summary=None,
        recent_commits=(), open_issues=(),
    )


def _sync(client, tmp_path: Path, week: str) -> None:
    """The step-4a sync pass, or nothing on an engine that has none."""
    from cp_engine import sync

    step = getattr(sync, "_sync_asks_from_mc2", None)
    if step is not None:
        step(client, tmp_path / "sprints", [_project()], week_iso=week, today=TODAY)


def _type_ask(path: Path, line: str) -> None:
    body = path.read_text()
    marker = "### Open asks\n"
    path.write_text(body.replace(marker, marker + line + "\n", 1))


def _open_texts(path: Path) -> list[str]:
    return [re.sub(r"\s*<!--.*?-->", "", a.text).strip()
            for a in open_client_asks(parse_sprint_file(path))]


# ── 1. a closure in MC-2 disappears from the next render ────────────────


def test_closure_in_mc2_disappears_from_the_next_render(tmp_path):
    """W39 raised the ask in the file; MC-2 then closed it (hosted
    resolve_commitment / Slack). Pre-4a the W40 render carried it from W39's
    file and every surface counted it open; now W40 renders MC-2's set."""
    w39 = _render(tmp_path, "2026-W39", None)
    _type_ask(w39, "- [open · 2026-09-22 · Morgan Wright] Send CAB agenda to David Schloss")
    # A legacy (short-code) hash: the match is by text within the workstream.
    client = FakeMC2([_row("Send CAB agenda to David Schloss", status="done",
                           cp_hash="3ab826fc")])
    w40 = _render(tmp_path, "2026-W40", "2026-W39")
    _sync(client, tmp_path, "2026-W40")
    assert "Send CAB agenda to David Schloss" not in _open_texts(w40)
    assert "Send CAB agenda" not in w40.read_text()


def test_an_open_commitment_renders_and_leaves_once_resolved(tmp_path):
    h = "b00c7a1e"
    client = FakeMC2([_row("Book travel for the nine participants", cp_hash=h,
                           due_date="2026-10-03")])
    w40 = _render(tmp_path, "2026-W40", None)
    _sync(client, tmp_path, "2026-W40")
    body = w40.read_text()
    assert ("- [open · 2026-09-22 · Morgan Wright · by 2026-10-03] Book travel for "
            f"the nine participants <!-- cp:hash={h} -->") in body
    assert _open_texts(w40) == ["Book travel for the nine participants"]

    client.commitments[0]["status"] = "done"
    _sync(client, tmp_path, "2026-W40")
    assert _open_texts(w40) == []
    assert "Book travel" not in w40.read_text()


# ── 2. a hand-typed ask is imported once, never duplicated ──────────────


def test_hand_typed_ask_is_imported_once_and_not_duplicated(tmp_path):
    client = FakeMC2()
    w40 = _render(tmp_path, "2026-W40", None)
    _type_ask(w40, "- [open · 2026-09-28 · Drew · by 2026-10-02] Send the roster sheet to Morgan")
    _sync(client, tmp_path, "2026-W40")
    _sync(client, tmp_path, "2026-W40")  # the second pass must be a no-op

    rows = [r for r in client.commitments if "roster sheet" in r["description"]]
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "open" and row["source_kind"] == "sprint_import"
    assert row["due_date"] == "2026-10-02"
    assert row["owner_email"] == "drew@firstperson.is"  # roster-resolved
    assert row["created_at"].startswith("2026-09-28")  # age stays legible
    body = w40.read_text()
    assert body.count("Send the roster sheet to Morgan") == 1  # moved, not copied
    assert _open_texts(w40) == ["Send the roster sheet to Morgan"]


def test_retyped_closed_ask_is_not_resurrected(tmp_path):
    """MC-2 wins: retyping an ask that was DROPPED matches it (any status)."""
    client = FakeMC2([_row("Draft interview guide for the CAB", status="dropped")])
    w40 = _render(tmp_path, "2026-W40", None)
    _type_ask(w40, "- [open · 2026-09-29 · Drew] Draft interview guide for the CAB.")
    _sync(client, tmp_path, "2026-W40")
    assert len(client.commitments) == 1
    assert _open_texts(w40) == []


def test_old_ask_in_an_earlier_week_is_imported_without_editing_that_week(tmp_path):
    client = FakeMC2()
    w39 = _render(tmp_path, "2026-W39", None)
    _type_ask(w39, "- [open · 2026-09-23 · Leah Ward] Confirm the on-stage slot")
    before = w39.read_text()
    w40 = _render(tmp_path, "2026-W40", "2026-W39")
    _sync(client, tmp_path, "2026-W40")
    _sync(client, tmp_path, "2026-W40")
    assert [r["description"] for r in client.commitments] == ["Confirm the on-stage slot"]
    assert w39.read_text() == before  # history is not edited
    assert _open_texts(w40) == ["Confirm the on-stage slot"]


def test_a_failed_import_keeps_the_bullet(tmp_path, monkeypatch):
    client = FakeMC2()
    w40 = _render(tmp_path, "2026-W40", None)
    _type_ask(w40, "- [open · 2026-09-28 · Drew] Never lose this ask")
    from cp_engine import asks

    def boom(*_a, **_k):
        raise RuntimeError("insert refused")

    monkeypatch.setattr(asks, "_insert", boom)
    _sync(client, tmp_path, "2026-W40")
    assert "Never lose this ask" in w40.read_text()


def test_snooze_marker_survives_the_rerender(tmp_path):
    h = "5a0e0e01"
    client = FakeMC2([_row("Chase the logo files", cp_hash=h)])
    w40 = _render(tmp_path, "2026-W40", None)
    _sync(client, tmp_path, "2026-W40")
    from cp_engine.ingest import _write_snooze

    assert _write_snooze(CODE, {"hash": h, "until": "2026-10-09"}, w40, bullet_kind="ask")
    _sync(client, tmp_path, "2026-W40")
    assert "cp:snoozed-until=2026-10-09" in w40.read_text()
    assert not (tmp_path / "exceptions").exists()  # an engine write, not foreign


# ── 3. one hash recipe across writers ───────────────────────────────────


def test_short_and_full_code_hashes_now_match(tmp_path, monkeypatch):
    """The meeting webhook tags `slt-5196`; the sprint bullet lives in
    `slt-5196-brand-campaign-26.md`. Same ask, so same hash."""
    webhook = Path(__file__).resolve().parents[1] / "webhook"
    monkeypatch.syspath_prepend(str(webhook))
    import commitments_propose as cp_mod

    from cp_engine import mc2_db

    client = FakeMC2()
    client.store["fathom_meetings"] = [{
        "id": "m-1", "participants": [],
        "action_items": [{"description": "Send wardrobe guidance to the nine interviewees",
                          "assignee": {"name": "Morgan Wright"}}],
    }]
    monkeypatch.setattr(mc2_db, "get_client", lambda *a, **k: client)
    cp_mod.propose_commitments("m-1", [SHORT])
    (row,) = client.commitments

    w40 = _render(tmp_path, "2026-W40", None)
    _write_ask(CODE, {"text": "Send wardrobe guidance to the nine interviewees",
                      "who": "Morgan Wright", "date": "2026-09-28"}, w40)
    bullet_hash = re.search(r"cp:hash=([0-9a-f]{8})", w40.read_text()).group(1)
    assert row["cp_hash"] == bullet_hash


# ── 4. close-ask on a carried ask ───────────────────────────────────────

_CARRIED = "- [open · 2026-09-15 · Rena] Approve the Round 3 pop-up copy <!-- cp:hash=3260a5c1 -->"


def _carried_tenant(tmp_path: Path) -> tuple[Path, Path]:
    """W38 owns the ask; W39 is a file rendered BEFORE step 4a, whose
    `carry-forward` region still holds the carried `[ask · …]` row."""
    w38 = _render(tmp_path, "2026-W38", None)
    _type_ask(w38, _CARRIED)
    w39 = _render(tmp_path, "2026-W39", "2026-W38")
    body = w39.read_text()
    body = re.sub(
        r"(## Carried over from [^\n]*\n)",
        r"\1- [ask · 2026-09-15 · Rena] Approve the Round 3 pop-up copy <!-- cp:hash=3260a5c1 -->\n",
        body, count=1,
    )
    w39.write_text(body)
    return w38, w39


def test_close_ask_on_a_carried_ask_closes_it_where_it_lives(tmp_path):
    """The Slack button names W39 (the week the digest read) and the hash.
    Pre-4a the writer looked only in W39, whose only copy was the carried
    `[ask ·` row its pattern could not match — nothing changed, the ask stayed
    open in W38 and came back next week."""
    w38, _w39 = _carried_tenant(tmp_path)
    plan = {"projects": {CODE: {"close-ask": [{"hash": "3260a5c1", "closed_by": "slack"}]}}}
    result = execute_plan(plan, tenant_root=tmp_path, today=TODAY, week_iso="2026-W39")
    assert result.errors == [], result.errors
    assert "[closed · 2026-09-15 · Rena] Approve the Round 3 pop-up copy" in w38.read_text()
    assert w38 in result.files_written


def test_close_ask_resolves_the_commitment(tmp_path):
    h = "cab0a6e1"
    client = FakeMC2([_row("Send the CAB agenda", cp_hash=h)])
    w40 = _render(tmp_path, "2026-W40", None)
    _sync(client, tmp_path, "2026-W40")
    assert "Send the CAB agenda" in w40.read_text()

    plan = {"projects": {CODE: {"close-ask": [{"hash": h}]}}}
    result = execute_plan(plan, tenant_root=tmp_path, today=TODAY,
                          week_iso="2026-W40", supabase=client)
    assert result.errors == [], result.errors
    assert client.commitments[0]["status"] == "done"
    assert "Send the CAB agenda" not in w40.read_text()  # gone without a re-sync
    assert not (tmp_path / "exceptions").exists()


# ── the recipe itself ───────────────────────────────────────────────────


def test_recipe_ignores_markers_case_whitespace_and_annotations():
    from cp_engine.asks import ask_hash

    a = ask_hash(CODE, "Send the deck to Janet.")
    assert a == ask_hash(CODE, "  send the  deck to Janet <!-- cp:hash=deadbeef -->")
    assert a == ask_hash(CODE, "Send the deck to Janet [owner unresolved: Speaker 2]")
    assert a != ask_hash("ibx-5153-ai-campaign", "Send the deck to Janet")
    # Any spelling of the code keys the same: the short form is the key.
    assert a == ask_hash(SHORT, "Send the deck to Janet")
    assert a == ask_hash("SLT 5196 Brand Campaign 26", "Send the deck to Janet")
    assert a != ask_hash(CODE, "Send the deck to Janet", occurrence=1)


def test_canonical_code_resolves_short_codes_through_mc2():
    from cp_engine.asks import resolve_canonical_code

    client = FakeMC2()
    assert resolve_canonical_code(SHORT, client=client) == CODE
    assert resolve_canonical_code(SHORT, client=client, project_id=PID) == CODE
    assert resolve_canonical_code(CODE) == CODE


@pytest.mark.parametrize("text, who, internal, expected", [
    ("Send the signed SOW to Drew", "Janet", False, "them_to_us"),
    ("Share the brand guide with FP", "Leah Ward (Salesloft)", False, "them_to_us"),
    ("Send the revised deck to Salesloft", "Drew", True, "us_to_them"),
    ("Confirm the venue", "Janet", False, None),
    ("Send the deck to Janet", "Drew", True, None),  # recipient unknown → default
])
def test_direction_is_inferred_only_when_the_wording_is_unambiguous(text, who, internal, expected):
    from cp_engine.asks import infer_direction

    assert infer_direction(text, who=who, owner_is_internal=internal,
                           company_name="Salesloft") == expected


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))


def test_a_file_scaffolded_before_the_region_gets_it_at_sync(tmp_path):
    """Current-week files written before step 4a have no `open-asks` markers;
    the first sync seeds them under `### Open asks` and renders into them."""
    w40 = _render(tmp_path, "2026-W40", None)
    body = w40.read_text()
    body = re.sub(r"<!-- cp-engine:start open-asks -->.*?<!-- cp-engine:end open-asks -->\n",
                  "", body, flags=re.S)
    w40.write_text(body)
    assert "open-asks" not in w40.read_text()  # premise: the legacy shape
    client = FakeMC2([_row("Confirm the shoot date", cp_hash="d47e0001")])
    _sync(client, tmp_path, "2026-W40")
    out = w40.read_text()
    assert out.index("### Open asks") < out.index("<!-- cp-engine:start open-asks -->")
    assert "Confirm the shoot date" in out
    assert _open_texts(w40) == ["Confirm the shoot date"]


def test_an_ask_closed_in_a_later_week_is_not_imported_from_its_older_copy(tmp_path):
    client = FakeMC2()
    w38 = _render(tmp_path, "2026-W38", None)
    _type_ask(w38, "- [open · 2026-09-15 · Rena] Approve the pop-up copy <!-- cp:hash=0a0a0a0a -->")
    w39 = _render(tmp_path, "2026-W39", "2026-W38")
    _type_ask(w39, "- [closed · 2026-09-15 · Rena] Approve the pop-up copy <!-- cp:hash=0a0a0a0a -->")
    _render(tmp_path, "2026-W40", "2026-W39")
    _sync(client, tmp_path, "2026-W40")
    assert client.commitments == []


# ── 2026-10-01 amendments (Drew): short-code key, reconcile policy, prep ──


def test_short_code_hash_survives_a_rename(tmp_path):
    """The hash keys on `<co>-<number>`: the same ask in a renamed
    workstream's sprint file keeps its identity. Control: on the full-slug
    recipe the two bullets hashed differently."""
    before = tmp_path / "a.md"
    after = tmp_path / "b.md"
    for p in (before, after):
        p.write_text("## Client communication\n### Open asks\n")
    item = {"text": "Send the CAB agenda to David Schloss", "who": "Morgan", "date": "2026-09-28"}
    _write_ask("slt-5196-brand-campaign-26", dict(item), before)
    _write_ask("slt-5196-brand-campaign-2026", dict(item), after)
    h = lambda p: re.search(r"cp:hash=([0-9a-f]{8})", p.read_text()).group(1)  # noqa: E731
    assert h(before) == h(after)


def _reconcile_tenant(tmp_path: Path) -> tuple[Path, "FakeMC2"]:
    """Three workstreams — Open, Holding, Closed — each with one recent
    unmatched ask, plus an Open-workstream ask that restates an existing
    commitment in other words."""
    import json

    ws = {
        "slt-5196-brand-campaign-26": (PID, "Open", 5196),
        "slt-5197-holding-job": ("p-hold", "Holding", 5197),
        "slt-5198-closed-job": ("p-closed", "Closed", 5198),
    }
    (tmp_path / ".cp-engine").mkdir()
    (tmp_path / ".cp-engine/paths.json").write_text(json.dumps({"workstreams": {
        code: {"mc2_id": pid, "status": st, "path": code} for code, (pid, st, _n) in ws.items()
    }}))
    week = tmp_path / "sprints" / "2026-W40"
    week.mkdir(parents=True)
    for code in ws:
        asks = f"- [open · 2026-09-28 · Leah Ward] Confirm the venue for {code}\n"
        if code.startswith("slt-5196"):
            asks += ("- [open · 2026-09-28 · Morgan Wright] Send the CAB meeting "
                     "agenda over to David Schloss this week\n")
        (week / f"{code}.md").write_text(
            "## Client communication\n### Open asks\n" + asks + "### Inbound\n")
    client = FakeMC2([_row("Send CAB agenda to David Schloss")])
    client.store["projects"] = [
        {"id": pid, "number": n, "full_job_name": code.upper().replace("-", " "),
         "mc_status": st, "company_id": "co-1"} for code, (pid, st, n) in ws.items()
    ]
    client.store["companies"] = [{"id": "co-1", "name": "Salesloft"}]
    return tmp_path, client


def _reconcile(tmp_path):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        import reconcile_sprint_asks as rec
    finally:
        sys.path.remove(str(scripts))
    tenant, client = _reconcile_tenant(tmp_path)
    actions, imports, _ = rec.reconcile(tenant, client, TODAY)
    return actions, imports, client


def test_reconcile_imports_holding_open_and_closed_expired(tmp_path):
    _actions, imports, _client = _reconcile(tmp_path)
    by_ws = {r["description"].rsplit(" ", 1)[-1]: r["status"] for r in imports
             if r["description"].startswith("Confirm the venue")}
    assert by_ws == {
        "slt-5196-brand-campaign-26": "open",
        "slt-5197-holding-job": "open",      # paused, not over
        "slt-5198-closed-job": "expired",    # nobody chases a closed job
    }


def test_reconcile_skips_a_likely_duplicate_and_names_its_twin(tmp_path):
    actions, imports, client = _reconcile(tmp_path)
    twin = client.commitments[0]
    assert not any("CAB meeting agenda" in r["description"] for r in imports)
    (skip,) = [a for a in actions if a["action"] == "skip_likely_duplicate"]
    assert skip["commitment_id"] == twin["id"]
    assert skip["matched_text"] == "Send CAB agenda to David Schloss"


def test_prep_says_asks_are_unreadable_when_the_mc2_read_fails(tmp_path):
    """Step 3 + 4a: a failed commitments read is shown, and the sprint
    file's rendered asks are the fallback — not skipped as if MC-2 had
    answered. Control: the pre-amendment prep printed `(none)` silently."""
    from datetime import datetime

    from cp_engine import SyncConfig, TenantConfig
    from cp_engine.prep_planning import build_planning_result, render_planning_bundle
    from cp_engine.sprints import current_sprint_week_iso

    class _NoCommitments(FakeMC2):
        def table(self, name):
            if name == "commitments":
                raise RuntimeError("db down")
            return super().table(name)

    config = TenantConfig(
        name="t", display="T", engine_version_constraint="~= 0.1",
        sync=SyncConfig(backend="mc-2", cron="0 * * * *", mc_2_supabase_project_ref="ref"),
        projects=(), root=tmp_path,
    )
    week = current_sprint_week_iso(datetime(2026, 9, 30))
    path = _render(tmp_path, week, None)
    body = path.read_text().replace(
        "- _Not yet rendered from MC-2._",
        "- [open · 2026-09-22 · Morgan Wright] Book travel for the nine "
        "participants <!-- cp:hash=b00c7a1e -->",
    )
    path.write_text(body)
    result = build_planning_result(config, (_project(),), today=date(2026, 9, 30),
                                   supabase_client=_NoCommitments())
    out = render_planning_bundle(result)
    assert "⚠️ asks unreadable" in out
    assert any("asks unreadable" in e for e in result.errors)
    assert "Book travel for the nine participants" in out
