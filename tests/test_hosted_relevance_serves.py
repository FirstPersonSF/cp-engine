# tests/test_hosted_relevance_serves.py — #179 step 4: a stakeholder's `serves`
# means RELEVANCE, not a work binding.
#
# Account scope (mig 104 / promote_stakeholder) makes a dossier READABLE from
# every project of the company. That is undifferentiated, and the cost is
# measurable: sap-5171 (display ads) currently reads all 16 SAP dossiers —
# 171k chars including Fred (CPO) and Charlie (President, Concur Travel), both
# interviewed for sap-5174's vision work and irrelevant to display ads.
#
# Drew's requirement: "It's fine for them to be account level cards or objects,
# but we need to be able to connect them to the project."
#
# `serves` already answers "what is this relevant to", so it carries the link.
# But `binding` is a WORK fact — spine_lint.py:109's absorbed-but-serving check
# reads it as one — so a person with `serves` must stay `unbound` rather than
# assert they are live work in progress.
#
# Same harness as test_hosted_rename_journal: the decorated verbs are MCP Tool
# objects, so this targets the module-level helper they are built from.
import importlib.util
import os
from pathlib import Path

import pytest

pytest.importorskip("jwt")
pytest.importorskip("mcp")
pytest.importorskip("supabase")

_SERVER_PATH = (
    Path(__file__).resolve().parents[1] / "prototypes" / "hosted-mcp" / "server.py"
)


@pytest.fixture(scope="module")
def srv():
    os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
    os.environ.setdefault("SUPABASE_ANON_KEY", "anon-key-for-tests")
    spec = importlib.util.spec_from_file_location("hosted_mcp_server", _SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stakeholder_serves_is_relevance(srv):
    assert srv._is_relevance_serves("Stakeholders")


def test_singular_and_casing_and_spacing_all_match(srv):
    """Layers drift between CamelCase (code) and spaced Title Case (live DB),
    and `canon_layer` accepts the singular alias."""
    for spelling in ("Stakeholders", "stakeholders", "Stakeholder", "stakeholder"):
        assert srv._is_relevance_serves(spelling), spelling


def test_work_layers_keep_their_work_binding(srv):
    """Only people are exempt. A deliverable or activity with `serves` IS
    bound to live work, and spine_lint's absorbed-but-serving check depends
    on that staying true."""
    for layer in ("Deliverables", "Activity", "Synthesis", "Client feedback",
                  "Source material", "Decisions", "Brief"):
        assert not srv._is_relevance_serves(layer), layer


def test_a_missing_layer_is_not_relevance(srv):
    """11 live rows carry layer: null. Defaulting them to relevance would
    silently unbind real work."""
    assert not srv._is_relevance_serves(None)
    assert not srv._is_relevance_serves("")


# --- the read side: narrowing the account roster ----------------------------

from cp_engine.project_sources import filter_account_by_relevance

PROJECT_WORK = {"_authored/display-ad-concepts", "ei-7"}


def _person(name, *, scope="account", serves=None, layer="Stakeholders"):
    return {"est_item_id": f"_authored/{name}", "framing": name,
            "layer": layer, "scope": scope, "serves": serves or []}


def test_an_account_person_serving_other_work_is_hidden():
    """Fred (CPO) was interviewed for sap-5174's vision work. He should not
    appear on sap-5171 (display ads)."""
    rows = [_person("fred", serves=["_authored/vision-report"])]
    kept, hidden = filter_account_by_relevance(rows, PROJECT_WORK)
    assert kept == []
    assert hidden == 1


def test_an_account_person_serving_this_project_is_kept():
    rows = [_person("fiona", serves=["_authored/display-ad-concepts"])]
    kept, hidden = filter_account_by_relevance(rows, PROJECT_WORK)
    assert len(kept) == 1
    assert hidden == 0


def test_an_unlinked_dossier_is_KEPT_not_hidden():
    """0 of the tenant's 18 account stakeholders carry `serves` today. Hiding
    them would empty every roster — worse than the noise this fixes. The
    filter degrades to current behaviour until the links exist."""
    rows = [_person("charlie", serves=[])]
    kept, hidden = filter_account_by_relevance(rows, PROJECT_WORK)
    assert len(kept) == 1
    assert hidden == 0


def test_a_projects_own_stakeholder_is_never_filtered():
    """Project-scoped people are already this project's, whatever they serve."""
    rows = [_person("janet", scope="project", serves=["_authored/somewhere-else"])]
    kept, _ = filter_account_by_relevance(rows, PROJECT_WORK)
    assert len(kept) == 1


def test_non_stakeholder_account_elements_pass_through():
    """Account scope applies to any layer (set_element_account_scope). Only
    PEOPLE are narrowed by relevance here."""
    rows = [_person("a-synthesis", layer="Synthesis",
                    serves=["_authored/somewhere-else"])]
    kept, hidden = filter_account_by_relevance(rows, PROJECT_WORK)
    assert len(kept) == 1
    assert hidden == 0


def test_it_reports_what_it_narrowed():
    rows = [
        _person("fred", serves=["_authored/vision-report"]),
        _person("charlie", serves=["_authored/vision-report"]),
        _person("fiona", serves=["_authored/display-ad-concepts"]),
        _person("unlinked", serves=[]),
    ]
    kept, hidden = filter_account_by_relevance(rows, PROJECT_WORK)
    assert {r["framing"] for r in kept} == {"fiona", "unlinked"}
    assert hidden == 2
