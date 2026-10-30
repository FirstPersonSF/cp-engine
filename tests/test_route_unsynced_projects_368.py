"""A workstream in MC-2 but not yet in the tree is a routing candidate (#368).

The report: cnc-5239 Canonic Site existed in MC-2 but had no tree directory
until a manual `cxp sync`, and the 10-29 ingest flagged "Publish Canonic
site" `[off-project? → cnc-5237-soc-2]`, the only Canonic leaf it knew.

What the code does: the ingest roster is `list_active_all(config)`, which
reads `backend.read_projects` (MC-2), never `.cp-engine/paths.json`. The
tree only ADDS vocabulary (`load_tree_documents`) for workstreams that have
a dir; a workstream without one keeps its roster entry and its name.

What happened on 10-29 (MC-2, read 10-30): the two flagged rows were
written 17:39 and 22:29 UTC; `CNC 5239 Canonic Site` was created 23:59 UTC.
The workstream did not exist in MC-2 when either ingest ran, so neither a
roster read nor a pre-ingest sync could have offered it.

These tests pin the contract the issue asks for, so a later change that
filters the roster through the tree fails here.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from cp_engine.config import SyncConfig, TenantConfig
from cp_engine.state import ProjectState

NEW = "cnc-5239-canonic-site"


def _ps(
    code,
    name,
    company,
    company_code,
    *,
    kind="client",
    parent=None,
    label="job",
    agreement=True,
    status="Open",
) -> ProjectState:
    return ProjectState(
        code=code,
        name=name,
        has_agreement=agreement,
        parent_code=parent,
        label=label,  # type: ignore[arg-type]
        company_kind=kind,  # type: ignore[arg-type]
        company_code=company_code,
        company_name=company,
        status=status,
        owner="Drew",
        last_touched=datetime(2026, 10, 29, tzinfo=UTC),
        deadline=None,
    )


# The MC-2 rows. Canonic's shape mirrors the live tree: StoryOS is the
# program, SOC 2 its child, and the new Canonic Site a second child that
# MC-2 has and the tree does not.
MC2_ROWS = [
    _ps(
        "cnc-9004-storyos",
        "CNC 9004 StoryOS",
        "Canonic",
        "CNC",
        kind="self-canonic",
        label="program",
        agreement=False,
    ),
    _ps(
        "cnc-5237-soc-2",
        "CNC 5237 SOC 2",
        "Canonic",
        "CNC",
        kind="self-canonic",
        parent="cnc-9004-storyos",
        label="initiative",
        agreement=False,
    ),
    _ps(
        NEW,
        "CNC 5239 Canonic Site",
        "Canonic",
        "CNC",
        kind="self-canonic",
        parent="cnc-9004-storyos",
        label="initiative",
        agreement=False,
    ),
    _ps(
        "sap-5198-2027-ad-videos",
        "SAP 5198 2027 Ad Videos",
        "SAP Concur",
        "SAP",
        parent="sap-5218-sap-concur",
    ),
    _ps(
        "sap-5218-sap-concur",
        "SAP 5218 SAP Concur",
        "SAP Concur",
        "SAP",
        label="account",
        agreement=False,
    ),
    _ps(
        "slt-5196-brand-campaign-26",
        "SLT 5196 Brand Campaign 26",
        "Salesloft",
        "SLT",
        parent="slt-5219-salesloft",
    ),
    _ps(
        "slt-5219-salesloft",
        "SLT 5219 Salesloft",
        "Salesloft",
        "SLT",
        label="account",
        agreement=False,
    ),
    _ps("ibx-5153-ai-campaign", "IBX 5153 AI Campaign", "Infoblox", "IBX"),
]

# Each synced workstream's own record. None of Canonic's names "site", as
# on 10-29: the live flag fell through to Canonic's only leaf (cnc-5237),
# which is what a roster without cnc-5239 produces from this corpus.
TREE_LINES = {
    "cnc-9004-storyos": [
        "Healy at Navon wants the Hexagon playbook before the StoryOS pilot call",
        "StoryOS narrator latency fix shipped to dev with Tony reviewing",
    ],
    "cnc-5237-soc-2": [
        "Thoropass opened the Type 1 audit; evidence collection is at 0 of 66",
        "Alex Turner to send the Thoropass pen-test methodology deck to Drew",
    ],
    "sap-5198-2027-ad-videos": [
        "Fiona and Michelle approved the 2027 ad video schedule for SAP Concur",
        "Contact Liam Chappell about freelance director availability for the shoot",
    ],
    "slt-5196-brand-campaign-26": [
        "Latoya and Sean interviews scheduled for the Atlanta Salesloft shoot",
        "Morgan to confirm the interviewee lineup for the Salesloft brand shoot",
    ],
    "ibx-5153-ai-campaign": [
        "Janet Noe wants Jaime in the room for the next Infoblox creative review",
        "Hector Mendoza Suarez sends the Infoblox platform diagram source",
    ],
}

ITEM = "Publish Canonic site; share live link w/ team"


def _tenant(root: Path) -> TenantConfig:
    """A tree synced BEFORE cnc-5239 existed: every workstream but the new
    one has a dir, a `cp.md` and a `paths.json` row."""
    rows = {}
    for p in MC2_ROWS:
        if p.code == NEW:
            continue
        rel = f"tree/{p.code}"
        d = root / rel
        d.mkdir(parents=True)
        (d / "cp.md").write_text(
            "# cp\n\n" + "\n".join(f"- {line}" for line in TREE_LINES.get(p.code, [])) + "\n",
            encoding="utf-8",
        )
        rows[p.code] = {
            "path": rel,
            "parent": p.parent_code,
            "label": p.label,
            "has_agreement": p.has_agreement,
            "status": p.status,
        }
    (root / ".cp-engine").mkdir()
    (root / ".cp-engine" / "paths.json").write_text(
        json.dumps({"version": 1, "workstreams": rows}), encoding="utf-8"
    )
    return TenantConfig(
        name="1p",
        display="1P Test",
        engine_version_constraint="~= 0.1",
        sync=SyncConfig(backend="mc-2", cron="0 * * * *", mc_2_supabase_project_ref="ref"),
        projects=(),
        root=root,
    )


class _Backend:
    def __init__(self, rows):
        self._rows = rows

    def read_projects(self, config):
        return tuple(self._rows)


@pytest.fixture
def tenant(tmp_path, monkeypatch):
    cfg = _tenant(tmp_path)
    monkeypatch.setattr("cp_engine.sync._default_backend_factory", lambda _n: _Backend(MC2_ROWS))
    return cfg


def _index(cfg, roster):
    from cp_engine.commitment_routing import build_routing_index

    return build_routing_index(roster, client=None, tenant_root=cfg.root)


def test_the_fixture_tree_does_not_know_the_new_workstream(tenant) -> None:
    from cp_engine.plan_from_transcript import _find_project_dir
    from cp_engine.state import load_paths_index

    assert NEW not in load_paths_index(tenant.root)
    assert _find_project_dir(tenant.root, NEW) is None
    assert _find_project_dir(tenant.root, "cnc-5237-soc-2") is not None


def test_ingest_roster_reads_mc2_not_the_tree(tenant) -> None:
    from cp_engine.plan_from_account_meeting import list_active_all

    codes = [p.code for p in list_active_all(tenant)]
    assert NEW in codes


@pytest.mark.parametrize("tagged", ["slt-5196-brand-campaign-26", "sap-5198-2027-ad-videos"])
def test_scored_suggestion_reaches_a_workstream_with_no_dir(tenant, tagged) -> None:
    from cp_engine.plan_from_account_meeting import list_active_all

    hit = _index(tenant, list_active_all(tenant)).suggest(ITEM, tagged)
    assert hit is not None and hit.target_code == NEW


def test_control_without_the_mc2_row_reproduces_the_10_29_flag(tenant) -> None:
    """The probe must FAIL on the 10-29 state: with no cnc-5239 row (it did
    not exist yet) the same item falls through to Canonic's only leaf."""
    roster = [p for p in MC2_ROWS if p.code != NEW]
    hit = _index(tenant, roster).suggest(ITEM, "slt-5196-brand-campaign-26")
    assert hit is not None and hit.target_code == "cnc-5237-soc-2"


def test_explicit_code_and_whole_name_screen_reach_it(tenant) -> None:
    wh = str(Path(__file__).resolve().parent.parent / "webhook")
    if wh not in sys.path:
        sys.path.insert(0, wh)
    import commitments_propose as cprop

    from cp_engine.plan_from_account_meeting import list_active_all

    roster = list_active_all(tenant)
    for text in ("Ship cnc-5239-canonic-site hero video", "Review CNC 5239 Canonic Site copy"):
        got = cprop.screen_off_project(text, ingest_codes=["slt-5196"], roster=roster)
        assert got == NEW, text


def test_propose_commitments_flags_the_unsynced_workstream(tenant, monkeypatch) -> None:
    """End to end through the webhook's propose path: the stored row carries
    `[off-project? → cnc-5239-canonic-site]`."""
    wh = str(Path(__file__).resolve().parent.parent / "webhook")
    if wh not in sys.path:
        sys.path.insert(0, wh)
    import commitments_propose as cprop

    from cp_engine.plan_from_account_meeting import list_active_all

    meeting = {
        "id": "m-368",
        "participants": [{"name": "Drew Fiero", "email": "drew@firstperson.is"}],
        "action_items": [{"description": ITEM, "assignee": {"name": "Drew Fiero"}}],
    }

    class _Q:
        def __getattr__(self, _n):
            return lambda *a, **k: self

        def execute(self):
            return SimpleNamespace(data=meeting)

    monkeypatch.setattr(
        cprop.mc2_db, "get_client", lambda required=False: SimpleNamespace(table=lambda n: _Q())
    )
    monkeypatch.setattr(
        cprop,
        "resolve_commitment_owner",
        lambda client, code: {
            "id": "p",
            "code": code,
            "kind": "project",
            "canonical_code": "slt-5196-brand-campaign-26",
        },
    )
    written: list[dict] = []
    monkeypatch.setattr(
        cprop, "write_commitment", lambda client, **kw: written.append(kw) or "inserted"
    )

    roster = list_active_all(tenant)
    summary = cprop.propose_commitments(
        "m-368", ["slt-5196"], roster=roster, routing_index=_index(tenant, roster)
    )
    assert summary["off_project_flagged"] == 1
    (row,) = written
    assert row["description"] == f"{ITEM} [off-project? → {NEW}]"
