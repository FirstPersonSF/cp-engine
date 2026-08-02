"""Task 6 — the important-flip promotion trigger, after the #143 hosted ratchet.

`set_spine_element` was ported to the hosted MCP server and deleted from stdio
(cp-engine #143). Its important false→true RAG-promotion side effect is NOT
mirrored on the hosted copy yet (tracked on #143 — it lands when
`promote_spine_transcript` ports), so the wrapper tests for that trigger went
with the wrapper.

What survives here is the OTHER door onto the same helper: the standalone
`promote_spine_transcript(code, key)` tool, which is the run/retry entry point
for exactly this promotion and still lives on stdio. The arg-shape assertion
below keeps `cp_engine.spine_promote.promote_transcript`'s call contract pinned
so the helper stays wired while the hosted mirror is outstanding.
"""


class _T:
    def __init__(self, captured):
        self._captured = captured

    def select(self, c):
        return self

    def eq(self, c, v):
        return self

    def order(self, *a, **k):
        return self

    def update(self, d):
        self._captured["updates"].append(d)
        return self

    def execute(self):
        return type("R", (), {"data": []})()


class _C:
    def __init__(self, captured):
        self._captured = captured

    def table(self, n):
        return _T(self._captured)


def test_promote_tool_passes_full_arg_shape(monkeypatch):
    """`promote_spine_transcript` reaches promote_transcript with every arg.

    Pins the call contract: project_code, pid and company_id are threaded
    through, and both supabase creds are passed as keywords. A future edit that
    drops or reorders an arg (e.g. stops passing company_id, which the
    engagement-only guard depends on) fails here instead of silently
    degrading promotion to a no-op at runtime.
    """
    import cp_engine.mcp_server as m

    row = {
        "id": "i",
        "est_item_id": "w1",
        "framing": "X",
        "status": "live",
        "important": False,
        "note": None,
        "rel_path": "1p/p/meetings/x.txt",
    }

    fake_client = _C({"updates": []})
    monkeypatch.setattr("cp_engine.mcp_server._resolve",
                        lambda code: (fake_client, "pid", "cid"))
    monkeypatch.setattr("cp_engine.project_sources.resolve_live_element",
                        lambda client, pid, key, cid=None: dict(row))
    monkeypatch.setattr("cp_engine.mcp_server._tenant_root", lambda: "/tenant")
    monkeypatch.setattr("cp_engine.config.load", lambda root: {})
    monkeypatch.setattr("cp_engine.sync_mc2._load_supabase_creds",
                        lambda cfg: ("https://x.supabase.co", "key"))
    monkeypatch.setattr("cp_engine.sync_mc2._load_ingest_creds", lambda cfg: None)

    calls = []

    def _fake(client, root, project_code, pid, company_id, row,
              *, supabase_url, supabase_key):
        calls.append({
            "project_code": project_code, "pid": pid,
            "company_id": company_id, "has_url": bool(supabase_url),
            "has_key": bool(supabase_key),
        })
        return {"ok": True, "ids": ["a1"]}

    monkeypatch.setattr("cp_engine.spine_promote.promote_transcript", _fake)

    res = m.promote_spine_transcript("code", "key")

    assert calls == [{
        "project_code": "code", "pid": "pid", "company_id": "cid",
        "has_url": True, "has_key": True,
    }]
    assert res == {"est_item_id": "w1", "promotion": {"ok": True, "ids": ["a1"]}}
