"""Stakeholders live on spine cards in MC-2 (architecture plan step 4b,
decided by Drew 2026-10-01), plus cp-engine #347 (render order).

The defects these pin, measured on the tenant 2026-10-01 (step-4 drift, B):

1. The cp.md ``stakeholders-strip`` aggregated sprint files only and never
   read a card: 20 of 42 live cards (12 SAP account cards, 6 slt-5196
   participants) appeared on no strip.
2. Attribution (#312) read ``spine/`` and the sprint ``### Stakeholders``
   bullets — never an account's ``_stakeholders/`` — so an account card's
   person was minted again as a "new stakeholder".
3. Ingest wrote a sprint bullet, never a card: 58 active external people
   existed only in markdown.
4. Spelling variants ("Rena Lanham" in five cp.md files for the card's
   "Rina Lanham") never resolved to one person.
5. #347: sync rendered a parent's rollup BEFORE its children's MC-2-rendered
   asks, so a new ask reached the account rollup one sync late.

CONTROLS. Every test drives a surface that exists on the pre-4b engine
(``sync_tenant``, ``execute_plan``, ``load_known_people`` /
``check_plan_attribution``). Run against origin/main's ``src`` each FAILS on
its assertions — not on an import — which is what proves it tests the defect.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from cp_engine import sync_tenant
from cp_engine.sprints import current_sprint_week_iso
from tests._spine_fake import FakeClient, substance_row
from tests.test_sync import FakeBackend, make_config, make_state

_NOW = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)
CO = "co-google"


def _ws(code, *, label, parent_code=None, has_agreement=False):
    return replace(
        make_state(code=code, has_agreement=has_agreement, company_kind="client",
                   company_code="GGL", company_name="Google"),
        label=label, parent_code=parent_code, mc2_id=f"uuid-{code}",
    )


ACCOUNT = _ws("ggl-5216-google", label="account")
JOB = _ws("ggl-5168-activation", label="job", parent_code=ACCOUNT.code, has_agreement=True)
ROSTER = (ACCOUNT, JOB)
JOB_DIR = Path("1p/google/ggl-5168-activation")


class _Backend(FakeBackend):
    def __init__(self, states, client):
        super().__init__(states)
        self._client = client

    def spine_client(self):
        return self._client


def _client(*cards, commitments=()):
    return FakeClient(
        spine_substance=list(cards),
        projects=[{"id": p.mc2_id, "company_id": CO, "number": 0,
                   "full_job_name": p.code} for p in ROSTER]
        + [{"id": "uuid-ggl-5185-narrative-program", "company_id": CO}],
        companies=[{"id": CO, "name": "Google"}],
        entities=[{"name": "Drew Fiero", "kind": "staff", "archived_at": None},
                  {"name": "Geoff Ahmann", "kind": "freelancer", "archived_at": None}],
        commitments=list(commitments),
    )


def _card(code, slug, framing, body="Dossier.", *, pid=None, scope="project",
          company_id=None, label="v1"):
    return substance_row(code, f"_authored/{slug}", label, project_id=pid or f"uuid-{code}",
                         framing=framing, body=body, layer="Stakeholders",
                         scope=scope, company_id=company_id)


def _sync(root: Path, client, *, roster=ROSTER, config=None):
    if config is None:
        config = make_config(root)
        if hasattr(config, "retire_stakeholder_markdown"):
            config = replace(config, retire_stakeholder_markdown=True)
    return sync_tenant(config,
                       backend_factory=lambda _: _Backend(roster, client), now=_NOW)


def _strip(root: Path) -> str:
    body = (root / JOB_DIR / "cp.md").read_text()
    return body.split("<!-- cp-engine:start stakeholders-strip -->")[1].split(
        "<!-- cp-engine:end stakeholders-strip -->")[0]


def _cards(client, name=None):
    rows = [r for r in client.store["spine_substance"]
            if r.get("layer") == "Stakeholders" and r.get("status") == "live"]
    return [r for r in rows if name is None or name in (r.get("framing") or "")
            or name in (r.get("body") or "")]


# ── 1. the strip is built from cards ────────────────────────────────────


def test_strip_lists_people_who_exist_only_on_cards(tmp_path):
    """A job card and an ACCOUNT card authored on another (inactive)
    workstream of the company both reach the job's strip — neither person is
    in any markdown. Pre-4b the strip read sprint files only and said
    "No stakeholders captured yet"."""
    client = _client(
        _card(JOB.code, "scott-hinkle", "Scott Hinkle — EHS Program Manager",
              "<!-- cp:stakeholder -->\n- **Name:** Scott Hinkle\n- **Email:** sh@google.com\n"
              "- **Side:** client\n<!-- /cp:stakeholder -->\n\nDossier."),
        _card("ggl-5185-narrative-program", "chris-cambria",
              "Chris Cambria — Event Safety owner (NY)", scope="account", company_id=CO),
        _card(JOB.code, "triptych", "Triptych — sub-vendor profile"),
    )
    _sync(tmp_path, client)
    strip = _strip(tmp_path)
    assert "**Scott Hinkle** — EHS Program Manager" in strip
    assert "sh@google.com" in strip
    assert "**Chris Cambria** — Event Safety owner (NY)" in strip and "_account_" in strip
    assert "Triptych" not in strip  # an organisation card is not a person
    assert "from spine cards" in strip


# ── 2. attribution reads cards (incl. account cards and their aliases) ──


def test_attribution_knows_account_cards_and_their_aliases(tmp_path):
    """Rina Lanham's card is account-scope (rendered under the company's
    `_stakeholders/`) and records "Rena Lanham" as an alias. A plan that
    names her by the variant is rewritten, and she is not minted as new.
    Pre-4b attribution never read `_stakeholders/`."""
    from cp_engine.attribution import check_plan_attribution, load_known_people

    acct = tmp_path / "1p" / "google" / "_stakeholders"
    acct.mkdir(parents=True)
    (acct / "rina-lanham.md").write_text(
        "---\nest_item_id: _authored/rina-lanham\nest_item_kind: context\n"
        "binding: unbound\nlayer: Stakeholders\nplacement: context\n---\n"
        "## v1 — 2026-08-01 · live\nframing: Rina Lanham — Chief of Staff to Gretchen\n"
        "sources:\n\n<!-- cp:stakeholder -->\n- **Name:** Rina Lanham\n"
        "- **Aliases:** Rena Lanham\n<!-- /cp:stakeholder -->\n\nDossier.\n"
    )
    job = tmp_path / JOB_DIR
    job.mkdir(parents=True)
    (tmp_path / ".cp-engine.toml").write_text("")
    known = load_known_people(tenant_root=tmp_path, project_dir=job)
    plan = {"projects": {JOB.code: {
        "inbound": [{"text": "Rena Lanham approved the deck.", "who": "Rena Lanham",
                     "date": "2026-09-22"}],
        "stakeholders": [{"name": "Rina Lanham", "role": "Chief of Staff"},
                         {"name": "Pat Quinlan", "role": "new"}],
    }}}
    check_plan_attribution(plan, transcript="", known_for=lambda _c: known)
    block = plan["projects"][JOB.code]
    assert block["inbound"][0]["who"] == "Rina Lanham"
    assert block["inbound"][0]["text"].startswith("Rina Lanham approved")
    assert [s["name"] for s in block["stakeholders"]] == ["Pat Quinlan"]


# ── 3. ingest writes a card, not a bullet ───────────────────────────────


def _ingest_tenant(tmp_path: Path) -> Path:
    from tests.test_ingest import _make_tenant

    tenant = _make_tenant(tmp_path)
    index = {"version": 1, "generated_at": "x", "workstreams": {
        "ggl-5168": {"path": "1p/google/ggl-5168", "parent": None, "has_agreement": True,
                     "label": "job", "mc2_id": JOB.mc2_id, "company": "GGL",
                     "status": "Open"}}}
    (tenant / ".cp-engine").mkdir(exist_ok=True)
    (tenant / ".cp-engine" / "paths.json").write_text(json.dumps(index))
    return tenant


def test_ingest_writes_a_card_not_a_sprint_bullet(tmp_path):
    from cp_engine.ingest import execute_plan
    from tests.test_ingest import _read_sprint_body

    tenant = _ingest_tenant(tmp_path)
    client = _client()
    plan = {"projects": {"ggl-5168": {"stakeholders": [
        {"name": "Laurie\nKellogg", "role": "Creative\nDirector",
         "context": "new to the account"},
        {"name": "Drew", "role": "internal"},       # team member: never a card
    ]}}}
    res = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12), supabase=client)
    assert not res.errors, res.errors
    body = _read_sprint_body(tenant)
    assert "Laurie" not in body
    (row,) = _cards(client)
    assert row["framing"] == "Laurie Kellogg — Creative Director"
    from cp_engine import stakeholders as sh  # after the control's assertions

    d = sh.parse_details(row["body"])
    assert d.name == "Laurie Kellogg" and d.role == "Creative Director"
    assert d.side == "client" and d.company == "Google"
    assert [s["title"] for s in client.store["spine_steps"]] == [
        "Created Laurie Kellogg — Creative Director"]
    # A re-ingest of the same person is matched, never a twin.
    res2 = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 13), supabase=client)
    assert len(_cards(client)) == 1 and res2.skipped_duplicate >= 1


# ── 4. a markdown-only person is migrated once ──────────────────────────


_SECTION = (
    "## Stakeholders\n\n**Google (client)**\n"
    "- **Jennifer Park** — Primary client lead, decision-maker on stickers\n"
    "- **Rena Lanham** — Primary client lead\n\n"
    "**First Person (internal)**\n- **Geoff** — Toolkit designer\n"
    "- **Brandon Grande** — Account lead\n\n"
)


def _seed_section(root: Path) -> Path:
    cp = root / JOB_DIR / "cp.md"
    body = cp.read_text()
    anchor = "## Archive Index"
    cp.write_text(body.replace(anchor, _SECTION + anchor, 1))
    return cp


def test_markdown_only_person_becomes_one_card_and_the_section_retires(tmp_path):
    client = _client(_card("ggl-5185-narrative-program", "rina-lanham",
                           "Rina Lanham — Chief of Staff to Gretchen",
                           scope="account", company_id=CO))
    _sync(tmp_path, client)
    cp = _seed_section(tmp_path)
    _sync(tmp_path, client)

    jennifer = _cards(client, "Jennifer Park")
    assert len(jennifer) == 1
    assert jennifer[0]["project_id"] == JOB.mc2_id
    # Internal people (team roster, MC-2 freelancer, internal group) get no card.
    assert not _cards(client, "Geoff") and not _cards(client, "Brandon")
    body = cp.read_text()
    assert "## Stakeholders\n" not in body
    assert "**Jennifer Park**" in _strip(tmp_path)
    q = list((tmp_path / "exceptions" / "region-edits").glob("*--stakeholders-section--*.md"))
    assert len(q) == 1 and "Jennifer Park" in q[0].read_text()

    # Re-running (the section typed again, the migration re-run) mints no twin.
    _seed_section(tmp_path)
    _sync(tmp_path, client)
    assert len(_cards(client, "Jennifer Park")) == 1


# ── 5. name variants collapse to one card ───────────────────────────────


def test_spelling_variant_collapses_onto_the_existing_card(tmp_path):
    """cp.md says "Rena Lanham"; the card says "Rina Lanham". The import
    records the variant on THAT card (one version), creates no "Rena" card,
    and the next mention under either spelling matches it."""
    client = _client(_card("ggl-5185-narrative-program", "rina-lanham",
                           "Rina Lanham — Chief of Staff to Gretchen",
                           scope="account", company_id=CO))
    # The confirmed pair rides the tenant `[names] aliases` (the migration's
    # canonicalization table proposes exactly these entries); the card then
    # carries the variant itself, so it resolves even without the alias.
    config = make_config(tmp_path)
    config = replace(config, name_aliases={"Rena Lanham": "Rina Lanham"},
                     **({"retire_stakeholder_markdown": True}
                        if hasattr(config, "retire_stakeholder_markdown") else {}))
    _sync(tmp_path, client, config=config)
    _seed_section(tmp_path)
    _sync(tmp_path, client, config=config)
    assert not _cards(client, "Rena Lanham — ")
    (rina,) = [r for r in _cards(client) if "Rina" in r["framing"]]
    assert rina["version_label"] == "v2"
    assert "- **Aliases:** Rena Lanham" in rina["body"]
    from cp_engine import stakeholders as sh  # after the control's assertions

    assert "Rena Lanham" in sh.parse_details(rina["body"]).aliases
    cards = [sh.card_from_row(r) for r in _cards(client)]
    assert sh.match_card(cards, "Rena Lanham").name == "Rina Lanham"
    assert sh.match_card(cards, "Rina").name == "Rina Lanham"
    # Different people are never merged on a shared surname.
    assert sh.match_card(cards + [sh.Card("_authored/t", "x", "Tyler Johnson — dev", "")],
                         "Nate Johnson") is None


def test_sprint_stakeholder_bullet_is_imported_then_removed(tmp_path):
    client = _client()
    _sync(tmp_path, client)
    week = current_sprint_week_iso(_NOW)
    sprint = tmp_path / "sprints" / week / f"{JOB.code}.md"
    body = sprint.read_text()
    assert "### Stakeholders" not in body  # the template no longer scaffolds it
    marker = "### Slack digest\n"
    sprint.write_text(body.replace(
        marker, "### Stakeholders\n- [Bria Holt · Reviewer · closed-window discipline]\n\n"
        + marker, 1))
    _sync(tmp_path, client)
    assert len(_cards(client, "Bria Holt")) == 1
    assert "### Stakeholders" not in sprint.read_text()
    assert list((tmp_path / "exceptions" / "region-edits").glob("*--sprint-stakeholders--*.md"))


def test_details_block_round_trips():
    from cp_engine import stakeholders as sh

    d = sh.Details(name="Jaime Mehra", role="Marketing", company="Infoblox",
                   email="jmehra@infoblox.com", side="client", aliases=("Jamie",),
                   extra=(("LinkedIn", "x"),))
    body = sh.with_details("Prose.\n", d)
    assert body.startswith(sh.DETAILS_START)
    assert sh.parse_details(body) == d
    assert sh.strip_details(body).strip() == "Prose."
    assert sh.parse_details("No block here.") is None


# ── 6. #347: one sync is complete, the next is a no-op ──────────────────


def test_parent_rollup_includes_a_new_child_ask_in_one_sync(tmp_path):
    """The child gains an MC-2 ask. ONE sync puts it in the account's rollup
    (pre-fix: the account rendered first and caught up a sync later), and
    the sync after that writes nothing."""
    client = _client()
    _sync(tmp_path, client)
    asked = (_NOW - timedelta(days=12)).isoformat()
    client.store["commitments"].append({
        "id": "c1", "description": "Send the pop-up element shortlist",
        "status": "open", "project_id": JOB.mc2_id, "owner_name": "Rina Lanham",
        "owner_email": None, "direction": "them_to_us", "due_date": None,
        "date_status": "proposed", "source_kind": "meeting_ingest",
        "source_meeting_id": None, "cp_hash": None, "created_at": asked,
        "updated_at": None,
    })
    _sync(tmp_path, client)
    week = current_sprint_week_iso(_NOW)
    parent = (tmp_path / "sprints" / week / f"{ACCOUNT.code}.md").read_text()
    assert "Send the pop-up element shortlist" in parent
    again = _sync(tmp_path, client)
    sprint_writes = [p for p in again.files_written if "sprints" in str(p)]
    assert sprint_writes == []


def test_sections_stay_until_the_tenant_switches_retirement_on(tmp_path):
    """The rollout gate: before the migration has run, sync neither imports
    nor removes a section (it would meet un-canonicalized spellings first)."""
    client = _client()
    config = make_config(tmp_path)
    _sync(tmp_path, client, config=config)
    cp = _seed_section(tmp_path)
    _sync(tmp_path, client, config=config)
    assert "## Stakeholders\n" in cp.read_text()
    assert not _cards(client, "Jennifer Park")


def test_config_parses_the_retirement_flag(tmp_path):
    from cp_engine.config import load
    from tests.test_config import write_committed, write_local

    write_committed(tmp_path, projects=[], extra="[stakeholders]\nretire_markdown = true")
    write_local(tmp_path, {})
    assert load(tmp_path).retire_stakeholder_markdown is True
