"""One meeting, end to end: Fathom → webhook auto-ingest → tenant commit →
sync → hosted MCP read (architecture plan step 6).

WHY ONE TEST ACROSS FOUR DEPLOYABLES. Every defect in this class shipped with
green unit tests on each side of a boundary, because each test read back
where it wrote:

- #194 — the webhook recorded ``success`` while 1,375 bullets were dropped;
- #263 — an account summary written INSIDE a managed region, eaten by the
  next sync (39 times, 18 runs) before anyone noticed;
- #345 — a short-code tag resolved on one surface and not the next;
- W40/W41 — on Wed 2026-09-30 the hosted server said W40 while the engine
  and the tree said W41, so ``get_project_state`` served last week's file.

So this drives the REAL pieces, in order, against one fake MC-2 and one real
git tenant, and asserts on what a person reading through the hosted server
would see:

    fathom_meetings row ──▶ POST /api/auto-ingest (signed, SHORT code)
      ──▶ generate_plan (LLM stubbed at the transport) ──▶ execute_plan
      ──▶ commit + push to a bare tenant repo
      ──▶ cp_engine.sync_tenant on a fresh clone (MC2Backend → the fake)
      ──▶ commit + push ──▶ hosted get_project_state / read_project_file /
          list_commitments / list_spine_elements (their own shallow clone)

WHAT IS FAKED, AND ONLY THIS: MC-2 (``tests/_spine_fake.FakeClient``, the
shared store-backed PostgREST fake — every module reaches it through
``mc2_db.get_client`` or the hosted ``user_client``), the Anthropic call
(``plan_from_transcript._call_claude``), and the tenant clock (frozen at
Tue 2026-09-29 23:30 Pacific = Wed 06:30 UTC, the instant a UTC clock rolls
the sprint week and the tenant's must not). Git, the webhook app, the HMAC
check, the engine's sync and the hosted tools are the production code.

The pipeline runs ONCE per module (``world``); each test pins one property
of the result, so a failure names the property that broke.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
# The webhook's dir FIRST: both deployables ship a top-level `observability`
# module, and the webhook's `import observability` must find its own (the
# hosted server loads its copy by path, so it is indifferent to the order).
if str(ROOT / "webhook") not in sys.path:
    sys.path.insert(0, str(ROOT / "webhook"))
if str(ROOT / "prototypes" / "hosted-mcp") not in sys.path:
    sys.path.append(str(ROOT / "prototypes" / "hosted-mcp"))

from tests._spine_fake import FakeClient  # noqa: E402

pytestmark = pytest.mark.e2e

SECRET = "e2e-secret"
SHORT = "ggl-5168"
FULL = "ggl-5168-activation"
ACCOUNT = "ggl-5216-google"
JOB_DIR = "1p/google/ggl-5168-activation"
CO_ID, ACC_ID, JOB_ID = "co-google", "p-acc-5216", "p-job-5168"
MEETING_ID = "11111111-aaaa-4bbb-8ccc-000000005168"

# Tue 2026-09-29 23:30 PDT == Wed 2026-09-30 06:30Z. The tenant's sprint week
# is W40 (Mon/Tue stay); a UTC clock says Wednesday and rolls to W41.
FROZEN = datetime(2026, 9, 30, 6, 30, tzinfo=UTC)
# The previous sprint's seed sync: Tue 2026-09-22, W39.
PREV = datetime(2026, 9, 22, 17, 0, tzinfo=UTC)
WEEK, PRIOR_WEEK, UTC_WEEK = "2026-W40", "2026-W39", "2026-W41"
MEETING_DAY = "2026-09-29"  # the tenant date the meeting happened on

# The plan's content — what a person must find again at the far end.
DECISION = "Launch the activation microsite on the Oct 15 date, not Oct 22"
ASK = "Send the final venue floor plan to Rina Lanham"
ASK_DUE = "2026-10-06"
RISK = "Venue power capacity unconfirmed for the LED wall"
QUESTION = "Do we need a second photographer for the evening session"
MISHEARD, CANONICAL = "Rena Lanham", "Rina Lanham"

TRANSCRIPT = [
    {"timestamp": "00:00:04", "speaker": {"display_name": "Drew Fiero"},
     "text": "Okay, activation check-in. Rena, can you walk us through the venue?"},
    {"timestamp": "00:00:19", "speaker": {"display_name": "Rena Lanham"},
     "text": "Sure. I'm the new event lead on our side. We still don't know if the "
             "venue power can carry the LED wall."},
    {"timestamp": "00:01:02", "speaker": {"display_name": "Drew Fiero"},
     "text": "Let's lock it: we launch the microsite on October fifteenth, not the "
             "twenty-second. I'll get you the final floor plan by next Tuesday."},
    {"timestamp": "00:01:40", "speaker": {"display_name": "Rena Lanham"},
     "text": "Open question for us is whether we need a second photographer at night."},
]

EXEC_FIELDS = {
    "Objective": "Google activation event and launch microsite for Q4.",
    "Status": "Venue locked; microsite build in progress.",
    "Where it stands": ["Venue contract signed 09-24.", "Microsite in design review."],
    "Next up": ["Floor plan to client by 10-06.", "Microsite launch 10-15."],
    "Blockers": ["Venue power capacity unconfirmed."],
}


def _plan_yaml(code: str) -> str:
    """What the model returns — keyed on the code it was given (the SHORT
    one, as the tag), dated with the meeting's tenant date."""
    return f"""```yaml
projects:
  {code}:
    decisions:
      - text: "{DECISION}"
        date: "{MEETING_DAY}"
    asks:
      - text: "{ASK}"
        who: "Drew"
        by: "{ASK_DUE}"
        date: "{MEETING_DAY}"
    risks:
      - text: "{RISK}"
        severity: "watching"
        category: "delivery"
        date: "{MEETING_DAY}"
    open_questions:
      - text: "{QUESTION}"
        date: "{MEETING_DAY}"
    stakeholders:
      - name: "{MISHEARD}"
        role: "Event Lead"
        context: "new client-side event lead for the activation"
```"""


# ── the fake MC-2 ────────────────────────────────────────────────────────

_COMPANY = {"code": "GGL", "name": "Google", "kind": "client"}


def _project_row(pid, number, full, name, *, parent=None, deal_stage=None, **kw):
    return {"id": pid, "number": number, "full_job_name": full, "name": name,
            "mc_status": "Open", "account_manager": "Drew", "is_internal": False,
            "deal_stage": deal_stage, "budget": None, "dropbox_folder_url": None,
            "updated_at": "2026-09-20T00:00:00+00:00", "parent_id": parent,
            "company_id": CO_ID, "companies": dict(_COMPANY), "repos": [],
            "code": None, **kw}


STALE_ID = "00000000-0000-0000-0000-00000000057a"
STALE_ASK = "Send the stale signage proofs"


def _mc2() -> FakeClient:
    client = FakeClient(
        companies=[{"id": CO_ID, **_COMPANY}],
        projects=[
            _project_row(ACC_ID, 5216, "GGL 5216 Google", "Google"),
            _project_row(JOB_ID, 5168, "GGL 5168 Activation", "Activation",
                         parent=ACC_ID, deal_stage="Won", budget=50000),
        ],
        entities=[{"name": "Drew Fiero", "email": "drew@firstperson.is",
                   "kind": "staff", "archived_at": None}],
        fathom_meetings=[{
            "id": MEETING_ID, "recording_id": 5168001,
            "title": "Google Activation — weekly check-in",
            # Fathom stores UTC: this meeting started Tue 23:00 Pacific.
            "meeting_date": "2026-09-30T06:00:00+00:00",
            "transcript": TRANSCRIPT, "summary": "Venue, launch date, floor plan.",
            "action_items": [],
            "participants": [{"name": "Drew Fiero", "email": "drew@firstperson.is"},
                             {"name": "Rena Lanham", "email": None}],
            "duration_minutes": 30, "fathom_url": "https://fathom.example/m/1",
            "project_tags": [SHORT], "project_id": None, "summary_embedded_at": None,
            "meeting_type": "client",
        }],
        commitments=[{
            # An undated meeting-ingest ask 20 days old: past the 14-day TTL
            # (#136), so the first sync must close it before rendering.
            "id": STALE_ID, "description": STALE_ASK, "owner_email": None,
            "owner_name": "Drew Fiero", "direction": "internal", "due_date": None,
            "date_status": "proposed", "status": "open",
            "source_kind": "meeting_ingest", "source_meeting_id": None,
            "cp_hash": "5ta1e000", "project_id": JOB_ID,
            "created_at": "2026-09-10T17:00:00+00:00",
            "updated_at": "2026-09-10T17:00:00+00:00",
        }], spine_substance=[], spine_steps=[],
        auto_ingest_runs=[], webhook_runs=[],
    )
    client.rpcs["is_team_member"] = lambda _p: True
    # What Postgres fills on INSERT. Without an id the webhook's
    # duplicate-delivery check and the hosted list's dedupe-on-id both
    # misbehave in ways production cannot.
    for table in ("commitments", "auto_ingest_runs", "webhook_runs", "spine_steps",
                  "spine_inbox", "mcp_audit_log"):
        client.defaults[table] = lambda: {"id": str(uuid.uuid4()),
                                          "created_at": _Clock.at.isoformat()}
    return client


# ── git ──────────────────────────────────────────────────────────────────

_PERSON = {"GIT_AUTHOR_NAME": "Drew", "GIT_AUTHOR_EMAIL": "drew@firstperson.is",
           "GIT_COMMITTER_NAME": "Drew", "GIT_COMMITTER_EMAIL": "drew@firstperson.is"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env={**os.environ, **_PERSON}).stdout.strip()


def _commit_push(clone: Path, message: str) -> bool:
    _git(clone, "add", "-A")
    if not _git(clone, "status", "--porcelain"):
        return False
    _git(clone, "commit", "-q", "-m", message)
    _git(clone, "push", "-q", "origin", "HEAD:main")
    return True


def _show(remote: Path, rel: str) -> str:
    return _git(remote, "show", f"main:{rel}")


# ── the clock ────────────────────────────────────────────────────────────


class _Clock:
    at = FROZEN


def _frozen_datetime_class():
    """`cp_engine.clock`'s `datetime`, with `now()` pinned. Every tenant-clock
    read in the engine, webhook and hosted server goes through
    `clock.tenant_now` / `tenant_today`, which call this. The metaclass keeps
    `isinstance(x, datetime)` (clock.local_date) true for real datetimes."""

    class _Meta(type):
        def __instancecheck__(cls, obj):
            return isinstance(obj, datetime)

    class _FrozenDT(datetime, metaclass=_Meta):
        @classmethod
        def now(cls, tz=None):
            from cp_engine.clock import tenant_timezone

            if tz is None:
                return _Clock.at.astimezone(tenant_timezone()).replace(tzinfo=None)
            return _Clock.at.astimezone(tz)

    return _FrozenDT


# ── the run ──────────────────────────────────────────────────────────────

_TOML = f"""[tenant]
name = "cp"
display = "Drew + Tony"
timezone = "America/Los_Angeles"

[engine]
version = "~= 0.130"

[sync]
backend = "mc-2"
cron = "0 * * * *"

[sync.mc_2]
supabase_project_ref = "e2e-fake"

[team]
members = ["drew", "tony"]

[names.aliases]
"{MISHEARD}" = "{CANONICAL}"
"Jeff Allman" = "Geoff Ahmann"

[stakeholders]
retire_markdown = true
"""


def _refuse_network(real_connect):
    def connect(self, address):
        if self.family == getattr(socket, "AF_UNIX", None):
            return real_connect(self, address)
        host = address[0] if isinstance(address, tuple) else address
        if host in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, address)
        raise AssertionError(f"e2e test reached the network: connect({address!r})")

    return connect


def _sign(raw: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return {"x-webhook-timestamp": ts, "x-webhook-signature": sig,
            "content-type": "application/json"}


def _sync(clone: Path, at: datetime):
    from cp_engine import config as cfg_mod
    from cp_engine.sync import sync_tenant

    _Clock.at = at
    try:
        return sync_tenant(cfg_mod.load(clone), now=at)
    finally:
        _Clock.at = FROZEN


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    mp = pytest.MonkeyPatch()
    try:
        yield _run(tmp, mp)
    finally:
        mp.undo()
        _reset_hosted_tree()


def _reset_hosted_tree():
    """Forget the hosted server's clone — and delete it: `tree_root()` clones
    into its own `mkdtemp()`, outside pytest's tmp dir."""
    import shutil

    mod = sys.modules.get("server")
    if mod is None:
        return
    root = mod._TREE_STATE.get("root")
    if root and Path(root).parent.name.startswith("hosted-cp-tree-"):
        shutil.rmtree(Path(root).parent, ignore_errors=True)
    mod._TREE_STATE.clear()


def _run(tmp: Path, mp: pytest.MonkeyPatch) -> SimpleNamespace:
    from cp_engine import clock, mc2_db

    client = _mc2()
    mp.setattr(mc2_db, "get_client", lambda *a, **k: client)
    mp.setattr(clock, "datetime", _frozen_datetime_class())

    # No network, by construction: the Anthropic key is removed, every model
    # call is stubbed at its transport, and a socket connect anywhere but a
    # local socket fails the run (an unstubbed call surfaced as a real POST
    # to api.anthropic.com while this file was being written).
    mp.delenv("ANTHROPIC_API_KEY", raising=False)
    mp.setattr(socket.socket, "connect", _refuse_network(socket.socket.connect))

    # The LLM, at the transport. `plan_from_transcript._call_claude` serves
    # the plan (YAML) — the ingest's only model call since step 5a turned off
    # the spine-inbox distillation and the Deeper-notes synthesis.
    import main as webhook_main  # the webhook app; its modules resolve below

    from cp_engine import plan_from_transcript

    prompts: list[str] = []

    def fake_claude(prompt, **_kw):
        prompts.append(prompt)
        return _plan_yaml(asked_for[-1])

    mp.setattr(plan_from_transcript, "_call_claude", fake_claude)

    # The model keys its plan on the code it was asked about; record which
    # one each pass asked about (the real generate_plan still runs).
    import pipeline

    asked_for: list[str] = []
    real_generate = pipeline.generate_plan

    def generate_plan(**kw):
        asked_for.append(kw["project_code"])
        return real_generate(**kw)

    mp.setattr(pipeline, "generate_plan", generate_plan)

    # ── the tenant: a bare remote, seeded by REAL syncs for W39 then W40 ──
    remote = tmp / "cp.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    seed = tmp / "seed"
    _git(tmp, "clone", "-q", str(remote), str(seed))
    (seed / ".cp-engine.toml").write_text(_TOML, encoding="utf-8")
    _sync(seed, PREV)
    _commit_push(seed, "seed: sync W39")
    _sync(seed, FROZEN)
    _commit_push(seed, "seed: sync W40")

    # ── the webhook ──
    mp.setenv("WEBHOOK_HMAC_SECRET", SECRET)
    mp.setenv("CP_TENANT_REPO_URL", str(remote))
    mp.delenv("CP_TENANT_BRANCH", raising=False)
    mp.delenv("GIT_SSH_KEY", raising=False)
    mp.delenv("SUPABASE_URL", raising=False)
    mp.delenv("SUPABASE_SERVICE_KEY", raising=False)
    mp.setenv("GIT_AUTHOR_NAME", "cp-engine-webhook")
    mp.setenv("GIT_AUTHOR_EMAIL", "webhook@firstperson.is")
    mp.setenv("GIT_COMMITTER_NAME", "cp-engine-webhook")
    mp.setenv("GIT_COMMITTER_EMAIL", "webhook@firstperson.is")

    from fastapi.testclient import TestClient

    http = TestClient(webhook_main.app, raise_server_exceptions=False)

    def post(path: str, body: dict):
        raw = json.dumps(body).encode()
        return http.post(path, content=raw, headers=_sign(raw))

    # An Exec Summary written through its real writer, BEFORE the meeting.
    exec_resp = post("/api/project-state/capture",
                     {"project_code": FULL, "fields": EXEC_FIELDS, "user": "Drew"})
    exec_before = _show(remote, f"{JOB_DIR}/cp.md")

    ingest1 = post("/api/auto-ingest", {"meeting_id": MEETING_ID, "project_codes": [SHORT]})
    after_ingest_head = _git(remote, "rev-parse", "main")
    sprint_after_ingest = _show(remote, f"sprints/{WEEK}/{FULL}.md")

    # ── sync, on a fresh clone, as the tenant's sync job does ──
    work = tmp / "work"
    _git(tmp, "clone", "-q", str(remote), str(work))
    sync1 = _sync(work, FROZEN)
    _commit_push(work, "[cp-sync] e2e — auto-sync from MC-2")
    commitments_after_sync1 = [dict(r) for r in client.store["commitments"]]

    # ── the hosted server, reading the same remote + the same MC-2 ──
    for k, v in {"SUPABASE_URL": "http://example.invalid", "SUPABASE_ANON_KEY": "x",
                 "MC2_API_BASE": "http://example.invalid"}.items():
        mp.setenv(k, v)
    import server

    mp.setattr(server, "TENANT_REPO", str(remote))
    mp.setattr(server, "GIT_SSH_KEY", "")
    mp.setattr(server, "user_client", lambda: client)
    mp.setattr(server, "caller_subject", lambda: "drew")
    mp.setattr(server, "caller_email", lambda: "drew@firstperson.is")
    server._TREE_STATE.clear()

    hosted = SimpleNamespace(
        state=server.get_project_state(SHORT),
        sprint_file=server.read_project_file(f"sprints/{WEEK}/{FULL}.md"),
        cp_md=server.read_project_file(f"{JOB_DIR}/cp.md"),
        commitments=server.list_commitments(SHORT),
        stakeholders=server.list_spine_elements(SHORT),
    )

    # ── the same meeting again ──
    # (a) Fathom retries the identical delivery: short-circuited.
    ingest2 = post("/api/auto-ingest", {"meeting_id": MEETING_ID, "project_codes": [SHORT]})
    # (b) Re-tagged under the FULL code: a different tag tuple, so the WHOLE
    #     pipeline runs again — plan, execute, commitments, card, artifacts.
    ingest3 = post("/api/auto-ingest", {"meeting_id": MEETING_ID, "project_codes": [FULL]})
    _git(work, "pull", "-q", "--rebase", "origin", "main")
    sync2 = _sync(work, FROZEN)
    _commit_push(work, "[cp-sync] e2e — second auto-sync")
    head_before_sync3 = _git(remote, "rev-parse", "main")
    sync3 = _sync(work, FROZEN)
    sync3_dirty = _git(work, "status", "--porcelain")

    return SimpleNamespace(
        client=client, remote=remote, work=work, prompts=prompts,
        exec_resp=exec_resp, exec_before=exec_before,
        ingest1=ingest1, ingest2=ingest2, ingest3=ingest3, after_ingest_head=after_ingest_head,
        sprint_after_ingest=sprint_after_ingest,
        sync1=sync1, sync2=sync2, sync3=sync3, sync3_dirty=sync3_dirty,
        head_before_sync3=head_before_sync3,
        commitments_after_sync1=commitments_after_sync1,
        hosted=hosted,
    )


# ── helpers over the result ──────────────────────────────────────────────


def _region(text: str, name: str) -> str:
    start, end = f"<!-- cp-engine:start {name} -->", f"<!-- cp-engine:end {name} -->"
    assert start in text and end in text, f"no {name} region"
    return text.split(start, 1)[1].split(end, 1)[0]


def _outside_regions(text: str) -> str:
    import re

    return re.sub(r"<!-- cp-engine:start (\S+) -->.*?<!-- cp-engine:end \1 -->",
                  "", text, flags=re.DOTALL)


def _section(text: str, heading: str) -> str:
    """The body of a `### heading` (to the next heading of any level)."""
    import re

    m = re.search(rf"^### {re.escape(heading)}\n(.*?)(?=^#{{1,3}} |\Z)", text,
                  re.MULTILINE | re.DOTALL)
    assert m, f"no ### {heading}"
    return m.group(1)


def _ask_rows(client):
    return [r for r in client.store["commitments"]
            if "venue floor plan" in (r.get("description") or "").lower()]


def _short_hash(text: str) -> str:
    """The step-4a recipe, recomputed here rather than imported: the SHORT
    `<co>-<number>` key, `ask`, the casefolded text."""
    return hashlib.sha256(f"{SHORT}|ask|{text.casefold()}".encode()).hexdigest()[:8]


# ── 0. the pipeline ran ──────────────────────────────────────────────────


def test_every_hop_answered(world):
    assert world.exec_resp.status_code == 200, world.exec_resp.text
    assert world.ingest1.status_code == 200, world.ingest1.text
    body = world.ingest1.json()
    (entry,) = body["ingested"]
    assert entry["errors"] == [], entry
    assert entry["commit_sha"], body
    assert world.prompts, "the plan was never generated"
    for name in ("state", "sprint_file", "cp_md", "commitments", "stakeholders"):
        out = getattr(world.hosted, name)
        assert "error" not in out, (name, out)


# ── 1. the sprint week, by the tenant clock ──────────────────────────────


def test_bullets_land_in_the_tenant_clock_sprint_week_and_hosted_agrees(world):
    text = world.sprint_after_ingest
    assert DECISION in text and QUESTION in text and RISK in text
    tree = _git(world.remote, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert f"sprints/{UTC_WEEK}/{FULL}.md" not in tree, (
        "a UTC clock rolled the sprint week on a Pacific Tuesday")
    assert DECISION not in _show(world.remote, f"sprints/{PRIOR_WEEK}/{FULL}.md")
    state = world.hosted.state
    assert state["sprint_week"] == WEEK, state.get("sprint_note")
    assert state["sprint_file"] == f"sprints/{WEEK}/{FULL}.md"
    assert "sprint_note" not in state, state["sprint_note"]
    assert DECISION in state["sprint_text"]


# ── 2. the SHORT tag resolved to the workstream's file (#345) ────────────


def test_short_code_tag_targets_the_full_workstream_file(world):
    (entry,) = world.ingest1.json()["ingested"]
    assert entry["code"] == SHORT
    assert [Path(p).name for p in entry["files_written"]] == [f"{FULL}.md"]
    tree = _git(world.remote, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert not [p for p in tree if p.startswith("sprints/") and p.endswith(f"/{SHORT}.md")]
    # Not the parent's file either (the prefix `ggl-5` is shared, the job is not).
    assert DECISION not in _show(world.remote, f"sprints/{WEEK}/{ACCOUNT}.md")
    assert world.hosted.state["working_dir"] == JOB_DIR
    subjects = _git(world.remote, "log", "--format=%s", "main").splitlines()
    assert f"[auto-ingest] {SHORT}: meeting {MEETING_ID[:8]}" in subjects, subjects


# ── 3. the ask is one MC-2 commitment, rendered, listed, never twinned ───


def test_ask_is_one_commitment_with_the_short_code_hash(world):
    rows = [r for r in world.commitments_after_sync1
            if "venue floor plan" in (r.get("description") or "").lower()]
    assert len(rows) == 1, rows
    (row,) = rows
    assert row["cp_hash"] == _short_hash(ASK), row
    assert row["project_id"] == JOB_ID
    assert row["status"] == "open"
    assert row["due_date"] == ASK_DUE


def test_ask_renders_in_the_open_asks_region_after_sync(world):
    sprint = _show(world.remote, f"sprints/{WEEK}/{FULL}.md")
    region = _region(sprint, "open-asks")
    assert ASK in region and f"by {ASK_DUE}" in region, region
    assert f"cp:hash={_short_hash(ASK)}" in region
    # Moved into the region, not copied beside it.
    assert sprint.count(ASK) == 1
    # And what the hosted reader sees.
    assert ASK in _region(world.hosted.sprint_file["text"], "open-asks")


def test_sync_expires_the_stale_undated_ask_before_rendering(world):
    """The 14-day TTL on undated meeting-ingest asks is enforced by the daily
    sync (step 5a retired the dates loop that used to): the row is closed as
    `expired` and never reaches the `open-asks` region."""
    (row,) = [r for r in world.commitments_after_sync1 if r["id"] == STALE_ID]
    assert row["status"] == "expired", row
    sprint = _show(world.remote, f"sprints/{WEEK}/{FULL}.md")
    assert STALE_ASK not in sprint


def test_list_commitments_returns_the_ask(world):
    out = world.hosted.commitments
    hits = [c for c in out["commitments"] if ASK.lower() in (c.get("description") or "").lower()]
    assert len(hits) == 1, out


def test_a_retried_delivery_is_short_circuited(world):
    assert world.ingest2.status_code == 200, world.ingest2.text
    assert world.ingest2.json()["status"] == "duplicate_delivery_skipped", world.ingest2.json()


def test_the_same_meeting_ingested_again_duplicates_nothing(world):
    """The re-tag under the FULL code runs the whole pipeline a second time:
    every write must find its first copy — the ask by its short-code hash,
    the bullets by theirs, the person by name — and say so cleanly."""
    assert world.ingest3.status_code == 200, world.ingest3.text
    body = world.ingest3.json()
    (entry,) = body["ingested"]
    assert entry["errors"] == [] and entry["files_written"] == [], entry
    assert body["warnings"] == [], body["warnings"]
    assert len(_ask_rows(world.client)) == 1, _ask_rows(world.client)
    sprint = _show(world.remote, f"sprints/{WEEK}/{FULL}.md")
    for text in (ASK, DECISION, QUESTION, RISK):
        assert sprint.count(text) == 1, text
    cards = [r for r in world.client.store["spine_substance"]
             if r.get("layer") == "Stakeholders" and r.get("status") == "live"]
    assert len(cards) == 1, [c.get("framing") for c in cards]


# ── 4. the stakeholder is a card under the CANONICAL name ────────────────


def test_misheard_stakeholder_becomes_a_card_under_the_canonical_name(world):
    cards = [r for r in world.client.store["spine_substance"]
             if r.get("layer") == "Stakeholders" and r.get("status") == "live"]
    names = [r.get("framing") or "" for r in cards]
    assert len(cards) == 1, names
    assert names[0].startswith(f"{CANONICAL} — "), names
    assert all(MISHEARD not in (r.get("framing") or "") for r in cards)
    assert cards[0]["project_id"] == JOB_ID
    strip = _region(_show(world.remote, f"{JOB_DIR}/cp.md"), "stakeholders-strip")
    assert f"**{CANONICAL}**" in strip and MISHEARD not in strip, strip
    hosted = [e for e in world.hosted.stakeholders["elements"]
              if e.get("layer") == "Stakeholders"]
    assert [e for e in hosted if CANONICAL in (e.get("framing") or "")], hosted
    assert not [e for e in hosted if MISHEARD in (e.get("framing") or "")], hosted


# ── 5. hand-written sections survive sync (#263) ─────────────────────────


def test_decision_and_open_question_survive_sync_outside_managed_regions(world):
    synced = _show(world.remote, f"sprints/{WEEK}/{FULL}.md")
    hand = _outside_regions(synced)
    assert DECISION in _section(hand, "Decisions")
    assert QUESTION in _section(hand, "Open questions")
    assert RISK in hand
    hosted = world.hosted.sprint_file["text"]
    assert DECISION in _section(_outside_regions(hosted), "Decisions")
    assert QUESTION in _section(_outside_regions(hosted), "Open questions")
    quarantine = [p for p in _git(world.remote, "ls-tree", "-r", "--name-only", "main")
                  .splitlines() if p.startswith("exceptions/region-edits/")]
    assert quarantine == [], quarantine


# ── 6. the Exec Summary is untouched unless written ──────────────────────


def test_exec_summary_survives_ingest_and_sync_and_reads_back_intact(world):
    written = _region(world.exec_before, "exec-summary")
    for value in ("Venue locked; microsite build in progress.", "Microsite launch 10-15."):
        assert value in written
    now = _region(_show(world.remote, f"{JOB_DIR}/cp.md"), "exec-summary")
    assert now == written
    assert world.hosted.state["exec_summary"] == written.strip()


# ── 7. the step-3 ledger ─────────────────────────────────────────────────


def test_the_post_left_a_webhook_runs_row(world):
    rows = [r for r in world.client.store["webhook_runs"] if r["route"] == "/api/auto-ingest"]
    assert len(rows) == 3, rows  # one per POST, the short-circuited one too
    assert [r["status"] for r in rows] == ["ok", "ok", "ok"], rows
    assert [r["http_status"] for r in rows] == [200, 200, 200]
    runs = world.client.store["auto_ingest_runs"]
    assert [r["status"] for r in runs] == ["success", "success"], runs
    assert [r["project_codes"] for r in runs] == [[SHORT], [FULL]]


# ── 8. sync is idempotent ────────────────────────────────────────────────


def test_a_second_sync_changes_nothing(world):
    assert world.sync3.files_written == (), world.sync3.files_written
    assert world.sync3_dirty == ""
    assert _git(world.remote, "rev-parse", "main") == world.head_before_sync3


