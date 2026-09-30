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
