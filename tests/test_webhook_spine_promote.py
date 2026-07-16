"""Tests for POST /api/spine/promote (spine estimate-binding, Task 4.a; async).

The mc-2 web UI's "Frame & promote" click lands a new live substance version
directly in the cp tenant git repo. mc-2 has the DB but no checkout of the
tenant filesystem, so the markdown write + git push happen HERE (the webhook
clones the tenant per-run). The endpoint is 202-then-background, mirroring
/api/spine/promote-transcript: signature-verify → load card → 409 guard →
resolve item id → insert a `running` spine_promote_runs row
(kind='frame_promote', card_id=<card>) → 202 {run_id, status}. The background
task does the estimate resolve → sparse clone → promote_card (directed
re-distill, write markdown) → mirror to spine_substance → commit + push →
card flip, then records done/failed (+ `result` jsonb) on the run row.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
from pathlib import Path
from contextlib import contextmanager

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

from fastapi.testclient import TestClient

import main as webhook_main
import git_ops
import pipeline

from cp_engine.estimate import Estimate, EstimateItem, EstimatePhase
from cp_engine.spine_inbox import InboxCard


def _signed(body: bytes, *, secret: bytes = b"test-secret") -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


@pytest.fixture
def client() -> TestClient:
    return TestClient(webhook_main.app)


def _post(client: TestClient, payload: dict):
    body = json.dumps(payload).encode()
    return client.post(
        "/api/spine/promote",
        content=body,
        headers={"x-webhook-signature": _signed(body)},
    )


def _card(**kw) -> InboxCard:
    base = dict(
        id="ibx-5153/inbox/mtg-1",
        project_id="u-123",
        project_code="ibx-5153",
        source_ref="mtg-1",
        raw_distillation="raw faithful body",
        guessed_est_item_id="d1",
        guessed_type="deliverable",
        status="proposed",
        framing=None,
    )
    base.update(kw)
    return InboxCard(**base)


def _estimate() -> Estimate:
    item = EstimateItem(
        id="d1", phase_id="ph0", kind="deliverable", name="Messaging system",
        short_description=None, position=0, library_item_id=None,
    )
    return Estimate(
        id="e1", mc_project_id="u-123", name="E1",
        phases=(EstimatePhase(id="ph0", name="Phase 0", overview=None,
                              position=0, items=(item,)),),
    )


class _RecordingClient:
    """Minimal Supabase-client stand-in recording the chains the endpoint +
    background runner use: `.insert(...).execute()` (the run row) and
    `.update(...).eq(...).execute()` (run-row done/failed + card flip).

    Inserts land in ``rec["inserts"]`` as {table, data}; executed updates in
    ``rec["db_updates"]`` as {table, update, eq}. ``rec["update_calls"]``
    counts `.update(...)` attempts. ``rec["update_raises_for"]`` — a
    ``(table_name, exception)`` pair — makes `.execute()` raise for updates
    against that ONE table (so a flip failure can be simulated without also
    breaking the run-row bookkeeping).
    """

    def __init__(self, rec: dict):
        self._rec = rec
        rec.setdefault("inserts", [])
        rec.setdefault("db_updates", [])
        rec.setdefault("update_calls", 0)

    def table(self, name):
        return _RecordingTable(self._rec, name)


class _RecordingTable:
    def __init__(self, rec, name):
        self._rec = rec
        self._name = name
        self._insert = None
        self._update = None
        self._eq = None

    def insert(self, data):
        self._rec["inserts"].append({"table": self._name, "data": data})
        self._insert = data
        return self

    def update(self, data):
        self._rec["update_calls"] += 1
        self._update = data
        return self

    def eq(self, col, val):
        self._eq = (col, val)
        return self

    def execute(self):
        if self._update is not None:
            raises_for = self._rec.get("update_raises_for")
            if raises_for is not None and raises_for[0] == self._name:
                raise raises_for[1]
            self._rec["db_updates"].append(
                {"table": self._name, "update": self._update, "eq": self._eq}
            )
        return type("Resp", (), {"data": []})()


def _wire_happy(monkeypatch, tmp_path: Path, *, card=None, estimate=None,
                promote_path=None, mirror=None, sha="deadbeef",
                version_label="v3", recorder=None):
    """Wire every collaborator the endpoint + background runner touch; return
    the recorder dict. By default spawned background coros are CAPTURED in
    ``rec["spawned"]`` (not run) — tests drive them with ``asyncio.run``.
    """
    rec = recorder if recorder is not None else {}
    card = card if card is not None else _card()
    estimate = estimate if estimate is not None else _estimate()

    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")

    # The background runner calls _cloned_tenant(sparse_paths=[...]) — record
    # the kwarg and yield the tmp tree, tracking enter/exit for cleanup proof.
    rec.setdefault("clone_calls", [])
    rec.setdefault("clone_enters", 0)
    rec.setdefault("clone_exits", 0)

    @contextmanager
    def _fake_clone(sparse_paths=None):
        rec["clone_calls"].append({"sparse_paths": sparse_paths})
        rec["clone_enters"] += 1
        try:
            yield tmp_path
        finally:
            rec["clone_exits"] += 1

    monkeypatch.setattr(git_ops, "_cloned_tenant", _fake_clone)

    sb = _RecordingClient(rec)
    monkeypatch.setattr(pipeline, "_create_supabase_client", lambda: sb)
    rec["client"] = sb

    cfg = type("Cfg", (), {"root": tmp_path})()
    monkeypatch.setattr(pipeline, "_load_tenant_config", lambda root: cfg)

    monkeypatch.setattr("cp_engine.spine_inbox.load_card",
                        lambda c, cid: card)
    monkeypatch.setattr("cp_engine.estimate.fetch_estimate",
                        lambda c, pid: estimate)
    monkeypatch.setattr("cp_engine.spine.find_spine_dir",
                        lambda root, code: tmp_path / "1p" / "infoblox" / code)

    if promote_path is None:
        promote_path = (tmp_path / "1p" / "infoblox" / "ibx-5153"
                        / "spine" / "phase-0" / "messaging-system.md")

    def fake_promote(card, **kw):
        rec["promote_kw"] = kw
        rec["promote_card"] = card
        promote_path.parent.mkdir(parents=True, exist_ok=True)
        promote_path.write_text("# stub substance\n")
        return promote_path
    monkeypatch.setattr("cp_engine.spine_inbox.promote_card", fake_promote)

    def fake_mirror(c, **kw):
        rec["mirror_kw"] = kw
        if mirror is not None:
            return mirror(c, **kw)
        return 1
    monkeypatch.setattr("cp_engine.spine_substance_sync.sync_spine_substance",
                        fake_mirror)

    fake_live = type("LV", (), {"label": version_label})()
    fake_item = type("WI", (), {"live_version": lambda self: fake_live})()
    monkeypatch.setattr("cp_engine.substance.parse_substance",
                        lambda p: fake_item)

    def fake_commit(*, tenant_root, project_code, version_label, rel_path):
        rec["commit_kw"] = dict(
            tenant_root=tenant_root, project_code=project_code,
            version_label=version_label, rel_path=rel_path,
        )
        return sha
    monkeypatch.setattr(git_ops, "_commit_and_push_promote", fake_commit)

    rec.setdefault("spawned", [])
    monkeypatch.setattr(pipeline, "_spawn_background",
                        lambda coro: rec["spawned"].append(coro))

    return rec


def _kickoff(client, rec, payload=None):
    """POST the payload, assert the 202 kickoff shape + run-row insert, and
    return (run_id, inserted_row, spawned_coro)."""
    resp = _post(client, payload or {
        "card_id": "ibx-5153/inbox/mtg-1", "framing": "the directing brief",
    })
    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert data["status"] == "running"
    run_id = data["run_id"]

    runs = [i for i in rec["inserts"] if i["table"] == "spine_promote_runs"]
    assert len(runs) == 1
    inserted = runs[0]["data"]
    assert inserted["id"] == run_id

    assert len(rec["spawned"]) == 1
    return run_id, inserted, rec["spawned"][0]


def _run_row_patch(rec, run_id):
    """The (single) spine_promote_runs update the background runner issued."""
    patches = [u for u in rec["db_updates"] if u["table"] == "spine_promote_runs"]
    assert len(patches) == 1
    assert patches[0]["eq"] == ("id", run_id)
    return patches[0]["update"]


# --------------------------------------------------------- happy path (202 + run)


def test_promote_202_inserts_running_run_row(monkeypatch, client, tmp_path):
    """The kickoff: 202 {run_id, status:'running'} + a spine_promote_runs row
    carrying kind='frame_promote' and card_id — BEFORE any clone/LLM work
    (the spawned coro is captured, never run, and nothing downstream fired)."""
    rec = _wire_happy(monkeypatch, tmp_path)
    run_id, inserted, coro = _kickoff(client, rec)

    assert run_id.startswith("ibx-5153/")
    assert inserted["project_id"] == "u-123"
    assert inserted["project_code"] == "ibx-5153"
    assert inserted["est_item_id"] == "d1"          # resolved binding target
    assert inserted["kind"] == "frame_promote"
    assert inserted["card_id"] == "ibx-5153/inbox/mtg-1"
    assert inserted["status"] == "running"
    assert "started_at" in inserted

    # Nothing heavy ran at kickoff time.
    assert rec["clone_enters"] == 0
    assert "promote_kw" not in rec
    assert "commit_kw" not in rec

    coro.close()  # captured, deliberately not driven in this test


def test_promote_background_completes_run_row_done(monkeypatch, client, tmp_path):
    """Drive the captured background coro: promote_card runs with the resolved
    item name/phase/kind + framing (flip_card=False, client passed), the mirror
    + commit fire, the card flips AFTER the push, and the run row lands done
    with the full result jsonb."""
    rec = _wire_happy(monkeypatch, tmp_path)
    run_id, _, coro = _kickoff(client, rec)

    asyncio.run(coro)

    # promote_card got the resolved item name/phase/kind + framing.
    kw = rec["promote_kw"]
    assert kw["framing"] == "the directing brief"
    assert kw["est_item_id"] == "d1"
    assert kw["kind"] == "deliverable"
    assert kw["name"] == "Messaging system"
    assert kw["phase"] == "Phase 0"
    assert kw["model"] == webhook_main.DEFAULT_MODEL
    assert kw["sources"] == ["mtg-1"]  # falls back to card.source_ref
    # C1: promote_card must NOT flip the card (flip_card=False). The flip is
    # deferred to the runner, AFTER a successful push. The client IS passed
    # so the issue-#44 create-don't-version path can write authored rows.
    assert kw["client"] is rec["client"]
    assert kw["flip_card"] is False

    # mirror + commit were both called.
    assert "mirror_kw" in rec
    assert rec["commit_kw"]["project_code"] == "ibx-5153"
    assert rec["commit_kw"]["version_label"] == "v3"

    # The card DID get flipped to 'promoted' — issued by the runner itself,
    # separately from promote_card.
    flips = [u for u in rec["db_updates"]
             if u["update"] == {"status": "promoted"}]
    assert len(flips) == 1
    assert flips[0]["table"] == "spine_inbox"
    assert flips[0]["eq"] == ("id", "ibx-5153/inbox/mtg-1")

    # Run row → done with the result jsonb the mc-2 poller renders.
    patch = _run_row_patch(rec, run_id)
    assert patch["status"] == "done"
    assert "finished_at" in patch
    assert patch["result"] == {
        "version_label": "v3",
        "rel_path": "1p/infoblox/ibx-5153/spine/phase-0/messaging-system.md",
        "mirrored": True,
        "mirror_skipped": [],
        "created_new_element": False,
        "card_flipped": True,
    }

    # Clone lifecycle: entered once, exited once (no per-promote disk leak).
    assert rec["clone_enters"] == 1
    assert rec["clone_exits"] == 1


def test_promote_background_clone_is_sparse_scope_dirs(monkeypatch, client, tmp_path):
    """The background clone is sparse, scoped to exactly the engine's scope
    dirs (`_SCOPE_DIRS`) — the set find_spine_dir walks and promote writes
    under. Root files (.cp-engine.toml) come free via cone mode."""
    from cp_engine.sync import _SCOPE_DIRS

    rec = _wire_happy(monkeypatch, tmp_path)
    _, _, coro = _kickoff(client, rec)
    asyncio.run(coro)

    assert rec["clone_calls"] == [{"sparse_paths": list(_SCOPE_DIRS)}]


def test_promote_created_new_element_true_for_authored_path(
    monkeypatch, client, tmp_path
):
    """When promote_card takes the issue-#44 create path (returns a mirror
    under spine/_authored/), the result records created_new_element=True."""
    authored = (tmp_path / "1p" / "infoblox" / "ibx-5153"
                / "spine" / "_authored" / "interview-with-paul-wu.md")
    rec = _wire_happy(monkeypatch, tmp_path, promote_path=authored,
                      version_label="v1")
    run_id, _, coro = _kickoff(client, rec)
    asyncio.run(coro)

    patch = _run_row_patch(rec, run_id)
    assert patch["status"] == "done"
    assert patch["result"]["created_new_element"] is True
    assert patch["result"]["version_label"] == "v1"
    assert patch["result"]["rel_path"] == (
        "1p/infoblox/ibx-5153/spine/_authored/interview-with-paul-wu.md"
    )


# ---------------------------------------------------------------- 404


def test_promote_404_when_card_missing(monkeypatch, client, tmp_path):
    rec = _wire_happy(monkeypatch, tmp_path)
    monkeypatch.setattr("cp_engine.spine_inbox.load_card", lambda c, cid: None)
    resp = _post(client, {"card_id": "ibx-5153/inbox/nope", "framing": "x"})
    assert resp.status_code == 404
    assert "no inbox card" in resp.text
    assert rec["inserts"] == []
    assert rec["spawned"] == []


# ---------------------------------------------------------------- 409


def test_promote_409_when_card_already_promoted(monkeypatch, client, tmp_path):
    """Double-submit guard (issue #44): the same card promoted twice wrote two
    near-duplicate versions 17s apart. An already-'promoted' card must 409 —
    no run row insert, no background spawn, no re-distill, no card flip.
    Re-framing the card (which resets its status) is the deliberate path to
    promote again."""
    rec = _wire_happy(monkeypatch, tmp_path, card=_card(status="promoted"))
    resp = _post(client, {
        "card_id": "ibx-5153/inbox/mtg-1", "framing": "brief",
    })
    assert resp.status_code == 409
    assert "already promoted" in resp.text
    # Nothing downstream ran: no run row, no spawn, no promote, no DB update.
    assert rec["inserts"] == []
    assert rec["spawned"] == []
    assert "promote_kw" not in rec
    assert "mirror_kw" not in rec
    assert "commit_kw" not in rec
    assert rec["update_calls"] == 0


# ---------------------------------------------------------------- 400s


def test_promote_400_when_framing_empty(monkeypatch, client, tmp_path):
    rec = _wire_happy(monkeypatch, tmp_path)
    resp = _post(client, {"card_id": "ibx-5153/inbox/mtg-1", "framing": "   "})
    assert resp.status_code == 400
    assert "framing" in resp.text.lower()
    assert rec["inserts"] == []
    assert rec["spawned"] == []


def test_promote_400_when_no_est_item_id(monkeypatch, client, tmp_path):
    # Card carries no guess and none is passed → nothing to bind to.
    rec = _wire_happy(monkeypatch, tmp_path, card=_card(guessed_est_item_id=None))
    resp = _post(client, {
        "card_id": "ibx-5153/inbox/mtg-1", "framing": "brief",
    })
    assert resp.status_code == 400
    assert "estimate item" in resp.text.lower()
    assert rec["inserts"] == []
    assert rec["spawned"] == []


# ---------------------------------------------------------------- 401


def test_promote_401_on_bad_signature(monkeypatch, client, tmp_path):
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    body = json.dumps({"card_id": "x", "framing": "y"}).encode()
    resp = client.post(
        "/api/spine/promote",
        content=body,
        headers={"x-webhook-signature": "deadbeef"},
    )
    assert resp.status_code == 401


def test_promote_401_when_signature_missing(monkeypatch, client, tmp_path):
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    body = json.dumps({"card_id": "x", "framing": "y"}).encode()
    resp = client.post("/api/spine/promote", content=body)
    assert resp.status_code == 401


# ---------------------------------------------------------------- 500


def test_promote_500_when_supabase_unconfigured(monkeypatch, client, tmp_path):
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.setattr(pipeline, "_create_supabase_client", lambda: None)
    resp = _post(client, {"card_id": "x/inbox/y", "framing": "brief"})
    assert resp.status_code == 500
    assert "supabase" in resp.text.lower()


# ---------------------------------------------------------------- mirror fail


def test_promote_mirror_failure_still_done(monkeypatch, client, tmp_path):
    def boom(c, **kw):
        raise RuntimeError("supabase down")
    rec = _wire_happy(monkeypatch, tmp_path, mirror=boom)
    run_id, _, coro = _kickoff(client, rec, {
        "card_id": "ibx-5153/inbox/mtg-1", "framing": "brief",
    })
    asyncio.run(coro)

    patch = _run_row_patch(rec, run_id)
    assert patch["status"] == "done"  # push already succeeded
    assert patch["result"]["mirrored"] is False
    assert patch["result"]["card_flipped"] is True  # flip independent of mirror


# ---------------------------------------------------------------- defaults


def test_promote_defaults_resolution(monkeypatch, client, tmp_path):
    """est_item_id falls back to card guess; sources fall back to
    [card.source_ref]; model defaults to DEFAULT_MODEL; kind defaults to the
    estimate item's kind."""
    rec = _wire_happy(monkeypatch, tmp_path)
    _, _, coro = _kickoff(client, rec, {
        "card_id": "ibx-5153/inbox/mtg-1", "framing": "brief",
    })
    asyncio.run(coro)
    kw = rec["promote_kw"]
    assert kw["est_item_id"] == "d1"            # card.guessed_est_item_id
    assert kw["sources"] == ["mtg-1"]           # [card.source_ref]
    assert kw["model"] == "claude-opus-4-7"     # DEFAULT_MODEL
    assert kw["kind"] == "deliverable"          # item.kind


def test_promote_explicit_overrides(monkeypatch, client, tmp_path):
    """Passed est_item_id / kind / sources / model override the defaults."""
    rec = _wire_happy(monkeypatch, tmp_path)
    _, _, coro = _kickoff(client, rec, {
        "card_id": "ibx-5153/inbox/mtg-1",
        "framing": "brief",
        "est_item_id": "d1",
        "kind": "activity",
        "sources": ["doc-a", "doc-b"],
        "model": "claude-opus-4-8",
    })
    asyncio.run(coro)
    kw = rec["promote_kw"]
    assert kw["est_item_id"] == "d1"
    assert kw["kind"] == "activity"
    assert kw["sources"] == ["doc-a", "doc-b"]
    assert kw["model"] == "claude-opus-4-8"


def test_promote_estimate_unreachable_degrades(monkeypatch, client, tmp_path):
    """If the estimator schema is unreachable, the promote still proceeds
    unbound-by-name (name/phase None, kind falls back to 'deliverable') and
    the run still completes done."""
    rec = _wire_happy(monkeypatch, tmp_path)

    def boom(c, pid):
        raise RuntimeError("estimator down")
    monkeypatch.setattr("cp_engine.estimate.fetch_estimate", boom)

    run_id, _, coro = _kickoff(client, rec, {
        "card_id": "ibx-5153/inbox/mtg-1", "framing": "brief",
    })
    asyncio.run(coro)
    kw = rec["promote_kw"]
    assert kw["name"] is None
    assert kw["phase"] is None
    assert kw["kind"] == "deliverable"  # resolved_kind fallback
    assert _run_row_patch(rec, run_id)["status"] == "done"


# -------------------------------------------------- C1/C2: push-failure ordering


def test_promote_push_failure_records_failed_and_never_flips(
    monkeypatch, client, tmp_path
):
    """If the commit/push fails, the run row records FAILED (with the error
    string) AND the card is NEVER flipped to 'promoted' — the core C1
    guarantee (no silent data loss). The throwaway clone is discarded (exit
    ran), but the card stays proposed/framed so the human can safely retry.
    """
    rec = _wire_happy(monkeypatch, tmp_path)

    def boom(**kw):
        raise RuntimeError("git push rejected")
    monkeypatch.setattr(git_ops, "_commit_and_push_promote", boom)

    run_id, _, coro = _kickoff(client, rec, {
        "card_id": "ibx-5153/inbox/mtg-1", "framing": "brief",
    })
    asyncio.run(coro)  # never raises — failure is recorded on the run row

    patch = _run_row_patch(rec, run_id)
    assert patch["status"] == "failed"
    assert "git push rejected" in patch["error"]
    assert "finished_at" in patch
    # The card was NOT flipped — the runner never reached the post-push flip.
    flips = [u for u in rec["db_updates"] if u["table"] == "spine_inbox"]
    assert flips == []
    # Clone cleanup ran on the failure path too.
    assert rec["clone_exits"] == 1


def test_promote_flip_failure_after_push_still_done(monkeypatch, client, tmp_path):
    """The benign residual window: push SUCCEEDED but the final card flip
    raises. The version is durable in the repo, so the run records done with
    card_flipped=false (never failed — that would falsely tell the UI the
    durable write was lost).
    """
    rec = _wire_happy(monkeypatch, tmp_path)
    rec["update_raises_for"] = ("spine_inbox",
                                RuntimeError("supabase update rejected"))

    run_id, _, coro = _kickoff(client, rec, {
        "card_id": "ibx-5153/inbox/mtg-1", "framing": "brief",
    })
    asyncio.run(coro)

    patch = _run_row_patch(rec, run_id)
    assert patch["status"] == "done"
    assert patch["result"]["card_flipped"] is False
    # The flip WAS attempted (it raised) but no spine_inbox update landed.
    assert [u for u in rec["db_updates"] if u["table"] == "spine_inbox"] == []


# ------------------------------------------------- I1: real render→parse round-trip


def test_promote_real_round_trip(monkeypatch, client, tmp_path):
    """Integration-flavored: run the REAL promote_card + REAL parse_substance /
    render_substance end-to-end against a tmp dir, stubbing ONLY the distiller
    (so no Claude call) and the git/clone + supabase client. Proves the markdown
    that promote_card writes re-parses and yields the right version label (v1 for
    a fresh item) — guarding the render/parse interplay the other tests stub out.
    """
    from cp_engine.spine_inbox import promote_card
    from cp_engine.substance import parse_substance

    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")

    project_dir = tmp_path / "1p" / "infoblox" / "ibx-5153"
    card = _card()

    path = promote_card(
        card,
        framing="the directing brief",
        est_item_id="d1",
        kind="deliverable",
        project_dir=project_dir,
        sources=["mtg-1"],
        distiller=lambda prompt, model, api_key=None: "Distilled body text.",
        model="claude-opus-4-7",
        client=None,  # no flip — matches the endpoint's C1 contract
        name="Messaging system",
        phase="Phase 0",
    )

    assert path.exists()
    parsed = parse_substance(path)
    live = parsed.live_version()
    assert live.label == "v1"           # fresh item → v1
    assert live.status == "live"
    assert parsed.est_item_id == "d1"
    assert "Distilled body text." in live.body
    # Layer stamps from the card's kind so the mirrored row never lands NULL.
    assert parsed.layer == "Deliverables"


def test_promote_backfills_layer_on_existing_unstamped_file(tmp_path):
    """A file promoted before layer stamping existed has no `layer` in its
    frontmatter. Promoting into it again (the add_version path) stamps the
    layer from the card's kind instead of leaving it NULL forever.
    """
    from cp_engine.spine_inbox import promote_card
    from cp_engine.substance import parse_substance

    project_dir = tmp_path / "1p" / "infoblox" / "ibx-5153"
    card = _card()

    common = dict(
        est_item_id="a7",
        kind="activity",
        project_dir=project_dir,
        sources=["mtg-1"],
        distiller=lambda prompt, model, api_key=None: "Distilled body text.",
        model="claude-opus-4-7",
        client=None,
        name="Kickoff workshop",
        phase="Phase 1",
    )

    path = promote_card(card, framing="first framing", **common)
    # Simulate a pre-stamping file: strip the layer line from the frontmatter.
    stripped = "\n".join(
        line for line in path.read_text().splitlines() if not line.startswith("layer:")
    )
    path.write_text(stripped + "\n")
    assert parse_substance(path).layer is None

    promote_card(card, framing="second framing", **common)
    parsed = parse_substance(path)
    assert parsed.layer == "Activity"
    assert parsed.live_version().label == "v2"


# ------------------------------------------------- git_ops: sparse clone plumbing


def _record_subprocess(monkeypatch):
    """Replace git_ops.subprocess.run with a recorder; returns the call list."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append({"cmd": list(cmd), "kw": kw})
        return type("CP", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)
    return calls


def test_cloned_tenant_sparse_args(monkeypatch):
    """sparse_paths → partial (--filter=blob:none --sparse) clone followed by
    `git sparse-checkout set <paths...>` inside the clone. No real network."""
    monkeypatch.setenv("CP_TENANT_REPO_URL", "git@example.com:t/cp.git")
    monkeypatch.delenv("GIT_SSH_KEY", raising=False)
    calls = _record_subprocess(monkeypatch)

    with git_ops._cloned_tenant(sparse_paths=["1p", "firstpersonsf", "canonic"]) as root:
        assert root.name == "cp"

    clone = calls[0]["cmd"]
    assert clone[:2] == ["git", "clone"]
    assert "--depth=10" in clone
    assert "--filter=blob:none" in clone
    assert "--sparse" in clone
    assert clone[-2:] == ["git@example.com:t/cp.git", str(root)]

    sparse = calls[1]["cmd"]
    assert sparse == ["git", "sparse-checkout", "set",
                      "1p", "firstpersonsf", "canonic"]
    assert calls[1]["kw"]["cwd"] == root


def test_cloned_tenant_default_not_sparse(monkeypatch):
    """No sparse_paths → the historic full shallow clone, no sparse flags,
    no sparse-checkout invocation."""
    monkeypatch.setenv("CP_TENANT_REPO_URL", "git@example.com:t/cp.git")
    monkeypatch.delenv("GIT_SSH_KEY", raising=False)
    calls = _record_subprocess(monkeypatch)

    with git_ops._cloned_tenant() as root:
        pass

    clone = calls[0]["cmd"]
    assert "--depth=10" in clone
    assert "--filter=blob:none" not in clone
    assert "--sparse" not in clone
    assert not any("sparse-checkout" in c["cmd"] for c in calls)
