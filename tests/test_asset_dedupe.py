"""Tests for `cp_engine.asset_dedupe` — the #57 backlog cleanup planner.

Planning is pure (plain row lists in, DedupeGroup plan out) so most tests need
no client. `apply_dedupe` gets a small fake recording updates/deletes; the
dry-run test proves the CLI's default path never writes.
"""

from __future__ import annotations

from click.testing import CliRunner

from cp_engine.asset_dedupe import (
    DedupeGroup,
    apply_dedupe,
    plan_dedupe,
    spine_referenced_asset_ids,
)


def _asset(id_, title, created, *, project_id="p-1", initiative_id=None, prev=None):
    return {
        "id": id_,
        "title": title,
        "project_id": project_id,
        "initiative_id": initiative_id,
        "file_hash": f"hash-{id_}",
        "created_at": created,
        "prev_asset_id": prev,
    }


# ──────────────────────────────────────────────────────────────────────
#  plan_dedupe (dry-run grouping logic)
# ──────────────────────────────────────────────────────────────────────


def test_groups_same_owner_case_insensitive_title_keep_newest():
    assets = [
        _asset("a1", "Brief.PDF", "2026-06-01T00:00:00Z"),
        _asset("a2", "brief.pdf", "2026-07-01T00:00:00Z"),
        _asset("a3", "Brief.pdf", "2026-05-01T00:00:00Z"),
        _asset("b1", "Other doc", "2026-06-01T00:00:00Z"),  # singleton — no group
    ]

    groups = plan_dedupe(assets, referenced_ids=set())

    assert len(groups) == 1
    g = groups[0]
    assert g.owner_col == "project_id" and g.owner_id == "p-1"
    assert g.keeper["id"] == "a2"  # newest wins
    assert [r["id"] for r in g.losers] == ["a1", "a3"]  # newest → oldest
    assert g.blocked_refs == [] and not g.blocked


def test_same_title_different_owner_never_groups():
    assets = [
        _asset("a1", "Brief.pdf", "2026-06-01T00:00:00Z", project_id="p-1"),
        _asset("a2", "Brief.pdf", "2026-07-01T00:00:00Z", project_id="p-2"),
        _asset(
            "a3",
            "Brief.pdf",
            "2026-07-02T00:00:00Z",
            project_id=None,
            initiative_id="i-1",
        ),
    ]
    assert plan_dedupe(assets, referenced_ids=set()) == []


def test_untitled_or_unowned_rows_are_skipped():
    assets = [
        _asset("a1", "", "2026-06-01T00:00:00Z"),
        _asset("a2", "", "2026-07-01T00:00:00Z"),
        _asset("a3", "Doc", "2026-06-01T00:00:00Z", project_id=None),
        _asset("a4", "Doc", "2026-07-01T00:00:00Z", project_id=None),
    ]
    assert plan_dedupe(assets, referenced_ids=set()) == []


def test_spine_referenced_loser_blocks_the_group():
    assets = [
        _asset("new", "Brief.pdf", "2026-07-01T00:00:00Z"),
        _asset("old", "Brief.pdf", "2026-06-01T00:00:00Z"),
    ]

    groups = plan_dedupe(assets, referenced_ids={"old"})

    assert len(groups) == 1
    g = groups[0]
    assert g.blocked
    assert [r["id"] for r in g.blocked_refs] == ["old"]
    assert g.losers == []  # nothing actionable in a blocked group


def test_spine_referenced_keeper_does_not_block():
    # Only OLDER copies being cited is a problem; the keeper staying
    # referenced is the desired end state.
    assets = [
        _asset("new", "Brief.pdf", "2026-07-01T00:00:00Z"),
        _asset("old", "Brief.pdf", "2026-06-01T00:00:00Z"),
    ]
    groups = plan_dedupe(assets, referenced_ids={"new"})
    assert not groups[0].blocked
    assert [r["id"] for r in groups[0].losers] == ["old"]


# ──────────────────────────────────────────────────────────────────────
#  spine_referenced_asset_ids
# ──────────────────────────────────────────────────────────────────────


def test_spine_referenced_ids_reads_typed_refs_only():
    rows = [
        {"id": "e1", "sources": [{"type": "rag_asset", "id": "a-1"}]},
        {"id": "e2", "sources": ["plain-string-ref.pdf"]},  # no id — ignored
        {"id": "e3", "sources": [{"type": "other", "id": "a-2"}]},  # wrong type
        {"id": "e4", "sources": None},
        {"id": "e5", "sources": "malformed"},
    ]
    assert spine_referenced_asset_ids(rows) == {"a-1"}


# ──────────────────────────────────────────────────────────────────────
#  apply_dedupe
# ──────────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, client, table):
        self._c = client
        self._table = table
        self._mode = None
        self._filters = {}

    def update(self, payload):
        self._mode = ("update", payload)
        return self

    def delete(self):
        self._mode = ("delete",)
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def execute(self):
        if self._mode[0] == "update":
            self._c.updates.append(
                {
                    "table": self._table,
                    "payload": self._mode[1],
                    "filters": dict(self._filters),
                }
            )
        else:
            self._c.deletes.append(
                {"table": self._table, "filters": dict(self._filters)}
            )
        return _Resp([])


class _FakeClient:
    def __init__(self):
        self.updates = []
        self.deletes = []

    def table(self, name):
        return _FakeQuery(self, name)


def _group(**overrides):
    base = dict(
        owner_col="project_id",
        owner_id="p-1",
        title="Brief.pdf",
        keeper=_asset("new", "Brief.pdf", "2026-07-01T00:00:00Z"),
        losers=[
            _asset("old", "Brief.pdf", "2026-06-01T00:00:00Z"),
            _asset("older", "Brief.pdf", "2026-05-01T00:00:00Z"),
        ],
        blocked_refs=[],
    )
    base.update(overrides)
    return DedupeGroup(**base)


def test_apply_chains_keeper_and_retires_losers():
    client = _FakeClient()

    counts = apply_dedupe(client, [_group()])

    assert counts == {"groups": 1, "retired": 2, "chained": 1, "blocked": 0}
    # Keeper chained to the NEWEST loser.
    chain = client.updates[0]
    assert chain == {
        "table": "rag_assets",
        "payload": {"prev_asset_id": "old"},
        "filters": {"id": "new"},
    }
    # Each loser: superseded + chunks deleted (embeddings cascade).
    retired = [
        u for u in client.updates if u["payload"] == {"status": "superseded"}
    ]
    assert [u["filters"]["id"] for u in retired] == ["old", "older"]
    assert [d["filters"]["asset_id"] for d in client.deletes] == ["old", "older"]
    assert all(d["table"] == "asset_chunks" for d in client.deletes)


def test_apply_never_clobbers_an_existing_chain():
    keeper = _asset("new", "Brief.pdf", "2026-07-01T00:00:00Z", prev="already")
    client = _FakeClient()

    counts = apply_dedupe(client, [_group(keeper=keeper)])

    assert counts["chained"] == 0
    assert not any(
        "prev_asset_id" in u["payload"] for u in client.updates
    )


def test_apply_skips_blocked_groups_entirely():
    blocked = _group(
        losers=[],
        blocked_refs=[_asset("old", "Brief.pdf", "2026-06-01T00:00:00Z")],
    )
    client = _FakeClient()

    counts = apply_dedupe(client, [blocked])

    assert counts == {"groups": 0, "retired": 0, "chained": 0, "blocked": 1}
    assert client.updates == [] and client.deletes == []


# ──────────────────────────────────────────────────────────────────────
#  CLI: dry-run by default (no writes without --apply)
# ──────────────────────────────────────────────────────────────────────


def test_cli_dry_run_reports_but_never_writes(monkeypatch):
    from cp_engine.cli import main

    client = _FakeClient()
    monkeypatch.setattr("cp_engine.cli.build_mc2_client", lambda: client)
    monkeypatch.setattr(
        "cp_engine.asset_dedupe.fetch_active_assets",
        lambda c: [
            _asset("new", "Brief.pdf", "2026-07-01T00:00:00Z"),
            _asset("old", "Brief.pdf", "2026-06-01T00:00:00Z"),
            _asset("held", "Deck.pdf", "2026-07-01T00:00:00Z"),
            _asset("held-old", "Deck.pdf", "2026-06-01T00:00:00Z"),
        ],
    )
    monkeypatch.setattr(
        "cp_engine.asset_dedupe.fetch_spine_referenced_asset_ids",
        lambda c: {"held-old"},
    )

    result = CliRunner().invoke(main, ["assets-dedupe"])

    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert "retire old" in result.output
    assert "HOLD   held-old" in result.output
    assert "BLOCKED" in result.output
    # Pure read: no update/delete ever issued.
    assert client.updates == [] and client.deletes == []


def test_cli_apply_executes_the_plan(monkeypatch):
    from cp_engine.cli import main

    client = _FakeClient()
    monkeypatch.setattr("cp_engine.cli.build_mc2_client", lambda: client)
    monkeypatch.setattr(
        "cp_engine.asset_dedupe.fetch_active_assets",
        lambda c: [
            _asset("new", "Brief.pdf", "2026-07-01T00:00:00Z"),
            _asset("old", "Brief.pdf", "2026-06-01T00:00:00Z"),
        ],
    )
    monkeypatch.setattr(
        "cp_engine.asset_dedupe.fetch_spine_referenced_asset_ids",
        lambda c: set(),
    )

    result = CliRunner().invoke(main, ["assets-dedupe", "--apply"])

    assert result.exit_code == 0, result.output
    assert "1 asset(s) retired" in result.output
    assert any(u["payload"] == {"status": "superseded"} for u in client.updates)
    assert [d["filters"]["asset_id"] for d in client.deletes] == ["old"]
