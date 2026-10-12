"""Two small fixes found by the 2026-09-30 agents.

- `--projects ggl-5188` found 0 projects: the prep/agenda filter matched full
  codes only, though the tenant protocol says short codes resolve.
- `cxp stub-sweep`'s arrival-date lookup put `_authored/<slug>` source ids in a
  uuid `in_` filter; PostgREST failed the whole query (22P02), the sweep
  swallowed it, and the arrived-after check was silent on ibx-5153.
"""

from types import SimpleNamespace

from cp_engine.stub_sweep import rag_asset_ids
from cp_engine.state import select_codes

U1 = "3f2b9c1e-8a4d-4f6b-9c2e-1a2b3c4d5e6f"
U2 = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"


def test_short_code_selects_the_full_code_project():
    ps = [SimpleNamespace(code="ggl-5188-calendar-maintenance"),
          SimpleNamespace(code="ggl-5168-activation")]
    assert [p.code for p in select_codes(ps, ("ggl-5188",))] == ["ggl-5188-calendar-maintenance"]
    assert [p.code for p in select_codes(ps, ("GGL-5168-activation",))] == ["ggl-5168-activation"]


def test_authored_source_ids_are_kept_out_of_the_uuid_lookup():
    rows = [
        {"sources": [{"id": U1}, {"id": "_authored/marcello-s-case/v1"}]},
        {"sources": [{"id": U2}, {"id": U1}, "not-a-dict", {"title": "no id"}]},
    ]
    assert rag_asset_ids(rows) == sorted([U1, U2])


# ── the command, not just the helper (#357) ─────────────────────────────────
# The helper test above passes whether or not `stub-sweep` calls it. This one
# drives the command against a client that answers like PostgREST: a non-uuid
# in a uuid `in_` fails the WHOLE query (22P02). Unfixed, the sweep swallowed
# that and the arrived-after check went silent for every stub.

import re as _re

from click.testing import CliRunner

_UUID = _re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class _Q:
    def __init__(self, table, data):
        self.table, self.data, self.ids = table, data, None

    def select(self, _cols):
        return self

    def eq(self, *_a):
        return self

    def in_(self, col, vals):
        if self.table == "rag_assets" and col == "id":
            bad = [v for v in vals if not _UUID.match(str(v))]
            if bad:
                raise RuntimeError(
                    f"22P02 invalid input syntax for type uuid: {bad[0]!r}"
                )
            self.ids = set(vals)
        return self

    def execute(self):
        rows = self.data
        if self.ids is not None:
            rows = [r for r in rows if r["id"] in self.ids]
        return SimpleNamespace(data=rows)


class _Client:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Q(name, self.tables.get(name, []))


def test_stub_sweep_dates_uuid_sources_despite_authored_ids(monkeypatch, tmp_path):
    import cp_engine.cli as cli
    import cp_engine.spine as spine_mod
    from cp_engine import mc2_db
    from cp_engine.cli_cmds import spine as spine_cmds

    stub = {
        "est_item_id": "_authored/marcello-grande", "framing": "Marcello Grande",
        "layer": "Source material", "status": "live", "archived": False,
        "body": f"Ingested document: **Marcello Grande** (doc)\n\nrag_asset: `{U1}`",
        "serves": ["_authored/deck-build"], "version_date": "2026-06-20",
        "sources": [{"id": U1, "type": "rag_asset", "title": "Marcello Grande"}],
    }
    # The activity carries a hand-authored source — the shape that broke the
    # lookup on ibx-5153.
    act = {
        "est_item_id": "_authored/deck-build", "framing": "Deck build",
        "layer": "Activity", "status": "live", "archived": False, "body": "",
        "serves": [], "version_date": "2026-06-27",
        "sources": [{"id": "_authored/marcello-s-case/v1", "type": "authored"}],
    }
    client = _Client({
        "spine_substance": [stub, act],
        "spine_relations": [],
        "rag_assets": [{"id": U1, "created_at": "2026-07-21T10:00:00+00:00"}],
    })
    monkeypatch.setattr(cli, "_load_config_or_die",
                        lambda: SimpleNamespace(root=tmp_path))
    monkeypatch.setattr(mc2_db, "get_client", lambda _c: client)
    monkeypatch.setattr(spine_cmds, "_resolve_project_id_loud",
                        lambda *_a, **_k: "pid")

    def _no_dir(*_a, **_k):
        raise spine_mod.SpineDirNotFound("none")

    monkeypatch.setattr(spine_mod, "find_spine_dir", _no_dir)

    result = CliRunner().invoke(spine_cmds.stub_sweep_cmd, ["ibx-5153"])
    assert result.exit_code == 0, result.output
    assert "could not resolve source arrival dates" not in result.output
    assert "DOC ARRIVED AFTER" in result.output
