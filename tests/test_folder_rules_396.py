"""Folder rules for Dropbox / Drive across the workstream tree (#396).

What broke (MC-2, 2026-10-30): Dropbox "on" with no folder silently ingested
nothing; parent nodes had no folder, so account-level material had no home;
and initiatives and Canonic work had no convention at all.

The fakes FILTER (eq / in_), and the Dropbox fake answers only for folders
that exist, so a rule that guesses a path gets nothing back — the property
under test is "never invent a path".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cp_engine import asset_ingest as ai
from cp_engine import daily_health as dh
from cp_engine import folder_rules as fr

# ──────────────────────────────────────────────────────────────────────
#  Fakes
# ──────────────────────────────────────────────────────────────────────


class _Q:
    def __init__(self, rows):
        self.rows, self.f = rows, []

    def select(self, cols, *a, **k):
        assert "*" not in cols
        return self

    def eq(self, col, val):
        self.f.append(lambda r, c=col, v=val: r.get(c) == v)
        return self

    def in_(self, col, vals):
        vals = set(vals)
        self.f.append(lambda r, c=col: r.get(c) in vals)
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return SimpleNamespace(data=[dict(r) for r in self.rows if all(f(r) for f in self.f)])


class _DB:
    def __init__(self, projects, bindings=()):
        self.tables = {"projects": list(projects), "project_integrations": list(bindings)}

    def table(self, name):
        return _Q(self.tables.get(name, []))


def _folder(name, path):
    return SimpleNamespace(name=name, path_display=path)  # no `size` → a folder


def _file(name, path):
    return SimpleNamespace(name=name, path_display=path, size=10, id=f"id:{path}",
                           client_modified="2026-10-30", content_hash=f"h:{path}")


class _Dbx:
    """A Dropbox tree: {folder path: [entries]}. Unknown folders raise
    not_found, like the API."""

    def __init__(self, tree):
        self.tree = {k.lower(): v for k, v in tree.items()}
        self.calls = []

    def files_list_folder(self, path, recursive=False):
        self.calls.append((path, recursive))
        key = path.lower()
        if key not in self.tree:
            raise RuntimeError("ListFolderError('path', LookupError('not_found', None))")
        if not recursive:
            entries = self.tree[key]
        else:
            entries = [e for k, v in self.tree.items()
                       if k == key or k.startswith(key + "/") for e in v]
        return SimpleNamespace(entries=entries, has_more=False, cursor=None)

    def files_get_metadata(self, path):
        parent, _, name = path.rpartition("/")
        for e in self.tree.get(parent.lower(), []):
            if e.name == name:
                return e
        if path.lower() in self.tree:
            return _folder(name, path)
        raise RuntimeError("GetMetadataError('path', LookupError('not_found', None))")


ROOT = "/1P Active Projects"
ACCT = f"{ROOT}/IBX 5217 Infoblox"


def _row(pid, *, number, name, parent=None, kind="client", deal=None, status="Open",
         dbx=True, drive=False, prefix="IBX", company="co-ibx"):
    return {"id": pid, "company_id": company, "number": number, "name": name,
            "full_job_name": f"{prefix} {number} {name}", "parent_id": parent,
            "mc_status": status, "deal_stage": deal, "enable_dropbox": dbx,
            "enable_google_drive": drive, "asset_ingest_folders": None,
            "companies": {"code": prefix, "kind": kind}}


def _bind(pid, path):
    return {"project_id": pid, "service": "dropbox", "label": "", "external_ref": {"url": path}}


def _tree_db():
    acct = _row("acct", number=5217, name="Infoblox")
    job = _row("job", number=5153, name="AI Campaign", parent="acct", deal="Won")
    other = _row("other", number=5167, name="DDI Platform Video", parent="acct", deal="Won")
    return _DB([acct, job, other], [_bind("acct", ACCT)])


def _resolve(db):
    def lookup(pid):
        rows = [r for r in db.tables["projects"] if r["id"] == pid]
        if not rows:
            return None
        from cp_engine.mc2_bindings import fetch_binding_rows, hydrate_project_row

        row = dict(rows[0])
        g = fetch_binding_rows(db, project_ids=[pid])
        return ai._row_to_folders(hydrate_project_row(row, g.get(pid, [])))
    return lookup


# ──────────────────────────────────────────────────────────────────────
#  Matching a workstream's folder among real folders
# ──────────────────────────────────────────────────────────────────────


def test_match_by_exact_name_then_by_number_token():
    names = ["IBX 5153 AI Campaign", "IBX 5167 DDI Platform Video", "Shared"]
    assert fr.match_subfolder(names, number=5153, name="x")[0] == "IBX 5153 AI Campaign"
    assert fr.match_subfolder(["CAN 5239 Canonic Site"], number=5239,
                              name="Canonic Site")[0] == "CAN 5239 Canonic Site"
    assert fr.match_subfolder(["Brand"], number=None, name="brand")[0] == "Brand"


def test_number_is_a_token_not_a_substring():
    assert fr.match_subfolder(["GGL 15153 Other"], number=5153, name="x") == (None, [])


def test_several_candidates_match_nothing_and_say_so():
    match, cands = fr.match_subfolder(["5153 a", "5153 b"], number=5153, name="x")
    assert match is None and cands == ["5153 a", "5153 b"]


# ──────────────────────────────────────────────────────────────────────
#  Rule 1 — inheritance, only when the subfolder exists
# ──────────────────────────────────────────────────────────────────────


def _rules(db, pid, dbx):
    folders = _resolve(db)(pid)
    res = fr.apply_folder_rules(db, folders, connectors={"dropbox": SimpleNamespace(dbx=dbx)},
                                factories={}, lookup=_resolve(db))
    return folders, res


def test_child_inherits_its_subfolder_of_the_parent_folder():
    dbx = _Dbx({ACCT: [_folder("IBX 5153 AI Campaign", f"{ACCT}/IBX 5153 AI Campaign")]})
    folders, res = _rules(_tree_db(), "job", dbx)
    assert folders.mc_dropbox_folder_id == f"{ACCT}/IBX 5153 AI Campaign"
    assert folders.inherited_from == {"dropbox": "ibx-5217-infoblox"}
    [note] = [n for n in res.notes if n.get("kind") == "inherited"]
    assert "ibx-5217-infoblox" in note["note"] and note["source"] == "dropbox"
    assert ai.folders_unconfigured_reason(folders) is None


def test_no_subfolder_inherits_nothing_never_a_constructed_path():
    dbx = _Dbx({ACCT: [_folder("Brand", f"{ACCT}/Brand")]})
    folders, res = _rules(_tree_db(), "job", dbx)
    assert folders.mc_dropbox_folder_id is None and not folders.inherited_from
    assert any("no subfolder" in n["note"] for n in res.notes)
    assert ai.folders_unconfigured_reason(folders) is not None


def test_unreadable_parent_folder_is_a_reason_not_a_crash():
    folders, res = _rules(_tree_db(), "job", _Dbx({}))
    assert folders.mc_dropbox_folder_id is None
    assert any("unreadable" in n["note"] for n in res.notes)


def test_inheritance_walks_up_through_an_unbound_program():
    acct = _row("acct", number=5217, name="Infoblox")
    prog = _row("prog", number=5300, name="Go Safety", parent="acct")
    job = _row("job", number=5136, name="Website", parent="prog", deal="Won")
    db = _DB([acct, prog, job], [_bind("acct", ACCT)])
    pdir = f"{ACCT}/IBX 5300 Go Safety"
    dbx = _Dbx({ACCT: [_folder("IBX 5300 Go Safety", pdir)],
                pdir: [_folder("5136 Website", f"{pdir}/5136 Website")]})
    folders, _ = _rules(db, "job", dbx)
    assert folders.mc_dropbox_folder_id == f"{pdir}/5136 Website"
    assert folders.inherited_from == {"dropbox": "ibx-5300-go-safety"}


def test_a_bound_or_disabled_source_is_never_inherited():
    db = _tree_db()
    db.tables["projects"][1]["enable_dropbox"] = False
    dbx = _Dbx({ACCT: [_folder("IBX 5153 AI Campaign", f"{ACCT}/IBX 5153 AI Campaign")]})
    folders, _ = _rules(db, "job", dbx)
    assert folders.mc_dropbox_folder_id is None and dbx.calls == []


def test_self_company_with_agreement_is_not_a_target():
    db = _DB([_row("acct", number=9004, name="StoryOS", kind="self-canonic", prefix="CNC"),
              _row("job", number=5240, name="Deal", parent="acct", kind="self-canonic",
                   prefix="CNC", deal="Won")],
             [_bind("acct", "/Canonic/CNC 9004 StoryOS")])
    home = "/Canonic/CNC 9004 StoryOS"
    dbx = _Dbx({home: [_folder("5240 Deal", f"{home}/5240 Deal")]})
    folders, res = _rules(db, "job", dbx)
    assert folders.mc_dropbox_folder_id is None and res.notes == [] and dbx.calls == []


# ──────────────────────────────────────────────────────────────────────
#  Rule 2 — a parent node ingests at account scope, minus its children
# ──────────────────────────────────────────────────────────────────────


def _acct_dbx():
    return _Dbx({
        ACCT: [_folder("Brand", f"{ACCT}/Brand"),
               _folder("IBX 5153 AI Campaign", f"{ACCT}/IBX 5153 AI Campaign")],
        f"{ACCT}/Brand": [_file("guidelines.pdf", f"{ACCT}/Brand/guidelines.pdf")],
        f"{ACCT}/IBX 5153 AI Campaign": [
            _file("brief.docx", f"{ACCT}/IBX 5153 AI Campaign/brief.docx")],
    })


def test_parent_node_is_account_scope_and_drops_child_folders():
    dbx = _acct_dbx()
    folders, _ = _rules(_tree_db(), "acct", dbx)
    assert folders.is_parent_node and folders.ingest_scope == "account"
    assert folders.child_dropbox_roots == (f"{ACCT}/IBX 5153 AI Campaign",)
    refs, notes = ai.list_files(folders, dropbox_connector=SimpleNamespace(dbx=dbx),
                                use_cache=False)
    assert [r.name for r in refs] == ["guidelines.pdf"]
    assert any("child workstream folders" in n["note"] for n in notes)


def test_a_program_is_a_parent_node_too():
    db = _DB([_row("prog", number=9004, name="StoryOS", kind="self-canonic", prefix="CNC"),
              _row("kid", number=5239, name="Canonic Site", parent="prog", kind="self-canonic",
                   prefix="CNC")],
             [_bind("prog", "/Canonic/CNC 9004 StoryOS")])
    dbx = _Dbx({"/Canonic/CNC 9004 StoryOS": []})
    folders, _ = _rules(db, "prog", dbx)
    assert not folders.is_account_node and folders.is_parent_node
    assert folders.ingest_scope == "account"


def test_a_child_bound_inside_the_parent_folder_is_dropped_by_its_binding():
    db = _tree_db()
    db.tables["project_integrations"].append(_bind("other", f"{ACCT}/Video/DDI"))
    folders, _ = _rules(db, "acct", _acct_dbx())
    assert f"{ACCT}/Video/DDI" in folders.child_dropbox_roots


def test_a_leaf_job_stays_project_scope():
    db = _tree_db()
    db.tables["project_integrations"].append(_bind("job", f"{ROOT}/IBX 5153 AI Campaign"))
    folders, _ = _rules(db, "job", _Dbx({}))
    assert not folders.is_parent_node and folders.ingest_scope == "project"


def test_drive_children_are_excluded_by_top_level_folder_name():
    f = ai.ProjectFolders("p", "c", "client", "drv", None, True, False,
                          child_drive_folders=("IBX 5153 AI Campaign",))
    refs = [ai.FileRef("drive", "1", "a.pdf", None, None, None, folder_path=("Brand",)),
            ai.FileRef("drive", "2", "b.pdf", None, None, None,
                       folder_path=("IBX 5153 AI Campaign", "01")),
            ai.FileRef("drive", "3", "c.pdf", None, None, None)]
    kept, dropped = fr.exclude_child_folders(f, refs)
    assert [r.id for r in kept] == ["1", "3"] and dropped == 1


# ──────────────────────────────────────────────────────────────────────
#  The ingest entry point uses the rules
# ──────────────────────────────────────────────────────────────────────


def test_ingest_passes_the_confirm_gate_on_an_inherited_folder(monkeypatch):
    db = _tree_db()
    monkeypatch.setattr(ai, "resolve_project_folders_by_id", lambda c, pid: _resolve(db)(pid))
    dbx = _Dbx({ACCT: [_folder("IBX 5153 AI Campaign", f"{ACCT}/IBX 5153 AI Campaign")],
                f"{ACCT}/IBX 5153 AI Campaign": []})
    run = ai.ingest_project_assets("ibx-5153", mc_project_id="job", client=db,
                                   dropbox_connector=SimpleNamespace(dbx=dbx), use_cache=False)
    assert run.unconfigured_reason is None
    assert any(n.get("kind") == "inherited" for n in run.source_notes)
    assert (f"{ACCT}/IBX 5153 AI Campaign", True) in dbx.calls


def test_ingest_refusal_says_why_the_parent_did_not_serve(monkeypatch):
    db = _tree_db()
    monkeypatch.setattr(ai, "resolve_project_folders_by_id", lambda c, pid: _resolve(db)(pid))
    run = ai.ingest_project_assets("ibx-5153", mc_project_id="job", client=db,
                                   dropbox_connector=SimpleNamespace(dbx=_Dbx({ACCT: []})),
                                   use_cache=False)
    assert run.unconfigured_reason and "no subfolder" in run.unconfigured_reason


# ──────────────────────────────────────────────────────────────────────
#  Rule 3 — health
# ──────────────────────────────────────────────────────────────────────


def _health_db():
    return _DB([
        _row("acct", number=5217, name="Infoblox", status="Archived"),
        _row("job", number=5153, name="AI Campaign", parent="acct", deal="Won", drive=True),
        _row("bound", number=5167, name="DDI", deal="Won"),
        _row("off", number=5199, name="Off", deal="Won", dbx=False),
        _row("house", number=9010, name="House", kind="self-fpsf", prefix="1PI", deal="Won"),
        _row("gone", number=5100, name="Gone", deal="Won", status="Archived"),
    ], [_bind("acct", ACCT), _bind("bound", f"{ROOT}/IBX 5167 DDI")])


def test_unbound_lists_enabled_sources_with_no_folder_to_read():
    tree = fr.read_tree(_health_db())
    # job: dropbox inheritable from the (archived) account; drive is not.
    assert fr.unbound_workstreams(tree) == [("ibx-5153-ai-campaign", ["drive"])]


def test_health_line_names_the_workstreams():
    c = dh.check_folders(_health_db())
    assert not c.ok and c.text == "1 unbound"
    assert c.items == ["ibx-5153-ai-campaign · drive"]
    assert len(c.label.split()) <= 3


def test_health_all_bound_and_unreadable():
    db = _DB([_row("bound", number=5167, name="DDI", deal="Won")],
             [_bind("bound", f"{ROOT}/IBX 5167 DDI")])
    assert dh.check_folders(db).ok
    assert dh.check_folders(None).text == "no client"

    class _Boom:
        def table(self, _n):
            raise RuntimeError("down")
    c = dh.check_folders(_Boom())
    assert not c.ok and "down" in c.detail["error"]


def test_health_lists_at_most_ten_then_counts():
    rows = [_row(f"p{i}", number=6000 + i, name=f"J{i}", deal="Won") for i in range(12)]
    c = dh.check_folders(_DB(rows))
    assert len(c.items) == 11 and c.items[-1] == "…and 2 more"


# ──────────────────────────────────────────────────────────────────────
#  Rule 4 — convention audit + printed SQL
# ──────────────────────────────────────────────────────────────────────


def _audit(db, dbx):
    rows = fr.audit_folders(db, connectors={"dropbox": SimpleNamespace(dbx=dbx)})
    return {r.code: r for r in rows}


def test_convention_roots():
    f = lambda kind: SimpleNamespace(company_kind=kind, full_job_name="X 1 Y")  # noqa: E731
    assert fr.suggested_folder(f("client")) == "/1P Active Projects/X 1 Y"
    assert fr.suggested_folder(f("self-fpsf")) == "/1P Initiatives/X 1 Y"
    assert fr.suggested_folder(f("self-canonic")) == "/Canonic/X 1 Y"


def test_audit_reports_exists_found_missing_and_root_missing():
    db = _DB([
        _row("a", number=5151, name="GRC Narrative", prefix="GGL", deal="Won"),
        _row("b", number=5176, name="London Safety Video - Phase I", prefix="GGL", deal="Won"),
        _row("c", number=5218, name="SAP Concur", prefix="SAP"),
        _row("d", number=9005, name="Mission Control", prefix="1PI", kind="self-fpsf"),
        _row("e", number=5239, name="Canonic Site", prefix="CNC", kind="self-canonic"),
    ])
    dbx = _Dbx({ROOT: [_folder("GGL 5151 GRC Narrative", f"{ROOT}/GGL 5151 GRC Narrative"),
                       _folder("GGL 5176 London Safety Video Phase I",
                               f"{ROOT}/GGL 5176 London Safety Video Phase I")],
                "/Canonic": [_folder("CAN 5239 Canonic Site", "/Canonic/CAN 5239 Canonic Site")]})
    rows = _audit(db, dbx)
    assert rows["ggl-5151-grc-narrative"].suggested_check == "exists"
    assert rows["ggl-5176-london-safety-video-phase-i"].suggested_check == \
        f"found: {ROOT}/GGL 5176 London Safety Video Phase I"
    assert rows["sap-5218-sap-concur"].suggested_check == "missing"
    assert rows["1pi-9005-mission-control"].suggested_check == "root missing"
    assert rows["cnc-5239-canonic-site"].found_path == "/Canonic/CAN 5239 Canonic Site"

    sql = "\n".join(fr.fill_sql(list(rows.values())))
    assert "GGL 5151 GRC Narrative" in sql and "GGL 5176 London Safety Video Phase I" in sql
    assert "CAN 5239 Canonic Site" in sql
    assert "SAP Concur" not in sql and "Mission Control" not in sql
    # No unique key on project_integrations: the guard is NOT EXISTS, never ON CONFLICT (42P10).
    assert "ON CONFLICT" not in sql
    assert sql.count("WHERE NOT EXISTS (SELECT 1 FROM public.project_integrations") == 3
    assert sql.count("INSERT INTO public.project_integrations") == 3


def test_a_bound_path_that_does_not_exist_gets_an_update_not_an_insert():
    db = _DB([_row("g", number=5197, name="go/readiness 2026", prefix="GGL", deal="Won")],
             [_bind("g", f"{ROOT}/GGL 5197 go/readiness 2026")])
    dbx = _Dbx({ROOT: [_folder("GGL 5197 go readiness", f"{ROOT}/GGL 5197 go readiness")]})
    rows = _audit(db, dbx)
    r = rows["ggl-5197-go-readiness-2026"]
    assert r.bound_check["dropbox"] == "missing"
    [stmt] = fr.fill_sql([r])
    assert stmt.splitlines()[1].startswith("UPDATE public.project_integrations")
    assert "external_ref->>'url' = '/1P Active Projects/GGL 5197 go/readiness 2026'" in stmt


def test_no_sql_for_out_of_scope_disabled_or_inherited():
    db = _DB([
        _row("acct", number=5217, name="Infoblox"),
        _row("job", number=5153, name="AI Campaign", parent="acct", deal="Won"),
        _row("off", number=5143, name="Off", deal="Won", dbx=False),
        _row("house", number=9010, name="House", kind="self-fpsf", prefix="1PI", deal="Won"),
    ], [_bind("acct", ACCT)])
    dbx = _Dbx({ROOT: [_folder("IBX 5143 Off", f"{ROOT}/IBX 5143 Off"),
                       _folder("IBX 5153 AI Campaign", f"{ROOT}/IBX 5153 AI Campaign")],
                ACCT: [_folder("IBX 5153 AI Campaign", f"{ACCT}/IBX 5153 AI Campaign")],
                "/1P Initiatives": [_folder("1PI 9010 House", "/1P Initiatives/1PI 9010 House")]})
    rows = _audit(db, dbx)
    assert rows["ibx-5153-ai-campaign"].inherited["dropbox"].startswith(f"{ACCT}/IBX 5153")
    assert not rows["1pi-9010-house"].in_scope
    assert fr.fill_sql(list(rows.values())) == []
    text = fr.render_audit(list(rows.values()))
    assert "1pi-9010-house (job, Open) — not an ingest target" in text
    assert "inherited /1P Active Projects/IBX 5217 Infoblox/IBX 5153 AI Campaign" in text


def test_offline_audit_reads_unchecked():
    db = _DB([_row("a", number=5151, name="GRC", deal="Won")])
    [r] = fr.audit_folders(db, connectors={})
    assert r.suggested_check == "unchecked"


def test_cli_folders_prints_audit_and_sql(monkeypatch):
    from click.testing import CliRunner

    import cp_engine.cli as cli

    db = _DB([_row("a", number=5151, name="GRC Narrative", prefix="GGL", deal="Won")])
    dbx = _Dbx({ROOT: [_folder("GGL 5151 GRC Narrative", f"{ROOT}/GGL 5151 GRC Narrative")]})
    monkeypatch.setattr(cli, "build_mc2_client", lambda: db)
    monkeypatch.setattr(ai, "_dropbox_connector", lambda: SimpleNamespace(dbx=dbx))

    def _no_drive():
        raise RuntimeError("no drive creds")
    monkeypatch.setattr(ai, "_drive_connector", _no_drive)
    out = CliRunner().invoke(cli.main, ["folders", "--write-sql"])
    assert out.exit_code == 0, out.output
    assert "ggl-5151-grc-narrative (job, Open)" in out.output
    assert "INSERT INTO public.project_integrations" in out.output
    assert "1 active · 1 with a source on and no folder" in out.output


@pytest.mark.parametrize("kind", ["client", "self-fpsf", "self-canonic"])
def test_every_ingestable_kind_has_a_convention_root(kind):
    assert kind in fr.CONVENTION_ROOTS


# ──────────────────────────────────────────────────────────────────────
#  The Dropbox writers' resolver (deferred from #401)
# ──────────────────────────────────────────────────────────────────────


def _resolver(db, pid, dbx, monkeypatch):
    monkeypatch.setattr(ai, "resolve_project_folders_by_id", lambda c, p: _resolve(db)(p))
    return fr.resolve_dropbox_folder(db, pid, connector_factory=lambda: SimpleNamespace(dbx=dbx))


def test_resolver_inherits_names_the_ancestor_and_reaches_the_file(monkeypatch):
    dbx = _Dbx({ACCT: [_folder("IBX 5153 AI Campaign", f"{ACCT}/IBX 5153 AI Campaign")]})
    got = _resolver(_tree_db(), "job", dbx, monkeypatch)
    assert got.folder == f"{ACCT}/IBX 5153 AI Campaign"
    assert got.inherited_from == "ibx-5217-infoblox" and got.reason is None
    assert got.describe() == {"path": f"{ACCT}/IBX 5153 AI Campaign",
                              "from": "ibx-5217-infoblox"}
    assert got.connector is not None and got.connector.dbx is dbx  # reused by the writer
    path = f"{ACCT}/IBX 5153 AI Campaign/03 Assets/06 Spine/deck.pdf"
    assert ai.ingest_reach_gap(got.folders, path) is None


def test_resolver_bound_folder_lists_nothing(monkeypatch):
    db = _tree_db()
    db.tables["project_integrations"].append(_bind("job", "/Elsewhere/5153"))
    dbx = _Dbx({})
    got = _resolver(db, "job", dbx, monkeypatch)
    assert got.folder == "/Elsewhere/5153" and got.inherited_from is None
    assert got.describe() is None and dbx.calls == []


def test_resolver_without_a_match_says_why(monkeypatch):
    got = _resolver(_tree_db(), "job", _Dbx({ACCT: [_folder("Brand", f"{ACCT}/Brand")]}),
                    monkeypatch)
    assert got.folder is None
    assert got.reason.startswith("no subfolder for this workstream in ibx-5217-infoblox")
    assert ai.ingest_reach_gap(got.folders, "/x/y.pdf") == \
        "the project has no Dropbox folder, bound or inherited"


def test_resolver_missing_row_and_failing_rule(monkeypatch):
    monkeypatch.setattr(ai, "resolve_project_folders_by_id", lambda c, p: None)
    assert fr.resolve_dropbox_folder(object(), "nope").folders is None

    def boom(*a, **k):
        raise RuntimeError("rules down")
    db = _tree_db()
    db.tables["project_integrations"].append(_bind("job", "/Elsewhere/5153"))
    monkeypatch.setattr(ai, "resolve_project_folders_by_id", lambda c, p: _resolve(db)(p))
    monkeypatch.setattr(fr, "apply_folder_rules", boom)
    got = fr.resolve_dropbox_folder(db, "job")
    assert got.folder == "/Elsewhere/5153"  # the bound folder stands
    assert "rules down" in got.notes[0]["note"]


def test_parent_node_file_under_a_child_folder_is_not_queued(monkeypatch):
    child = f"{ACCT}/IBX 5153 AI Campaign"
    dbx = _Dbx({ACCT: [_folder("IBX 5153 AI Campaign", child)]})
    got = _resolver(_tree_db(), "acct", dbx, monkeypatch)
    assert got.folders.ingest_scope == "account"
    assert ai.ingest_reach_gap(got.folders, f"{ACCT}/03 Assets/brand.pdf") is None
    gap = ai.ingest_reach_gap(got.folders, f"{child}/deck.pdf")
    assert gap.startswith(f"{child}/deck.pdf is inside a child workstream's folder")


def test_reach_gap_disabled_is_not_the_same_as_unbound():
    f = ai.ProjectFolders(project_id="p", company_id="c", company_kind="client",
                          google_drive_folder_id=None, mc_dropbox_folder_id="/a",
                          enable_google_drive=False, enable_dropbox=False)
    assert ai.ingest_reach_gap(f, "/a/x.pdf") == \
        "the project's Dropbox source is not enabled in MC-2"


# ──────────────────────────────────────────────────────────────────────
#  Audit: a bound Drive folder is checked (deferred from #401)
# ──────────────────────────────────────────────────────────────────────


class _HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"<HttpError {status}>")
        self.resp = SimpleNamespace(status=status)


class _Drive:
    """`service.files().get(...).execute()` over {id: metadata}; read-only."""

    def __init__(self, files, error=None):
        self.meta, self.error, self.gets = files, error, []
        self.service = self

    def files(self):
        return self

    def get(self, fileId, fields, supportsAllDrives):
        assert supportsAllDrives is True
        self.gets.append(fileId)
        return SimpleNamespace(execute=lambda: self._get(fileId))

    def _get(self, file_id):
        if self.error:
            raise self.error
        if file_id not in self.meta:
            raise _HttpError(404)
        return self.meta[file_id]


def _drive_db():
    rows = [_row(p, number=n, name=p, deal="Won", dbx=False, drive=True)
            for p, n in (("ok", 5101), ("gone", 5102), ("bin", 5103), ("doc", 5104))]
    binds = [{"project_id": p, "service": "google_drive", "label": "",
              "external_ref": {"id": f"fid-{p}"}} for p in ("ok", "gone", "bin", "doc")]
    return _DB(rows, binds)


def test_audit_checks_bound_drive_folders(monkeypatch):
    from cp_engine.asset_ingest import _DRIVE_FOLDER_MIME

    db = _drive_db()
    tree = fr.read_tree(db)
    assert all(f.google_drive_folder_id for f in tree.values())
    drive = _Drive({
        "fid-ok": {"id": "fid-ok", "mimeType": _DRIVE_FOLDER_MIME, "trashed": False},
        "fid-bin": {"id": "fid-bin", "mimeType": _DRIVE_FOLDER_MIME, "trashed": True},
        "fid-doc": {"id": "fid-doc", "mimeType": "application/pdf", "trashed": False},
    })
    rows = {r.code: r for r in fr.audit_folders(db, connectors={"drive": drive}, tree=tree)}
    checks = {c.split("-")[1]: r.bound_check["drive"] for c, r in rows.items()}
    assert checks == {"5101": "exists", "5102": "not found", "5103": "trashed",
                      "5104": "not a folder"}
    text = fr.render_audit(list(rows.values()))
    assert "bound fid-gone [not found]" in text


def test_audit_drive_unchecked_offline_or_on_other_errors():
    db = _drive_db()
    tree = fr.read_tree(db)
    offline = fr.audit_folders(db, connectors={}, tree=tree)
    assert {r.bound_check["drive"] for r in offline} == {"unchecked"}
    flaky = fr.audit_folders(db, connectors={"drive": _Drive({}, error=_HttpError(500))},
                             tree=tree)
    assert {r.bound_check["drive"] for r in flaky} == {"unchecked"}
