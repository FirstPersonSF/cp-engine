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
    project_dir = tmp_path / "1p" / "sap-concur" / "sap-5174-vision"
    stale = project_dir / "spine" / "_authored" / "fred.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("pre-promotion mirror")
    client = _Client("cid", [_row("_authored/fred")])
    _mirror_account_elements(client, project_id="pid", project_code="sap-5174",
                             project_dir=project_dir)
    assert not stale.exists()


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
