"""Architecture step 4c: MC-2 owns every spine element; ``spine/`` is a
generated, guarded view.

The four CONTROL tests at the top were run against the pre-4c engine
(origin/main b872445) and FAIL there — see each docstring for how:

* a hand edit in ``spine/`` no longer reaches MC-2, and is quarantined;
* meeting-history writes land at ``<workstream>/meeting-history.md``;
* a hand-written card file imports into MC-2 exactly once;
* a snapshot is keyed by the workstream's full code.

The rest pin the guard's other branches.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

from tests._spine_fake import FakeClient, substance_row

CODE = "slt-5196-brand-campaign-26"

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))


def _tenant(tmp_path: Path) -> Path:
    (tmp_path / ".cp-engine.toml").write_text("[tenant]\nname = 't'\n")
    proj = tmp_path / "1p" / "salesloft" / CODE
    proj.mkdir(parents=True)
    return proj


_DISTILLED_FILE = (
    "---\n"
    "est_item_id: item-1\n"
    "est_item_kind: deliverable\n"
    "phase: Pre-production\n"
    "binding: live\n"
    "layer: Activity\n"
    "---\n"
    "## v1 — 2026-09-01 · live\n"
    "framing: MC-2 framing\n"
    "sources:\n"
    "\n"
    "MC-2 body\n"
)


def _distilled_row(**kw):
    base = dict(origin="distilled", framing="MC-2 framing", body="MC-2 body",
                layer="Activity", kind="deliverable", phase="Pre-production",
                binding="live", placement="item",
                rel_path="spine/pre-production/plan-production.md")
    base.update(kw)
    return substance_row(CODE, "item-1", **base)


# ── CONTROL 1 ────────────────────────────────────────────────────────────────


def test_control_hand_edit_never_reaches_mc2_and_is_quarantined(tmp_path):
    """Pre-4c, the second sync pushed the edited body INTO MC-2 (an
    unconfirmed field is disk-wins) — the MC-2 body assertion fails there.
    Now MC-2 is untouched, the file is re-rendered from MC-2, and the edit is
    preserved under exceptions/region-edits/ with a loud warning."""
    from cp_engine.spine_substance_sync import sync_spine_substance

    proj = _tenant(tmp_path)
    f = proj / "spine" / "pre-production" / "plan-production.md"
    f.parent.mkdir(parents=True)
    f.write_text(_DISTILLED_FILE)
    client = FakeClient(spine_substance=[_distilled_row()], projects=[])
    kw = dict(project_id="pid", project_code=CODE, project_dir=proj)

    sync_spine_substance(client, **kw)             # the engine's own write
    rendered = f.read_text()
    f.write_text(rendered.replace("MC-2 body", "HAND-EDITED body"))
    sync_spine_substance(client, **kw)

    (row,) = client.store["spine_substance"]
    assert row["body"] == "MC-2 body"
    assert row["framing"] == "MC-2 framing"
    assert "HAND-EDITED" not in f.read_text()
    q = list((tmp_path / "exceptions" / "region-edits").glob("*.md"))
    assert len(q) == 1, q
    assert "HAND-EDITED body" in q[0].read_text()


# ── CONTROL 2 ────────────────────────────────────────────────────────────────


def _config_for(tenant: Path):
    from cp_engine.config import SyncConfig, TenantConfig

    return TenantConfig(
        name="cp", display="cp", engine_version_constraint="~= 0.21",
        sync=SyncConfig(backend="mc-2", cron="0 * * * *",
                        mc_2_supabase_project_ref="ref"),
        projects=(), root=tenant,
    )


def _meeting(mid="m-1", summary="Discussed the shoot."):
    return {"id": mid, "title": "Logistics", "meeting_date": "2026-09-30",
            "summary": summary, "participants": ["Morgan Wright"]}


def test_control_meeting_history_lands_at_workstream_root(tmp_path):
    """Pre-4c the webhook wrote spine/Retrospective/meeting-history.md (inside
    the generated view) — the first assertion fails there."""
    import main as webhook_main

    proj = tmp_path / "firstpersonsf" / "ggl-5168"
    proj.mkdir(parents=True)
    status = webhook_main._append_retrospective(
        config=_config_for(tmp_path), code="ggl-5168", meeting=_meeting(),
        action_items=None, plan=None,
    )
    assert status == "appended"
    assert (proj / "meeting-history.md").is_file()
    assert "Discussed the shoot." in (proj / "meeting-history.md").read_text()
    assert not (proj / "spine").exists()


# ── CONTROL 3 ────────────────────────────────────────────────────────────────


_CARD = (
    "---\n"
    "est_item_id: _authored/morgan-wright-salesloft-customer-liaison\n"
    "est_item_kind: context\n"
    "binding: unbound\n"
    "layer: Stakeholders\n"
    "placement: context\n"
    "steps:\n"
    "- position: 1\n"
    "  title: Created Morgan Wright\n"
    "  status: done\n"
    "  date: '2026-08-28'\n"
    "  note: null\n"
    "---\n"
    "## v1 — 2026-08-28 · live\n"
    "framing: Morgan Wright — Salesloft (customer advocacy)\n"
    "sources:\n"
    "\n"
    "_Stakeholder dossier, v1._ She owns recruitment.\n"
)


def test_control_hand_written_card_imports_once(tmp_path):
    """Pre-4c there is no import path (`cp_engine.spine_import` does not
    exist) and sync's disk reader skips `_authored/`, so MC-2 never sees the
    card. Now it imports as an authored element — once."""
    from cp_engine.spine_import import find_handwritten, import_card

    proj = _tenant(tmp_path)
    card = proj / "spine" / "_authored" / "morgan-wright-salesloft-customer-liaison.md"
    card.parent.mkdir(parents=True)
    card.write_text(_CARD)
    client = FakeClient(spine_substance=[], spine_steps=[])

    (hw,) = find_handwritten(client, project_id="pid", project_dir=proj)
    assert hw.kind == "card" and not hw.already_in_mc2
    assert import_card(client, hw, project_id="pid", project_code=CODE) == "imported"
    (row,) = client.store["spine_substance"]
    assert row["id"] == f"{CODE}/_authored/morgan-wright-salesloft-customer-liaison/v1"
    assert row["origin"] == "authored" and row["layer"] == "Stakeholders"
    assert "She owns recruitment." in row["body"]
    assert row["version_date"] == "2026-08-28"
    assert [s["title"] for s in client.store["spine_steps"]] == ["Created Morgan Wright"]

    # Twice: nothing new.
    assert import_card(client, hw, project_id="pid", project_code=CODE) == "already in MC-2"
    assert len(client.store["spine_substance"]) == 1
    assert len(client.store["spine_steps"]) == 1
    # And it is no longer hand-written: the render now produces that file.
    assert find_handwritten(client, project_id="pid", project_dir=proj) == []


# ── CONTROL 4 ────────────────────────────────────────────────────────────────


def test_control_snapshot_keyed_by_full_code(tmp_path):
    """The one snapshot on disk was frozen under the short code; pre-4c its
    index row id was `ibx-5153/…` while MC-2 keys it `ibx-5153-ai-campaign/…`
    (the id assertion fails there)."""
    from cp_engine.spine_sync import sync_spine_snapshots

    full = "ibx-5153-ai-campaign"
    proj = tmp_path / "1p" / "infoblox" / full
    snap = proj / "spine" / "Deliverables" / "foundation-pp-doc.snapshots"
    snap.mkdir(parents=True)
    (snap / "2026-06-13-before-6-17-workshop.md").write_text(
        "---\nid: ibx-5153/deliverable/foundation-pp-doc\n"
        "snapshot:\n  of: ibx-5153/deliverable/foundation-pp-doc\n"
        "  label: before 6-17 workshop\n  created: '2026-06-13'\n---\nbody\n"
    )
    client = FakeClient(spine_snapshots=[])
    sync_spine_snapshots(client, project_code=full, project_dir=proj,
                         tenant_root=tmp_path, project_id="pid")
    (row,) = client.store["spine_snapshots"]
    assert row["id"] == f"{full}/deliverable/foundation-pp-doc@2026-06-13-before-6-17-workshop"
    assert row["deliverable_id"] == f"{full}/deliverable/foundation-pp-doc"
    assert row["project_code"] == full


# ── the guard's other branches ───────────────────────────────────────────────


def _render(client, proj, **kw):
    from cp_engine.spine_mirror import render_project_spine

    return render_project_spine(client, project_id="pid", project_code=CODE,
                                project_dir=proj, **kw)


def _quarantine(tmp_path):
    d = tmp_path / "exceptions" / "region-edits"
    return sorted(d.glob("*.md")) if d.is_dir() else []


def test_unstamped_file_the_render_produces_is_overwritten_quietly(tmp_path):
    """First render after 4c: a file with no manifest entry that MC-2 renders
    (the nine stale-framing files) is replaced, not quarantined."""
    proj = _tenant(tmp_path)
    f = proj / "spine" / "pre-production" / "plan-production.md"
    f.parent.mkdir(parents=True)
    f.write_text(_DISTILLED_FILE.replace("MC-2 framing", "older framing"))
    client = FakeClient(spine_substance=[_distilled_row()])
    assert _render(client, proj) == 1
    assert "framing: MC-2 framing" in f.read_text()
    assert _quarantine(tmp_path) == []
    manifest = json.loads((proj / "spine" / ".generated.json").read_text())
    assert list(manifest) == ["pre-production/plan-production.md"]


def test_unstamped_file_mc2_does_not_render_is_quarantined_then_removed(tmp_path):
    proj = _tenant(tmp_path)
    stray = proj / "spine" / "_authored" / "nobody-imported-me.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("hand-written notes\n")
    _render(FakeClient(spine_substance=[]), proj)
    assert not stray.exists()
    (q,) = _quarantine(tmp_path)
    assert "hand-written notes" in q.read_text()


def test_retired_element_written_by_engine_is_reaped_silently(tmp_path):
    proj = _tenant(tmp_path)
    client = FakeClient(spine_substance=[
        substance_row(CODE, "_authored/brief", layer="Brief")])
    _render(client, proj)
    f = proj / "spine" / "_authored" / "brief.md"
    assert f.is_file()
    client.store["spine_substance"][0]["archived"] = True
    _render(client, proj)
    assert not f.exists()
    assert _quarantine(tmp_path) == []
    assert not (proj / "spine" / ".generated.json").exists()


def test_mc2_moving_an_engine_written_file_is_not_a_hand_edit(tmp_path):
    proj = _tenant(tmp_path)
    client = FakeClient(spine_substance=[
        substance_row(CODE, "_authored/brief", body="first")])
    _render(client, proj)
    client.store["spine_substance"][0]["body"] = "second"
    _render(client, proj)
    assert "second" in (proj / "spine" / "_authored" / "brief.md").read_text()
    assert _quarantine(tmp_path) == []


def test_exempt_families_are_never_generated_or_reaped(tmp_path):
    """Frozen snapshots and the not-yet-moved legacy meeting history."""
    proj = _tenant(tmp_path)
    legacy = proj / "spine" / "Retrospective" / "meeting-history.md"
    snap = proj / "spine" / "Deliverables" / "x.snapshots" / "2026-06-13-s.md"
    for f in (legacy, snap):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("keep me\n")
    _render(FakeClient(spine_substance=[]), proj)
    assert legacy.read_text() == "keep me\n"
    assert snap.read_text() == "keep me\n"
    assert _quarantine(tmp_path) == []


def test_read_failure_touches_nothing(tmp_path):
    """An unreadable MC-2 must never be read as 'every element was deleted'."""
    proj = _tenant(tmp_path)
    f = proj / "spine" / "_authored" / "brief.md"
    f.parent.mkdir(parents=True)
    f.write_text("last good copy\n")
    client = FakeClient(spine_substance=[])
    client.fail["select"] = {"spine_substance"}
    with pytest.raises(RuntimeError):
        _render(client, proj)
    assert f.read_text() == "last good copy\n"


def test_an_element_that_cannot_render_keeps_its_file(tmp_path):
    proj = _tenant(tmp_path)
    f = proj / "spine" / "_authored" / "two-live.md"
    f.parent.mkdir(parents=True)
    f.write_text("last good copy\n")
    client = FakeClient(spine_substance=[
        substance_row(CODE, "_authored/two-live", "v1"),
        substance_row(CODE, "_authored/two-live", "v2"),   # both live
    ])
    warnings: list[str] = []
    assert _render(client, proj, warnings_out=warnings) == 0
    assert f.read_text() == "last good copy\n"
    assert any("2 live versions" in w for w in warnings)


def test_account_scope_rows_render_under_stakeholders_not_spine(tmp_path):
    from cp_engine.spine_substance_sync import sync_spine_substance

    proj = _tenant(tmp_path)
    stale = proj / "spine" / "_authored" / "rina.md"
    stale.parent.mkdir(parents=True)
    client = FakeClient(
        spine_substance=[substance_row(CODE, "_authored/rina", scope="account",
                                       layer="Stakeholders", company_id="co")],
        projects=[{"id": "pid", "company_id": "co"}],
    )
    sync_spine_substance(client, project_id="pid", project_code=CODE,
                         project_dir=proj)
    assert (proj.parent / "_stakeholders" / "rina.md").is_file()
    assert not stale.exists()


def test_render_is_deterministic_whatever_the_row_order(tmp_path):
    from cp_engine.authored_mirror import render_element

    rows = [substance_row(CODE, "_authored/x", f"v{n}",
                          status="live" if n == 3 else "superseded",
                          sources=[{"id": "a", "title": "T: colon", "type": "rag_asset"}],
                          serves=["s1"])
            for n in (1, 2, 3)]
    texts = set()
    for seed in range(5):
        random.Random(seed).shuffle(rows)
        texts.add(render_element(est_item_id="_authored/x", rows=list(rows),
                                 kind="context"))
    assert len(texts) == 1
    # #216: a mapping source keeps its continuation lines indented.
    assert "  - id: a\n    title: 'T: colon'\n    type: rag_asset" in texts.pop()


def test_bindings_reconcile_in_mc2_not_from_disk():
    from cp_engine.spine_substance_sync import reconcile_bindings_in_mc2

    class _Est:
        def item_by_id(self, i):
            return object() if i == "here" else None

    client = FakeClient(spine_substance=[
        substance_row(CODE, "here", origin="distilled", binding="orphaned"),
        substance_row(CODE, "gone", origin="distilled", binding="live"),
        substance_row(CODE, "_authored/a", binding="unbound"),
    ])
    assert reconcile_bindings_in_mc2(client, project_id="pid", estimate=_Est()) == 2
    by = {r["est_item_id"]: r for r in client.store["spine_substance"]}
    assert by["here"]["binding"] == "live"
    assert by["gone"]["binding"] == "orphaned"
    assert by["gone"]["review_flags"][0]["source"] == "binding"
    assert by["_authored/a"]["binding"] == "unbound"
    # estimate=None changes nothing (an outage is not "everything unbound").
    assert reconcile_bindings_in_mc2(client, project_id="pid", estimate=None) == 0
    # Re-running is a no-op (no fresh flag timestamp per sync).
    assert reconcile_bindings_in_mc2(client, project_id="pid", estimate=_Est()) == 0


# ── meeting history: one resolver ────────────────────────────────────────────


def test_history_path_keeps_appending_to_the_legacy_file_until_moved(tmp_path):
    from cp_engine.retrospective import history_path

    proj = tmp_path / "ws"
    legacy = proj / "spine" / "Retrospective" / "meeting-history.md"
    assert history_path(proj) == proj / "meeting-history.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("old history\n")
    assert history_path(proj) == legacy             # no second file
    (proj / "meeting-history.md").write_text("moved\n")
    assert history_path(proj) == proj / "meeting-history.md"


def test_sweep_reads_meeting_history_from_the_new_path(tmp_path):
    from cp_engine.clock import tenant_today
    from cp_engine.spine import SpineElement
    from cp_engine.spine_sweep import run_sweep

    (tmp_path / ".cp-engine.toml").write_text("[tenant]\nname = 't'\n")
    proj = tmp_path / "1p" / "acct" / "ibx-5153"
    proj.mkdir(parents=True)
    (proj / "cp.md").write_text("x\n")
    (proj / "meeting-history.md").write_text(
        "---\nid: x\n---\n\n# Meeting history\n\n"
        "### 2026-09-30 · Feedback (Janet)\n\nNEW_PATH_SUMMARY\n"
    )
    el = SpineElement(id="ibx-5153/deliverable/d", project="ibx-5153",
                      layer="Deliverables", title="D", status="active",
                      last_touched="2026-09-30", path=Path("x"), body="b")
    seen = {}
    run_sweep("ibx-5153", (el,), today=tenant_today(),
              llm=lambda p: seen.setdefault("p", p) and "ok",
              tenant_root=tmp_path)
    assert "NEW_PATH_SUMMARY" in seen["p"]
