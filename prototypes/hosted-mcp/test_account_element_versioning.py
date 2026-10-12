"""An account-scoped element resolves from any sibling project. (#356)

2026-10-09: three Google stakeholder cards listed under ggl-5136 (scope:
account) refused `add_spine_version` from ggl-5136 with "no authored element
… in 'ggl-5136'" — each worked only under the card's owning code, which
`list_spine_elements` does not show. The read verbs had the #225 scope ladder;
the shared element resolver behind the version, relation and step verbs did
not. Shaped like test_account_scope_reads.py: write on A, resolve from B.
"""

from __future__ import annotations

import pytest

from test_canonical_write_code import FakeClient, _columns

OWNER = "11111111-1111-1111-1111-111111111111"    # ggl-5216-google (account)
SIBLING = "22222222-2222-2222-2222-222222222222"  # ggl-5136
OWNER2 = "44444444-4444-4444-4444-444444444444"  # ggl-5185
OTHER_CO_PROJECT = "33333333-3333-3333-3333-333333333333"
GOOGLE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SAP = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture
def server(monkeypatch):
    for k, v in {"SUPABASE_URL": "http://example.invalid", "SUPABASE_ANON_KEY": "x"}.items():
        monkeypatch.setenv(k, v)
    import server as mod

    return mod


def _row(pid, company, slug, framing, version, status, scope="account"):
    return {
        "id": f"{pid}/{slug}/{version}", "project_id": pid, "project_code": "x",
        "est_item_id": slug, "framing": framing, "version_label": version,
        "status": status, "scope": scope, "company_id": company,
    }


@pytest.fixture
def client(server):
    c = FakeClient({"spine_substance": _columns(server._ELEMENT_RESOLVE_COLUMNS)})
    c.store["projects"] = [
        {"id": OWNER, "company_id": GOOGLE},
        {"id": SIBLING, "company_id": GOOGLE},
        {"id": OWNER2, "company_id": GOOGLE},
        {"id": OTHER_CO_PROJECT, "company_id": SAP},
    ]
    c.store["spine_substance"] = [
        _row(OWNER, GOOGLE, "_authored/rina-lanham", "Rina Lanham — Chief of Staff", "v3", "superseded"),
        _row(OWNER, GOOGLE, "_authored/rina-lanham", "Rina Lanham — Chief of Staff", "v4", "live"),
        _row(SIBLING, None, "_authored/working-plan", "Working plan", "v1", "live", scope="project"),
    ]
    return c


def test_sibling_resolves_the_account_element_with_every_version(server, client):
    est, versions, err = server.resolve_element_versions(client, SIBLING, "_authored/rina-lanham")
    assert err is None
    assert est == "_authored/rina-lanham"
    assert {v["version_label"] for v in versions} == {"v3", "v4"}
    # The write lands on the OWNING project, not the caller's.
    assert {v["project_id"] for v in versions} == {OWNER}


def test_sibling_resolves_by_bare_slug_and_framing(server, client):
    assert server.resolve_element_versions(client, SIBLING, "rina-lanham")[0] == "_authored/rina-lanham"
    assert server.resolve_element_versions(client, SIBLING, "Chief of Staff")[0] == "_authored/rina-lanham"


def test_project_element_still_wins_first(server, client):
    est, _, err = server.resolve_element_versions(client, SIBLING, "working-plan")
    assert (est, err) == ("_authored/working-plan", None)


def test_another_company_cannot_reach_it(server, client):
    assert server.resolve_element_versions(client, OTHER_CO_PROJECT, "rina-lanham") == (None, [], None)


def test_two_account_elements_sharing_a_key_is_an_error(server, client):
    client.store["spine_substance"].append(
        _row(OWNER2, GOOGLE, "_authored/rina-lanham-2", "Rina Lanham — sponsor note", "v1", "live")
    )
    est, _, err = server.resolve_element_versions(client, SIBLING, "Lanham")
    assert est is None and "matches 2 account elements" in err["error"]
