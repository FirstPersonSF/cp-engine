"""#314 — a supersede must SHOW what it hides.

The Carol v1 on ibx-5153 was a failed distill that read as canon for seven
weeks. It was caught only because someone pulled the element before
versioning it; a blind `add_spine_version` would have demoted it to history
unread. So the verb now returns `prior` — the superseded version's first
lines, size, and its machine-derived marker — and a `warning` when the body
it hid was a distiller's that no human confirmed.

Harness: the column-checking fake from test_canonical_write_code, so a column
the preview selects that `spine_substance` lacks fails here, not in prod.

    python -m pytest prototypes/hosted-mcp/test_supersede_preview.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_canonical_write_code import (  # noqa: E402, I001
    CANONICAL, COMPANY_ID, PID, SHORT, FakeClient, _columns,
)

ELEMENT = "_authored/carol-s-our-ai-story-narrative-prose"
BAD_V1 = ("-distillation\nOur AI memo (board-approved). It says we will use AI "
          "to augment our team, not replace them.\n\nTone is reassuring.")


@pytest.fixture
def server(monkeypatch):
    for k, v in {"SUPABASE_URL": "http://example.invalid",
                 "SUPABASE_ANON_KEY": "x"}.items():
        monkeypatch.setenv(k, v)
    import server as mod
    return mod


@pytest.fixture
def client(server, monkeypatch):
    known = {"spine_substance": _columns(server.SPINE_PULL_COLUMNS)
             | _columns(server._ELEMENT_RESOLVE_COLUMNS)
             # the preview's own select, verified against the live grant
             | {"field_states", "review_flags", "origin"}}
    c = FakeClient(known)
    c.store["projects"] = [{"id": PID, "code": "SLT-brand-campaign-26",
                            "full_job_name": "SLT 5196 Brand Campaign 26",
                            "company_id": COMPANY_ID, "number": 5196}]
    c.store["companies"] = [{"id": COMPANY_ID, "code": "SLT"}]
    monkeypatch.setattr(server, "user_client", lambda: c)
    monkeypatch.setattr(server, "caller_subject", lambda: "user-sub-1")
    monkeypatch.setattr(server, "audit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_paths_index_rows", lambda: {})
    monkeypatch.setattr(server, "upsert_auto_step", lambda *a, **k: {"ok": True})
    return c


def _seed(client, *, field_states, review_flags=()):
    client.store["spine_substance"] = [{
        "id": f"{CANONICAL}/{ELEMENT}/v1", "project_id": PID,
        "project_code": CANONICAL, "est_item_id": ELEMENT,
        "version_label": "v1", "version_date": "2026-06-20", "status": "live",
        "framing": "Carol's 'Our AI Story' narrative prose",
        "layer": "Source material", "origin": "authored", "body": BAD_V1,
        "field_states": field_states, "review_flags": list(review_flags),
    }]


def _bump(server):
    return server.add_spine_version(project_code=SHORT, element_id=ELEMENT,
                                    body="# Our AI Story — Final V1\n\nDNS is the control point.")


def test_supersede_returns_the_prior_versions_first_lines(server, client):
    """CONTROL: against the unfixed verb there is no `prior` key at all."""
    _seed(client, field_states={"serves": "confirmed"})
    out = _bump(server)
    assert "error" not in out, out
    prior = out["prior"]
    assert prior["version_label"] == "v1"
    assert prior["head"] == [
        "-distillation",
        "Our AI memo (board-approved). It says we will use AI to augment our "
        "team, not replace them.",
        "Tone is reassuring.",
    ]
    assert prior["body_chars"] == len(BAD_V1)
    # A person's (unmarked) body: preview, no alarm.
    assert "provenance" not in prior
    assert "superseded" not in (out.get("warning") or "")


def test_superseding_an_unconfirmed_distill_warns(server, client):
    """CONTROL: against the unfixed verb the machine-derived v1 is demoted
    silently — the exact move that would have buried Carol's."""
    from cp_engine.distill_fidelity import MACHINE_DERIVED, MACHINE_DERIVED_LABEL

    _seed(client, field_states={"body": MACHINE_DERIVED},
          review_flags=[{"field": "body", "source": "distill-fidelity",
                         "now": "only 1% of the body's phrases occur in the source"}])
    out = _bump(server)
    assert out["prior"]["provenance"] == MACHINE_DERIVED_LABEL
    assert out["prior"]["fidelity_flags"]
    assert "superseded v1" in out["warning"]


def test_pull_surfaces_the_marker(server, client, monkeypatch):
    """CONTROL: against the unfixed verb the pull carries no `provenance`."""
    from cp_engine.distill_fidelity import MACHINE_DERIVED, MACHINE_DERIVED_LABEL

    _seed(client, field_states={"body": MACHINE_DERIVED})
    monkeypatch.setattr(server, "_with_project_status", lambda d, *a, **k: d)
    out = server.pull_spine_element(element_id=ELEMENT)
    assert out["provenance"] == MACHINE_DERIVED_LABEL

    # A human confirming the body IS the clear — no other step.
    client.store["spine_substance"][0]["field_states"] = {"body": "confirmed"}
    out = server.pull_spine_element(element_id=ELEMENT)
    assert "provenance" not in out
