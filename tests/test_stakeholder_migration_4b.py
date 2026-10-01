"""The one-time step-4b migration (``scripts/step4b_migrate_stakeholders.py``,
``cp_engine.stakeholder_import``): markdown-only people become spine cards,
confirmed spelling variants collapse onto one card, internal people and
non-people are skipped, and a re-run never mints a twin."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from tests._spine_fake import FakeClient
from tests.test_stakeholders_4b import _card


def _ibx_tenant(tmp_path: Path):
    """An Infoblox account + one job, its cp.md naming people three ways,
    and a sprint week listing "Mahool" and "Jeff Amon"."""
    from cp_engine.state import PathEntry

    index = {
        "ibx-5217-infoblox": PathEntry("ibx-5217-infoblox", "1p/infoblox", None, False,
                                       "account", "uuid-ibx-acct", "IBX", "Open"),
        "ibx-5153-ai-campaign": PathEntry("ibx-5153-ai-campaign",
                                          "1p/infoblox/ibx-5153-ai-campaign",
                                          "ibx-5217-infoblox", True, "job", "uuid-ibx-5153",
                                          "IBX", "Open"),
        "ibx-5192-platform-sales-readiness-summit": PathEntry(
            "ibx-5192-platform-sales-readiness-summit",
            "1p/infoblox/ibx-5192-platform-sales-readiness-summit", "ibx-5217-infoblox",
            True, "job", "uuid-ibx-5192", "IBX", "Closed"),
    }
    (tmp_path / "1p/infoblox/ibx-5153-ai-campaign").mkdir(parents=True)
    (tmp_path / "1p/infoblox/cp.md").write_text(
        "# x\n\n## Stakeholders\n\n| Name | Email | Notes |\n|---|---|---|\n"
        "| Mehul Patel | mpatel1@infoblox.com | Workshop attendee |\n"
        "| Rena Ramos | | template example |\n\n## Next\n")
    (tmp_path / "1p/infoblox/ibx-5153-ai-campaign/cp.md").write_text(
        "# x\n\n## Stakeholders\n\n- **Mahul** — product leadership\n"
        "- **Laurie Kellogg** — Creative Director\n"
        "- **Geoff** — designer\n\n## Next\n")
    week = tmp_path / "sprints" / "2026-W38"
    week.mkdir(parents=True)
    (week / "ibx-5153.md").write_text(
        "## Client communication\n\n### Stakeholders\n"
        "- [Mahool · Infoblox · trust-first principle]\n"
        "- [Jeff Amon · Designer · may put the deck into final design]\n\n## Next\n")
    mehul = _card("ibx-5192-platform-sales-readiness-summit", "mehul-main-srs",
                  "Mehul - main SRS field-deck presenter", pid="uuid-ibx-5192")
    client = FakeClient(
        spine_substance=[mehul],
        projects=[{"id": e.mc2_id, "company_id": "co-ibx"} for e in index.values()],
        companies=[{"id": "co-ibx", "name": "Infoblox"}],
    )
    return index, client


def _plan(tmp_path, index, client):
    from cp_engine import stakeholders as sh
    from cp_engine.stakeholder_import import collect_mentions, plan_migration

    mentions, _ = collect_mentions(tmp_path, index, include_inactive=False)
    roster = sh.Roster.build(["drew", "tony"], ["Geoff Ahmann", "Drew Fiero"])
    return plan_migration(
        mentions, cards=sh.fetch_cards(client), index=index,
        project_company=sh.project_companies(client), roster=roster,
        tenant_aliases={"Jeff Allman": "Geoff Ahmann"},
        company_names={"co-ibx": "Infoblox"},
    )


def test_migration_collapses_variants_and_never_mints_twins_on_rerun(tmp_path):
    from cp_engine import stakeholders as sh
    from cp_engine.stakeholder_import import apply_plan

    index, client = _ibx_tenant(tmp_path)
    plan = _plan(tmp_path, index, client)
    by_name = {p.name: p for p in plan.people}
    # Mahul / Mahool / Mehul Patel → the ONE existing "Mehul" card, promoted
    # to account scope (it sits on a closed workstream).
    assert set(by_name) == {"Mehul Patel", "Laurie Kellogg"}
    mehul = by_name["Mehul Patel"]
    assert mehul.action == "update" and mehul.promote
    assert {"Mahul", "Mahool", "Mehul"} <= mehul.variants
    skipped = {(s.name, s.reason.split(" ")[0]) for s in plan.skipped}
    assert ("Geoff", "internal") in skipped and ("Jeff Amon", "internal") in skipped
    assert any(s.name == "Rena Ramos" for s in plan.skipped)

    apply_plan(client, plan, project_company=sh.project_companies(client),
               company_names={"co-ibx": "Infoblox"}, today=date(2026, 10, 1))
    live = sh.fetch_cards(client)
    assert sorted(c.name for c in live) == ["Laurie Kellogg", "Mehul Patel"]
    m = next(c for c in live if c.name == "Mehul Patel")
    assert m.details.email == "mpatel1@infoblox.com" and m.scope == "account"
    assert "Mahool" in m.aliases

    # Re-run: every person resolves to the card the first run made.
    plan2 = _plan(tmp_path, index, client)
    assert {p.action for p in plan2.people} == {"match"}
    apply_plan(client, plan2, project_company=sh.project_companies(client),
               company_names={"co-ibx": "Infoblox"}, today=date(2026, 10, 2))
    assert len(sh.fetch_cards(client)) == 2
