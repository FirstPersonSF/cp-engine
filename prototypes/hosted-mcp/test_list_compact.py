"""Hosted `list_spine_elements` accepts `compact`, like the stdio verb.

WHAT BROKE. Round-1 strictness (#318) made hosted tools reject unknown
arguments instead of dropping them. The tenant's `legend-copywriting` skill
orients with `list_spine_elements(project_code, tier="working", compact=true)`
— valid on stdio `cxp mcp`, where `compact` exists, but unknown on hosted. So
the strictness that fixed silent drops turned that call into a hard failure
for every hosted-only session running the skill.

WHAT THESE PIN. `compact` exists on hosted, trims the per-row detail, and
keeps identity and markers; the default is the full row.

    python -m pytest prototypes/hosted-mcp/test_list_compact.py -v
"""
from __future__ import annotations

from test_project_status_reads import LIVE, db, server  # noqa: F401 — fixtures


def test_the_skill_call_shape_is_accepted(server, db):  # noqa: F811
    out = server.list_spine_elements(LIVE, tier="working", compact=True)
    assert "error" not in out
    assert [e["slug"] for e in out["elements"]] == ["_authored/brief"]


def test_compact_drops_detail_and_keeps_identity(server, db):  # noqa: F811
    row = server.list_spine_elements(LIVE, compact=True)["elements"][0]
    assert not {"status", "version_date", "synced_at", "actor"} & row.keys()
    assert {"slug", "framing", "layer", "binding", "important"} <= row.keys()


def test_the_default_is_the_full_row(server, db):  # noqa: F811
    row = server.list_spine_elements(LIVE)["elements"][0]
    assert {"status", "version_date"} <= row.keys()
