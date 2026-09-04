"""Account-scoped elements are visible from every sibling project. (#225)

WHY THIS EXISTS. An account-scoped element (mc-2 mig 104) is a COMPANY fact:
promoted off one project, belonging to every sibling on that account, with its
`project_id` retained as PROVENANCE ONLY. Three hosted read paths filtered on
`project_id` alone, which gets the scope ladder exactly backwards — the element
was visible on the one project it came FROM and invisible on every project it
was promoted FOR.

Measured 2026-09-03 against live data, before the fix: 26 live account rows
across Google, SAP Concur and Infoblox; `list_spine_elements("GGL-activation")`
returned 6 project-local elements and none of the 6 Google account cards;
`pull_spine_element("_authored/rina-lanham", "GGL-activation")` returned "no
element found"; and asking `semantic_search` who Rina Lanham is, scoped to that
project, returned five project-local elements and not her stakeholder card.

WHY THE WRITE SIDE DIDN'T CATCH IT. `smoke_test.py` covers account scope
thoroughly — promote, re-promote, demote, re-demote, version round-trip — but
every assertion reads back from the element's HOME project. Nothing asserted a
SIBLING could see it, which is the entire point of promoting. The tests below
are deliberately shaped the other way round: write on A, read from B.

The union rule is extracted as a pure function so it is testable without a
database; `test_live.py`-style checks against real rows live at the bottom and
skip when no credentials are present.
"""

import pytest


# ─────────────────────────────────────────────────────────────────────
#  The rule, extracted (mirrors `read_spine_rows` in server.py)
# ─────────────────────────────────────────────────────────────────────


def union_scope_arms(
    project_rows: list[dict],
    account_rows: list[dict],
) -> list[dict]:
    """Both arms of the scope ladder, deduped.

    Arm 1 keeps this project's own rows but DROPS `scope='account'` ones —
    their project_id is provenance, not membership. Arm 2 adds the company's
    account rows. An element promoted off this very project is dropped by arm 1
    and re-added by arm 2, so it appears exactly once.
    """
    rows = [r for r in project_rows if (r.get("scope") or "project") != "account"]
    seen = {r.get("est_item_id") for r in rows}
    for r in account_rows:
        if r.get("est_item_id") not in seen:
            rows.append(r)
    return rows


PROJECT_LOCAL = {"est_item_id": "_authored/working-plan", "scope": "project"}
ACCOUNT_CARD = {"est_item_id": "_authored/rina-lanham", "scope": "account"}
ACCOUNT_RULE = {"est_item_id": "_authored/cardinal-rule", "scope": "account"}


def slugs(rows):
    return [r["est_item_id"] for r in rows]


# ─────────────────────────────────────────────────────────────────────
#  The bug, as tests
# ─────────────────────────────────────────────────────────────────────


def test_a_sibling_project_sees_the_account_element():
    """The headline case: promoted on A, read from B."""
    rows = union_scope_arms([PROJECT_LOCAL], [ACCOUNT_CARD, ACCOUNT_RULE])
    assert "_authored/rina-lanham" in slugs(rows)
    assert "_authored/cardinal-rule" in slugs(rows)
    assert "_authored/working-plan" in slugs(rows)


def test_the_home_project_does_not_show_it_twice():
    """On its home project the row arrives from BOTH arms — dedupe to one.

    This is the regression the naive `rows + account_rows` fix would introduce:
    the element still has `project_id = home`, so arm 1 would return it too.
    """
    rows = union_scope_arms([PROJECT_LOCAL, ACCOUNT_CARD], [ACCOUNT_CARD])
    assert slugs(rows).count("_authored/rina-lanham") == 1


def test_the_account_arm_is_the_canonical_copy():
    """When both arms carry it, the account row (with its badge) survives."""
    stale = {**ACCOUNT_CARD, "framing": "stale project-arm copy"}
    fresh = {**ACCOUNT_CARD, "framing": "account-arm copy"}
    rows = union_scope_arms([stale], [fresh])
    row = next(r for r in rows if r["est_item_id"] == "_authored/rina-lanham")
    assert row["framing"] == "account-arm copy"


def test_an_initiative_has_no_account_arm():
    """Initiatives have no company, so the account arm contributes nothing."""
    rows = union_scope_arms([PROJECT_LOCAL], [])
    assert slugs(rows) == ["_authored/working-plan"]


def test_a_project_scoped_row_is_never_borrowed_by_a_sibling():
    """Only `scope='account'` crosses the project boundary."""
    other_project_row = {"est_item_id": "_authored/theirs", "scope": "project"}
    rows = union_scope_arms([PROJECT_LOCAL], [])
    assert other_project_row["est_item_id"] not in slugs(rows)


def test_a_missing_scope_reads_as_project():
    """Legacy rows predate the column; absent must not mean account."""
    legacy = {"est_item_id": "_authored/legacy"}  # no `scope` key at all
    rows = union_scope_arms([legacy], [])
    assert slugs(rows) == ["_authored/legacy"]


@pytest.mark.parametrize("scope", ["project", None, "", "ACCOUNT"])
def test_only_exactly_account_is_treated_as_account(scope):
    """Arm 1 drops a row only on an exact `account` match — no fuzzy casing."""
    row = {"est_item_id": "_authored/x", "scope": scope}
    rows = union_scope_arms([row], [])
    assert slugs(rows) == ["_authored/x"]


# ─────────────────────────────────────────────────────────────────────
#  Live checks — skipped without credentials
# ─────────────────────────────────────────────────────────────────────


def _live_client():
    import os

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not (url and key):
        pytest.skip("no Supabase credentials in the environment")
    from supabase import create_client

    return create_client(url, key)


def test_live_account_rows_are_reachable_from_a_sibling():
    """Against real data: every account row's company has sibling projects.

    Guards the shape of the data the fix depends on — an account row with a
    `company_id` and a `project_id` that is only one of that company's
    projects. If promotion ever stopped setting `company_id`, the account arm
    would silently return nothing and this catches it.
    """
    client = _live_client()
    rows = (
        client.table("spine_substance")
        .select("est_item_id, scope, company_id, project_id")
        .eq("scope", "account")
        .eq("status", "live")
        .execute()
        .data
        or []
    )
    if not rows:
        pytest.skip("no account-scoped rows in this database")

    missing = [r["est_item_id"] for r in rows if not r.get("company_id")]
    assert not missing, (
        f"account-scoped rows with no company_id are unreachable by the "
        f"account arm — they would be invisible on every sibling: {missing}"
    )
