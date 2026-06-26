"""Task 6 — set_spine_element promotes the source transcript on important false→true.

Marking an element important (a genuine false→true transition) ALSO embeds its
source transcript into RAG. Importance is ALWAYS set first; promotion is
non-fatal (a failure surfaces as promotion:{ok:False}, never a tool {error}).
"""


def _row(important):
    return {
        "id": "p/_authored/x/v1",
        "est_item_id": "_authored/x",
        "framing": "X",
        "status": "live",
        "important": important,
        "note": None,
        "rel_path": "1p/p/meetings/x.txt",
    }


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


def _wire(monkeypatch, *, prior_important, promote_result=None):
    """Wire set_spine_element's collaborators. Returns (captured, calls)."""
    captured = {"updates": []}
    calls = []
    monkeypatch.setattr("cp_engine.mcp_server._resolve",
                        lambda code: (_C(captured), "pid", "cid"))
    monkeypatch.setattr("cp_engine.project_sources.resolve_live_element",
                        lambda client, pid, key: _row(prior_important))
    monkeypatch.setattr("cp_engine.mcp_server._tenant_root", lambda: "/tenant")
    monkeypatch.setattr("cp_engine.config.load", lambda root: {})
    monkeypatch.setattr("cp_engine.sync_mc2._load_supabase_creds",
                        lambda cfg: ("https://x.supabase.co", "key"))

    def _fake_promote(client, root, project_code, pid, company_id, row, **kw):
        calls.append({"company_id": company_id, "row": row, "kw": kw})
        return promote_result if promote_result is not None else {"ok": True}

    monkeypatch.setattr("cp_engine.spine_promote.promote_transcript", _fake_promote)
    return captured, calls


def test_false_to_true_fires_promotion(monkeypatch):
    captured, calls = _wire(monkeypatch, prior_important=False)
    import cp_engine.mcp_server as m
    res = m.set_spine_element("p", "_authored/x", important=True)
    # importance ALWAYS set
    assert {"important": True} in captured["updates"]
    assert res["important"] is True
    # promotion fired with company_id + the row
    assert len(calls) == 1
    assert calls[0]["company_id"] == "cid"
    assert calls[0]["row"]["est_item_id"] == "_authored/x"
    # result threaded under "promotion"
    assert res["promotion"] == {"ok": True}


def test_already_true_no_promotion(monkeypatch):
    captured, calls = _wire(monkeypatch, prior_important=True)
    import cp_engine.mcp_server as m
    res = m.set_spine_element("p", "_authored/x", important=True)
    assert res["important"] is True
    assert calls == []                  # no redundant re-embed
    assert "promotion" not in res       # common-path return shape preserved


def test_important_false_no_promotion(monkeypatch):
    captured, calls = _wire(monkeypatch, prior_important=True)
    import cp_engine.mcp_server as m
    res = m.set_spine_element("p", "_authored/x", important=False)
    assert res["important"] is False
    assert calls == []
    assert "promotion" not in res


def test_important_none_note_only_no_promotion(monkeypatch):
    captured, calls = _wire(monkeypatch, prior_important=False)
    import cp_engine.mcp_server as m
    res = m.set_spine_element("p", "_authored/x", note="x")
    assert calls == []                  # important untouched (None) → no promotion
    assert "promotion" not in res


def test_promotion_failure_is_non_fatal(monkeypatch):
    captured, calls = _wire(
        monkeypatch, prior_important=False,
        promote_result={"ok": False, "reason": "transcript file not found"},
    )
    import cp_engine.mcp_server as m
    res = m.set_spine_element("p", "_authored/x", important=True)
    # importance STILL set despite a failed promotion
    assert res["important"] is True
    assert "error" not in res
    assert res["promotion"] == {"ok": False, "reason": "transcript file not found"}
