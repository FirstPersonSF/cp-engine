# tests/test_preflight_spine_context.py — the stdio `preflight` verb reads the
# spine's titles from `framing`, the field spine rows actually carry (#332).
#
# Before: the verb read `row.get("title")` from each `list_spine` row. Spine
# rows have no `title` — spine_substance's title column is `framing` (mc-2 mig
# 063) and `list_spine` returns it under that name — so the lookup returned
# nothing for every row and preflight's "spine titles" context was always
# empty. The report never said `spine (N elements)` in `sources_read`, and no
# spine framing ever reached the shape, funding or found-fields passes.
#
# The rows here are built from the REAL listing column set
# (`mc2_db.SPINE_LIST_COLUMNS`) and go through the REAL `list_spine`, so the
# test cannot pass on a hand-written row shape that happens to carry `title`.
from types import SimpleNamespace

import cp_engine.config as config_mod
import cp_engine.mcp_server as srv
import cp_engine.project_sources as ps
from cp_engine.mc2_db import SPINE_LIST_COLUMNS


def _row(eid, framing):
    row = {c.strip(): None for c in SPINE_LIST_COLUMNS.split(",")}
    row.update(est_item_id=eid, framing=framing, layer="Note",
               binding="unbound", status="live", serves=[], body="b",
               archived=False, scope="project", project_id="p1",
               version_label="v1", version_date="2026-09-01")
    return row


class _Client:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        rows = self.rows if name == "spine_substance" else []

        class _T:
            def select(self, cols):
                assert "*" not in cols
                return self
            def eq(self, c, v): return self
            def in_(self, c, v): return self
            def order(self, *a, **k): return self
            def execute(self):
                return SimpleNamespace(data=[dict(r) for r in rows])
        return _T()


def test_preflight_reads_spine_framings(monkeypatch, tmp_path):
    rows = [_row("_authored/brief", "Creative brief — 2027 brand video set"),
            _row("_authored/deck", "Launch deck :30 and :15 cutdowns")]
    monkeypatch.setattr(srv, "_tenant_root", lambda: tmp_path)
    monkeypatch.setattr(config_mod, "load", lambda root: SimpleNamespace(root=tmp_path))
    monkeypatch.setattr(srv, "_resolve", lambda code: (_Client(rows), "p1", None))
    monkeypatch.setattr(ps, "list_sources", lambda *a, **k: [])

    captured = {}
    import cp_engine.preflight as pf
    real = pf.run_preflight

    def spy(*a, **kw):
        captured.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(pf, "run_preflight", spy)

    out = srv.preflight("sap-5198", "rfp")

    assert "error" not in out, out
    assert captured["spine_titles"] == [r["framing"] for r in rows]
    assert "spine (2 elements)" in out["sources_read"]
