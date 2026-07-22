# tests/test_spine_steps.py — #119 add/set/reorder/remove_spine_step
import cp_engine.spine_steps as steps
import cp_engine.mcp_server as srv


class _FakeSteps:
    """A stateful fake of the spine_steps table over the supabase-py fluent
    builder. Holds rows in a list; supports insert/update/delete + the
    select().eq().eq().order().execute() read the helpers issue. Filters build up
    across chained .eq() calls; the terminal op (insert/update/delete/execute)
    applies them."""

    def __init__(self):
        self.rows = []
        self._auto = 0

    # --- fluent builder ---
    def table(self, name):
        assert name == "spine_steps"
        return _Query(self)


class _Query:
    def __init__(self, store):
        self.store = store
        self.filters = {}
        self._op = None
        self._patch = None
        self._insert = None

    def select(self, cols):
        self._op = "select"
        return self

    def insert(self, row):
        self._op = "insert"
        self._insert = row
        return self

    def update(self, patch):
        self._op = "update"
        self._patch = patch
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def order(self, col):
        self._order = col
        return self

    def _match(self, r):
        return all(r.get(k) == v for k, v in self.filters.items())

    def execute(self):
        st = self.store
        if self._op == "insert":
            st._auto += 1
            row = {"id": f"auto{st._auto}", "step_date": None, "note": None,
                   "status": "upcoming", **self._insert}
            st.rows.append(row)
            data = [row]
        elif self._op == "update":
            data = []
            for r in st.rows:
                if self._match(r):
                    r.update(self._patch)
                    data.append(r)
        elif self._op == "delete":
            keep = [r for r in st.rows if not self._match(r)]
            data = [r for r in st.rows if self._match(r)]
            st.rows = keep
        else:  # select
            data = [r for r in st.rows if self._match(r)]
            data = sorted(data, key=lambda r: r.get("position") or 0)
        return type("R", (), {"data": data})()


def _resolve(monkeypatch, est_item_id="_authored/sow"):
    monkeypatch.setattr(
        steps, "_resolve_est_item_id",
        lambda client, pid, key, company_id=None: est_item_id)


def test_add_appends_at_next_position(monkeypatch):
    _resolve(monkeypatch)
    c = _FakeSteps()
    out1 = steps.add_step(c, "pid", "sow", "first")
    out2 = steps.add_step(c, "pid", "sow", "second", status="active",
                          step_date="7/16", note="a note")
    assert out1["position"] == 1 and out2["position"] == 2
    titles = [s["title"] for s in out2["steps"]]
    assert titles == ["first", "second"]
    assert out2["steps"][1]["status"] == "active"
    assert out2["steps"][1]["step_date"] == "7/16"


def test_add_rejects_bad_status_and_blank_title(monkeypatch):
    _resolve(monkeypatch)
    c = _FakeSteps()
    assert "error" in steps.add_step(c, "pid", "sow", "x", status="bogus")
    assert "error" in steps.add_step(c, "pid", "sow", "   ")
    assert c.rows == []


def test_add_rejects_overlong_note(monkeypatch):
    _resolve(monkeypatch)
    c = _FakeSteps()
    out = steps.add_step(c, "pid", "sow", "x", note="z" * 8001)
    assert "error" in out and "8000" in out["error"]


def test_add_unresolved_element_errors(monkeypatch):
    monkeypatch.setattr(steps, "_resolve_est_item_id",
                        lambda *a, **k: None)
    out = steps.add_step(_FakeSteps(), "pid", "nope", "x")
    assert "error" in out and "nope" in out["error"]


def test_set_updates_only_passed_fields(monkeypatch):
    _resolve(monkeypatch)
    c = _FakeSteps()
    steps.add_step(c, "pid", "sow", "first", status="upcoming")
    sid = c.rows[0]["id"]
    out = steps.set_step(c, "pid", "sow", sid, status="done")
    row = out["steps"][0]
    assert row["status"] == "done"
    assert row["title"] == "first"  # untouched


def test_set_nothing_to_update(monkeypatch):
    _resolve(monkeypatch)
    c = _FakeSteps()
    steps.add_step(c, "pid", "sow", "first")
    out = steps.set_step(c, "pid", "sow", c.rows[0]["id"])
    assert "note" in out and "nothing to update" in out["note"]


def test_reorder_renumbers_positions(monkeypatch):
    _resolve(monkeypatch)
    c = _FakeSteps()
    steps.add_step(c, "pid", "sow", "a")
    steps.add_step(c, "pid", "sow", "b")
    steps.add_step(c, "pid", "sow", "c")
    ids = [r["id"] for r in sorted(c.rows, key=lambda r: r["position"])]
    out = steps.reorder_steps(c, "pid", "sow", [ids[2], ids[0], ids[1]])
    assert [s["title"] for s in out["steps"]] == ["c", "a", "b"]


def test_remove_deletes_and_densifies(monkeypatch):
    _resolve(monkeypatch)
    c = _FakeSteps()
    steps.add_step(c, "pid", "sow", "a")
    steps.add_step(c, "pid", "sow", "b")
    steps.add_step(c, "pid", "sow", "c")
    mid = [r for r in c.rows if r["title"] == "b"][0]["id"]
    out = steps.remove_step(c, "pid", "sow", mid)
    assert [s["title"] for s in out["steps"]] == ["a", "c"]
    assert [s["position"] for s in out["steps"]] == [1, 2]  # densified


def test_writes_are_scoped_to_element(monkeypatch):
    # A step on a DIFFERENT element must not be touched by a scoped update.
    _resolve(monkeypatch, est_item_id="_authored/sow")
    c = _FakeSteps()
    steps.add_step(c, "pid", "sow", "mine")
    # inject a foreign-element row
    c.rows.append({"id": "foreign", "project_id": "pid",
                   "est_item_id": "_authored/other", "position": 1,
                   "title": "theirs", "status": "upcoming"})
    sid = [r for r in c.rows if r["title"] == "mine"][0]["id"]
    steps.set_step(c, "pid", "sow", sid, status="done")
    foreign = [r for r in c.rows if r["id"] == "foreign"][0]
    assert foreign["status"] == "upcoming"  # untouched


# --- tool-layer delegation ---------------------------------------------------

def test_tool_delegates_and_catches(monkeypatch):
    fake_client = object()
    monkeypatch.setattr(srv, "_resolve",
                        lambda code: (fake_client, "pid", "cid"))
    captured = {}

    def fake_add(client, pid, key, title, *, status, step_date, note,
                 company_id=None):
        captured["args"] = (client, pid, key, title, status, company_id)
        return {"est_item_id": key, "position": 1}

    monkeypatch.setattr("cp_engine.spine_steps.add_step", fake_add)
    out = srv.add_spine_step("ibx-5153", "sow", "a move")
    assert out == {"est_item_id": "sow", "position": 1}
    assert captured["args"] == (fake_client, "pid", "sow", "a move",
                                "upcoming", "cid")


def test_tool_unknown_project_errors(monkeypatch):
    monkeypatch.setattr(srv, "_resolve", lambda code: None)
    out = srv.add_spine_step("nope", "sow", "x")
    assert "error" in out and "not found" in out["error"]


# --- propose_step (auto-journey-steps: machine-authored, review-gated) --------


def test_propose_writes_auto_proposed(monkeypatch):
    _resolve(monkeypatch)
    c = _FakeSteps()
    out = steps.propose_step(c, "pid", "sow", "Ratified the pillars",
                             step_date="7/21")
    assert out["proposed"] is True
    row = c.rows[0]
    assert row["source"] == "auto"
    assert row["review"] == "proposed"
    assert row["status"] == "done"  # propose defaults to done (the move happened)
    assert row["title"] == "Ratified the pillars"


def test_propose_is_idempotent_on_title_and_date(monkeypatch):
    _resolve(monkeypatch)
    c = _FakeSteps()
    first = steps.propose_step(c, "pid", "sow", "Sent v3 to Janet", step_date="7/21")
    assert first["proposed"] is True
    # Same move again → no-op, no second row.
    again = steps.propose_step(c, "pid", "sow", "sent v3 to janet", step_date="7/21")
    assert again["proposed"] is False
    assert again["already"] is True
    assert len(c.rows) == 1


def test_propose_not_blocked_by_different_date(monkeypatch):
    _resolve(monkeypatch)
    c = _FakeSteps()
    steps.propose_step(c, "pid", "sow", "Weekly sync", step_date="7/21")
    out = steps.propose_step(c, "pid", "sow", "Weekly sync", step_date="7/28")
    assert out["proposed"] is True
    assert len(c.rows) == 2


def test_propose_idempotent_against_confirmed_twin(monkeypatch):
    """A step the human already CONFIRMED (or a manual one) blocks a re-propose —
    the guard matches on the natural key in ANY review state."""
    _resolve(monkeypatch)
    c = _FakeSteps()
    # Simulate a live human step with the same title/date.
    steps.add_step(c, "pid", "sow", "Booked the session", step_date="7/21")
    out = steps.propose_step(c, "pid", "sow", "Booked the session", step_date="7/21")
    assert out["proposed"] is False and out["already"] is True
    assert len(c.rows) == 1


def test_propose_rejects_bad_status_and_blank_title(monkeypatch):
    _resolve(monkeypatch)
    c = _FakeSteps()
    assert "error" in steps.propose_step(c, "pid", "sow", "x", status="bogus")
    assert "error" in steps.propose_step(c, "pid", "sow", "   ")


def test_propose_tool_unknown_project_errors(monkeypatch):
    monkeypatch.setattr(srv, "_resolve", lambda code: None)
    out = srv.propose_spine_step("nope", "sow", "x")
    assert "error" in out and "not found" in out["error"]
