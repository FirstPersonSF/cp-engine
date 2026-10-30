# tests/test_account_mirror.py — account-scoped elements mirror under the
# ACCOUNT dir (1p/<account>/_stakeholders/), not the provenance project's
# spine/, and the pre-promotion project-side file is cleaned up.
from pathlib import Path

from cp_engine.spine_substance_sync import _mirror_account_elements


def _row(eid, *, version="v1", status="live", scope="account", archived=False):
    return {"id": f"p/{eid}/{version}", "est_item_id": eid,
            "est_item_kind": None, "phase": None, "binding": "unbound",
            "layer": "Stakeholders", "placement": "context", "serves": [],
            "version_label": version, "version_date": "2026-07-11",
            "status": status, "framing": eid, "body": "dossier body",
            "sources": [], "origin": "authored", "scope": scope,
            "archived": archived}


class _Client:
    def __init__(self, company_id, account_rows):
        self._company_id = company_id
        self._account_rows = account_rows

    def table(self, name):
        outer = self

        class _T:
            def __init__(self):
                self._name = name
                self._eqs = {}
            def select(self, c): return self
            def eq(self, c, v): self._eqs[c] = v; return self
            def limit(self, n): return self
            def execute(self):
                if self._name == "projects":
                    data = ([{"company_id": outer._company_id}]
                            if outer._company_id else [])
                else:
                    data = [dict(r) for r in outer._account_rows
                            if self._eqs.get("scope") == "account"]
                return type("R", (), {"data": data})()
        return _T()


def test_account_rows_mirror_under_account_dir(tmp_path):
    project_dir = tmp_path / "1p" / "sap-concur" / "sap-5174-vision"
    project_dir.mkdir(parents=True)
    client = _Client("cid", [_row("_authored/fred")])
    n = _mirror_account_elements(client, project_id="pid", project_code="sap-5174",
                                 project_dir=project_dir)
    assert n == 1
    written = tmp_path / "1p" / "sap-concur" / "_stakeholders" / "fred.md"
    assert written.is_file() and "dossier body" in written.read_text()
    assert not (project_dir / "spine" / "_authored" / "fred.md").exists()


def test_stale_project_side_mirror_is_removed(tmp_path):
    """Step 4c: the pre-promotion copy under the project's spine/_authored/ is
    reaped by the project's own render (account rows are not rendered
    there), within the same sync that writes the account copy."""
    from cp_engine.spine_substance_sync import sync_spine_substance
    from tests._spine_fake import FakeClient, substance_row

    project_dir = tmp_path / "1p" / "sap-concur" / "sap-5174-vision"
    stale = project_dir / "spine" / "_authored" / "fred.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("pre-promotion mirror")
    client = FakeClient(
        spine_substance=[substance_row("sap-5174", "_authored/fred", project_id="pid",
                                       scope="account", company_id="cid")],
        projects=[{"id": "pid", "company_id": "cid"}],
    )
    sync_spine_substance(client, project_id="pid", project_code="sap-5174",
                         project_dir=project_dir)
    assert not stale.exists()
    assert (project_dir.parent / "_stakeholders" / "fred.md").is_file()


def test_archived_account_element_is_skipped(tmp_path):
    project_dir = tmp_path / "1p" / "sap-concur" / "proj"
    project_dir.mkdir(parents=True)
    client = _Client("cid", [_row("_authored/gone", archived=True)])
    n = _mirror_account_elements(client, project_id="pid", project_code="c",
                                 project_dir=project_dir)
    assert n == 0
    assert not (tmp_path / "1p" / "sap-concur" / "_stakeholders").exists()


def test_no_company_is_a_noop(tmp_path):
    project_dir = tmp_path / "firstpersonsf" / "mission-control"
    project_dir.mkdir(parents=True)
    client = _Client(None, [_row("_authored/x")])
    assert _mirror_account_elements(client, project_id="pid", project_code="mc",
                                    project_dir=project_dir) == 0


def test_multi_version_element_mirrors_all_versions(tmp_path):
    project_dir = tmp_path / "1p" / "acme" / "proj"
    project_dir.mkdir(parents=True)
    client = _Client("cid", [
        _row("_authored/fred", version="v2", status="live"),
        _row("_authored/fred", version="v1", status="superseded"),
    ])
    n = _mirror_account_elements(client, project_id="pid", project_code="c",
                                 project_dir=project_dir)
    assert n == 1
    text = (tmp_path / "1p" / "acme" / "_stakeholders" / "fred.md").read_text()
    assert "v2" in text and "v1" in text


def test_deactivation_sweep_skips_underscore_account_dirs(tmp_path):
    """`1p/<account>/_stakeholders/` must never be swept to inactive/ — the
    v0.56.0 field bug: the sweep saw it as an unknown project dir."""
    from cp_engine.sync import _deactivate_stale_cps
    account = tmp_path / "1p" / "sap-concur"
    (account / "_stakeholders").mkdir(parents=True)
    (account / "_stakeholders" / "fred.md").write_text("dossier")
    moved = _deactivate_stale_cps(tmp_path, set())
    assert moved == []
    assert (account / "_stakeholders" / "fred.md").is_file()


def test_demoted_element_file_is_reaped(tmp_path):
    # A file mirrored while the element was account-scoped must vanish once
    # the element leaves the account roster (demote → scope back to project).
    project_dir = tmp_path / "1p" / "sap-concur" / "proj"
    project_dir.mkdir(parents=True)
    gone = tmp_path / "1p" / "sap-concur" / "_stakeholders" / "demoted.md"
    gone.parent.mkdir(parents=True)
    gone.write_text("stale dossier (element demoted)")
    client = _Client("cid", [_row("_authored/kept")])
    n = _mirror_account_elements(client, project_id="pid", project_code="c",
                                 project_dir=project_dir)
    assert n == 1
    assert not gone.exists()
    assert (gone.parent / "kept.md").is_file()


def test_retired_element_file_is_reaped(tmp_path):
    # Archived (retired) elements are skipped by the writer — their existing
    # mirror file must also be REMOVED, not left to linger.
    project_dir = tmp_path / "1p" / "sap-concur" / "proj"
    project_dir.mkdir(parents=True)
    stale = tmp_path / "1p" / "sap-concur" / "_stakeholders" / "gone.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale dossier (element retired)")
    client = _Client("cid", [_row("_authored/gone", archived=True)])
    n = _mirror_account_elements(client, project_id="pid", project_code="c",
                                 project_dir=project_dir)
    assert n == 0
    assert not stale.exists()


def test_reap_spares_non_md_files(tmp_path):
    # Only engine-written *.md mirrors are reconciled; anything else a human
    # parked in the dir survives.
    project_dir = tmp_path / "1p" / "sap-concur" / "proj"
    project_dir.mkdir(parents=True)
    keep = tmp_path / "1p" / "sap-concur" / "_stakeholders" / "notes.txt"
    keep.parent.mkdir(parents=True)
    keep.write_text("human notes")
    client = _Client("cid", [])
    _mirror_account_elements(client, project_id="pid", project_code="c",
                             project_dir=project_dir)
    assert keep.exists()


# ── #197: one malformed element must not strand the rest of the company ──


def test_malformed_element_does_not_abort_the_sweep(tmp_path):
    """A zero-live element (the state a split-scope element produces, #198)
    used to raise out of the loop, stranding every LATER element. On Infoblox
    that left one sibling's mirror stale for 24 days and another's never
    written. The bad element is skipped; its siblings still mirror."""
    project_dir = tmp_path / "1p" / "infoblox" / "ibx-5192"
    project_dir.mkdir(parents=True)
    rows = [
        _row("_authored/aaa-good"),
        # zero live: only a superseded row survived at account scope
        _row("_authored/bbb-broken", version="v1", status="superseded"),
        _row("_authored/ccc-good"),
    ]
    n = _mirror_account_elements(_Client("cid", rows), project_id="pid",
                                 project_code="ibx-5192", project_dir=project_dir)
    stake = tmp_path / "1p" / "infoblox" / "_stakeholders"
    assert (stake / "aaa-good.md").is_file()
    assert (stake / "ccc-good.md").is_file(), "later element was stranded"
    assert not (stake / "bbb-broken.md").exists()
    assert n == 2, "written count must exclude the skipped element"


def test_failed_element_keeps_its_existing_file(tmp_path):
    """The reap deletes every file `expected` does not name. A failing element
    must still claim its filename, or a transient write failure escalates into
    deleting the last good copy of that dossier."""
    project_dir = tmp_path / "1p" / "infoblox" / "ibx-5192"
    project_dir.mkdir(parents=True)
    stake = tmp_path / "1p" / "infoblox" / "_stakeholders"
    stake.mkdir(parents=True)
    good = stake / "bbb-broken.md"
    good.write_text("last known good dossier")
    rows = [_row("_authored/bbb-broken", version="v1", status="superseded")]
    _mirror_account_elements(_Client("cid", rows), project_id="pid",
                             project_code="ibx-5192", project_dir=project_dir)
    assert good.is_file(), "reap deleted the file of an element that failed to write"
    assert good.read_text() == "last known good dossier"


def test_failed_element_keeps_its_project_side_copy(tmp_path):
    """The project-side file is only reaped when the account render can
    produce the element — otherwise a failure leaves it with no file on
    either side."""
    from cp_engine.spine_substance_sync import sync_spine_substance
    from tests._spine_fake import FakeClient, substance_row

    project_dir = tmp_path / "1p" / "infoblox" / "ibx-5192"
    stale = project_dir / "spine" / "_authored" / "bbb-broken.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("pre-promotion mirror")
    client = FakeClient(
        spine_substance=[substance_row("ibx-5192", "_authored/bbb-broken",
                                       project_id="pid", status="superseded",
                                       scope="account", company_id="cid")],
        projects=[{"id": "pid", "company_id": "cid"}],
    )
    sync_spine_substance(client, project_id="pid", project_code="ibx-5192",
                         project_dir=project_dir)
    assert stale.is_file(), "project-side copy dropped despite the account write failing"


# ── #372: an account node's dir IS the company dir; its parent is 1p/ ──


def test_account_node_renders_into_its_own_dir_not_scope_root(tmp_path):
    """An account node's working dir is ``1p/<company>/`` (#302), so its
    parent is ``1p/``. Rendering into ``parent / _stakeholders`` put every
    client company's cards in ONE ``1p/_stakeholders/``, each account sync
    reaping the last (Salesloft's three cards vanished on 10-29 when
    Teleflex synced). The node renders into its own dir."""
    account = tmp_path / "1p" / "salesloft"
    account.mkdir(parents=True)
    n = _mirror_account_elements(_Client("slt", [_row("_authored/leah-ward")]),
                                 project_id="pid", project_code="slt-5219",
                                 project_dir=account, tenant_root=tmp_path)
    assert n == 1
    assert (account / "_stakeholders" / "leah-ward.md").is_file()
    assert not (tmp_path / "1p" / "_stakeholders").exists()


def test_two_account_nodes_do_not_reap_each_other(tmp_path):
    slt = tmp_path / "1p" / "salesloft"
    tel = tmp_path / "1p" / "teleflex"
    slt.mkdir(parents=True)
    tel.mkdir(parents=True)
    _mirror_account_elements(_Client("slt", [_row("_authored/leah-ward")]),
                             project_id="a", project_code="slt-5219",
                             project_dir=slt, tenant_root=tmp_path)
    _mirror_account_elements(_Client("tel", [_row("_authored/brian-olson")]),
                             project_id="b", project_code="tel-5220",
                             project_dir=tel, tenant_root=tmp_path)
    assert (slt / "_stakeholders" / "leah-ward.md").is_file()
    assert (tel / "_stakeholders" / "brian-olson.md").is_file()


def test_program_child_renders_into_company_dir(tmp_path):
    """A job under a program sits two levels below the company dir; its
    cards still converge on ``1p/<company>/_stakeholders/``."""
    job = tmp_path / "1p" / "google" / "ggl-5300-go-safety" / "ggl-5136-site"
    job.mkdir(parents=True)
    _mirror_account_elements(_Client("ggl", [_row("_authored/fred")]),
                             project_id="pid", project_code="ggl-5136",
                             project_dir=job, tenant_root=tmp_path)
    assert (tmp_path / "1p" / "google" / "_stakeholders" / "fred.md").is_file()
    assert not (job.parent / "_stakeholders").exists()


def test_account_dir_for_shapes(tmp_path):
    from cp_engine.spine_substance_sync import account_dir_for

    t = tmp_path
    assert account_dir_for(t / "1p" / "infoblox", t) == t / "1p" / "infoblox"
    assert account_dir_for(t / "1p" / "infoblox" / "ibx-5153", t) == t / "1p" / "infoblox"
    assert (account_dir_for(t / "1p" / "google" / "inactive" / "ggl-5177", t)
            == t / "1p" / "google")
    assert (account_dir_for(t / "canonic" / "cnc-9004" / "cnc-5237", t)
            == t / "canonic")
    # Without tenant_root: located by the scope segment.
    assert account_dir_for(t / "1p" / "salesloft") == t / "1p" / "salesloft"
    # No scope dir on the path: the pre-#372 parent fallback.
    assert account_dir_for(t / "x" / "proj", t) == t / "x"


def test_stray_scope_root_stakeholders_is_reaped(tmp_path):
    """The ``1p/_stakeholders/`` left by pre-#372 syncs: engine-written
    cards go silently, the manifest with them, and the dir is removed."""
    import json

    from cp_engine import region_guard

    stray = tmp_path / "1p" / "_stakeholders"
    stray.mkdir(parents=True)
    text = "engine-written card"
    (stray / "alyssa-teleflex.md").write_text(text)
    (stray / ".generated.json").write_text(
        json.dumps({"alyssa-teleflex.md": region_guard.digest(text)}))
    account = tmp_path / "1p" / "teleflex"
    account.mkdir(parents=True)
    warnings: list[str] = []
    _mirror_account_elements(_Client("tel", [_row("_authored/alyssa-teleflex")]),
                             project_id="pid", project_code="tel-5220",
                             project_dir=account, tenant_root=tmp_path,
                             warnings_out=warnings)
    assert not stray.exists()
    assert (account / "_stakeholders" / "alyssa-teleflex.md").is_file()
    assert any("#372" in w for w in warnings)


def test_self_company_scope_dir_is_not_reaped_as_stray(tmp_path):
    """``canonic/_stakeholders/`` IS a self-company's dir — never stray."""
    job = tmp_path / "canonic" / "cnc-9004" / "cnc-5237"
    job.mkdir(parents=True)
    _mirror_account_elements(_Client("cnc", [_row("_authored/x")]),
                             project_id="pid", project_code="cnc-5237",
                             project_dir=job, tenant_root=tmp_path)
    assert (tmp_path / "canonic" / "_stakeholders" / "x.md").is_file()


def test_account_node_without_tenant_root_still_uses_its_own_dir(tmp_path):
    """The behavioural control for #372 on the pre-fix signature: no
    ``tenant_root`` kwarg, so it FAILS on unfixed code for the bug itself
    (cards in ``1p/_stakeholders/``), not for a missing argument."""
    account = tmp_path / "1p" / "salesloft"
    account.mkdir(parents=True)
    _mirror_account_elements(_Client("slt", [_row("_authored/leah-ward")]),
                             project_id="pid", project_code="slt-5219",
                             project_dir=account)
    assert (account / "_stakeholders" / "leah-ward.md").is_file()
    assert not (tmp_path / "1p" / "_stakeholders").exists()
