"""#355 (item 1): a Stakeholders card's ``**Company:**`` is never defaulted
to the workstream's client company.

Measured 2026-10-01 on slt-5196: regenerating ``participant-adelle-bonavire``
prepended a ``cp:stakeholder`` block saying ``Company: Salesloft`` (the
project's client) while her body said PTC three times. Every interviewee,
vendor and customer on a client project inherited the client's name.

The company now comes only from an explicit signal: an ingest item's
``company`` field, an author-written ``Company:`` line already on the card,
or a markdown group header that names the client (``**Google (client)**``).
Otherwise the line is omitted, like every other unknown field.

CONTROLS. Each test drives a surface that exists on the unfixed engine
(``execute_plan``, ``sync_tenant``, ``apply_plan``) and FAILS there on its
assertions.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from tests.test_stakeholders_4b import (
    JOB,
    JOB_DIR,
    _card,
    _cards,
    _client,
    _ingest_tenant,
    _sync,
)


def _details(row):
    from cp_engine import stakeholders as sh

    return sh.parse_details(row["body"])


def test_ingest_card_naming_another_company_is_not_stamped_with_the_client(tmp_path):
    from cp_engine.ingest import execute_plan

    tenant = _ingest_tenant(tmp_path)
    client = _client()
    plan = {"projects": {"ggl-5168": {"stakeholders": [
        {"name": "Adelle Bonavire", "role": "Interview participant",
         "context": "Director of Demand Gen at PTC; PTC runs the stack PTC chose"},
    ]}}}
    res = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12), supabase=client)
    assert not res.errors, res.errors
    (row,) = _cards(client)
    assert "**Company:** Google" not in row["body"]
    assert "**Company:**" not in row["body"]  # unknown → omitted, never a guess
    assert _details(row).company == ""


def test_ingest_card_with_no_company_omits_the_line(tmp_path):
    from cp_engine.ingest import execute_plan

    tenant = _ingest_tenant(tmp_path)
    client = _client()
    plan = {"projects": {"ggl-5168": {"stakeholders": [
        {"name": "Laurie Kellogg", "role": "Creative Director"},
    ]}}}
    execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12), supabase=client)
    (row,) = _cards(client)
    assert "**Company:**" not in row["body"]


def test_ingest_explicit_company_field_is_used(tmp_path):
    from cp_engine.ingest import execute_plan

    tenant = _ingest_tenant(tmp_path)
    client = _client()
    plan = {"projects": {"ggl-5168": {"stakeholders": [
        {"name": "Adelle Bonavire", "role": "Interview participant", "company": "PTC"},
    ]}}}
    execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12), supabase=client)
    (row,) = _cards(client)
    assert "- **Company:** PTC" in row["body"]
    assert _details(row).company == "PTC"


def test_regenerating_a_card_keeps_the_company_its_author_wrote(tmp_path):
    """The Adelle case: a card with no details block whose author wrote
    ``**Company:** PTC`` in the body. A later mention adds the block (v2);
    it must carry PTC, not the client."""
    from cp_engine.ingest import execute_plan

    tenant = _ingest_tenant(tmp_path)
    client = _client(_card(
        JOB.code, "participant-adelle-bonavire", "Adelle Bonavire — Interview participant",
        body="**Company:** PTC\n\nAdelle runs demand gen at PTC.", pid=JOB.mc2_id))
    plan = {"projects": {"ggl-5168": {"stakeholders": [
        {"name": "Adelle Bonavire", "role": "Interview participant",
         "email": "abonavire@ptc.com"},
    ]}}}
    res = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12), supabase=client)
    assert not res.errors, res.errors
    (row,) = _cards(client)
    assert row["version_label"] == "v2"
    d = _details(row)
    assert d.company == "PTC" and d.email == "abonavire@ptc.com"


_SECTION = (
    "## Stakeholders\n\n**Google (client)**\n"
    "- **Jennifer Park** — Primary client lead\n\n"
    "**Interviewees**\n"
    "- **Adelle Bonavire** — Director of Demand Gen at PTC\n\n"
)


def test_sync_import_uses_the_client_only_under_a_group_that_names_it(tmp_path):
    """The one legitimate client default: the author grouped the person under
    a header naming the client. A person under any other header gets none."""
    client = _client()
    _sync(tmp_path, client)
    cp = tmp_path / JOB_DIR / "cp.md"
    body = cp.read_text()
    cp.write_text(body.replace("## Archive Index", _SECTION + "## Archive Index", 1))
    _sync(tmp_path, client)
    (jennifer,) = _cards(client, "Jennifer Park")
    (adelle,) = _cards(client, "Adelle Bonavire")
    assert _details(jennifer).company == "Google"
    assert "**Company:**" not in adelle["body"]


def test_migration_does_not_default_the_client_company(tmp_path: Path):
    from cp_engine import stakeholders as sh
    from cp_engine.stakeholder_import import apply_plan
    from tests.test_stakeholder_migration_4b import _ibx_tenant, _plan

    index, client = _ibx_tenant(tmp_path)
    plan = _plan(tmp_path, index, client)
    apply_plan(client, plan, project_company=sh.project_companies(client),
               company_names={"co-ibx": "Infoblox"}, today=date(2026, 10, 1))
    laurie = next(c for c in sh.fetch_cards(client) if c.name == "Laurie Kellogg")
    assert laurie.details.company == ""
