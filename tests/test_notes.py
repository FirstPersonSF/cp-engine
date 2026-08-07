# tests/test_notes.py — #107 create_note (partner ping: in-app note + Slack DM)
import cp_engine.notes as notes
import cp_engine.mcp_server as srv


# ── fake supabase client ────────────────────────────────────────────────────

class _Table:
    def __init__(self, name, store, log):
        self.name = name
        self.store = store
        self.log = log
        self._op = None
        self._payload = None
        self._filters = []

    def select(self, _cols):
        self._op = "select"
        return self

    def insert(self, row):
        self._op = "insert"
        self._payload = row
        return self

    def update(self, patch):
        self._op = "update"
        self._payload = patch
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def ilike(self, col, val):
        self._filters.append(("ilike", col, val.strip("%").lower()))
        return self

    def execute(self):
        if self._op == "insert":
            self.store.setdefault(self.name, []).append(self._payload)
            self.log.append(("insert", self.name, self._payload))
            return type("R", (), {"data": [self._payload]})()
        if self._op == "update":
            self.log.append(("update", self.name, self._payload, self._filters))
            return type("R", (), {"data": [{}]})()
        # select: apply ilike over the seeded entities
        rows = list(self.store.get(self.name, []))
        for kind, col, val in self._filters:
            if kind == "ilike":
                rows = [r for r in rows if val in (r.get(col) or "").lower()]
        return type("R", (), {"data": rows})()


class _Client:
    def __init__(self, entities):
        self.store = {"entities": entities}
        self.log = []

    def table(self, name):
        return _Table(name, self.store, self.log)


_ENTS = [
    {"id": "u-drew", "name": "Drew Fiero", "email": "drew@firstperson.is",
     "slack_user_id": "U-DREW"},
    {"id": "u-marc", "name": "Marcello Grande", "email": "marcello@firstperson.is",
     "slack_user_id": "U-MARC"},
    {"id": "u-mara", "name": "Mara Lopez", "email": "mara@firstperson.is",
     "slack_user_id": None},
]


def _no_slack(monkeypatch):
    """load_slack_token raises → DM path returns 'skipped' (in-app only)."""
    import cp_engine.slack as slack_mod
    monkeypatch.setattr(slack_mod, "load_slack_token",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("no token")))


def _slack_ok(monkeypatch, sent):
    """A working Slack path: token present, post_dm records + returns a ts."""
    import cp_engine.slack as slack_mod
    monkeypatch.setattr(slack_mod, "load_slack_token", lambda cfg: "xoxb-test")

    class _Web:
        def __init__(self, token=None):
            pass
        def users_lookupByEmail(self, email):
            return {"ok": True, "user": {"id": "U-LOOKED-UP"}}
    monkeypatch.setattr("slack_sdk.WebClient", _Web)

    def _post_dm(client, *, user_id, text, blocks=None):
        sent.append({"user_id": user_id, "text": text, "blocks": blocks})
        return "1720000000.0001"
    monkeypatch.setattr(slack_mod, "post_dm", _post_dm)


# ── write_note ──────────────────────────────────────────────────────────────

def test_note_by_name_inserts_and_dms(monkeypatch):
    sent = []
    _slack_ok(monkeypatch, sent)
    client = _Client([dict(e) for e in _ENTS])
    out = notes.write_note(client, object(), project_code="ibx-5192",
                           project_id="pid-1", recipient="Marcello",
                           body="Progress on the deck.")
    assert out["recipient"] == "Marcello Grande"
    assert out["author"] == "Drew Fiero"          # default author
    assert out["slack_delivery"] == "sent"
    assert out["slack_ts"] == "1720000000.0001"
    # the notes row was inserted, unread, to the right recipient
    inserted = [e for e in client.log if e[0] == "insert" and e[1] == "notes"]
    assert inserted and inserted[0][2]["recipient_id"] == "u-marc"
    assert inserted[0][2]["status"] == "unread"
    # DM went to Marcello's cached slack id
    assert sent and sent[0]["user_id"] == "U-MARC"
    # deep link points at the project UUID
    btn = sent[0]["blocks"][-1]["elements"][0]["url"]
    assert btn.endswith("/jobs/pid-1")


def test_note_by_email(monkeypatch):
    sent = []
    _slack_ok(monkeypatch, sent)
    client = _Client([dict(e) for e in _ENTS])
    out = notes.write_note(client, object(), project_code="ibx-5192",
                           project_id="pid-1",
                           recipient="marcello@firstperson.is", body="hi")
    assert out["recipient"] == "Marcello Grande"
    assert out["slack_delivery"] == "sent"


def test_note_dm_skipped_still_inserts(monkeypatch):
    _no_slack(monkeypatch)
    client = _Client([dict(e) for e in _ENTS])
    out = notes.write_note(client, object(), project_code="ibx-5192",
                           project_id="pid-1", recipient="Marcello", body="hi")
    assert out["slack_delivery"] == "skipped"
    assert "slack_ts" not in out
    # note still exists in-app
    assert any(e[0] == "insert" and e[1] == "notes" for e in client.log)


def test_note_recipient_without_slack_user_and_no_lookup(monkeypatch):
    # Mara has no slack_user_id; make lookup fail → skipped, still in-app.
    import cp_engine.slack as slack_mod
    monkeypatch.setattr(slack_mod, "load_slack_token", lambda cfg: "xoxb-test")

    class _Web:
        def __init__(self, token=None): pass
        def users_lookupByEmail(self, email):
            return {"ok": False}
    monkeypatch.setattr("slack_sdk.WebClient", _Web)

    client = _Client([dict(e) for e in _ENTS])
    out = notes.write_note(client, object(), project_code="ibx-5192",
                           project_id="pid-1", recipient="Mara", body="hi")
    assert out["slack_delivery"] == "skipped"


def test_note_ambiguous_recipient_returns_note(monkeypatch):
    _no_slack(monkeypatch)
    # "Mar" matches both Marcello and Mara → ambiguous.
    client = _Client([dict(e) for e in _ENTS])
    out = notes.write_note(client, object(), project_code="ibx-5192",
                           project_id="pid-1", recipient="Mar", body="hi")
    assert "no single entity matching recipient 'Mar'" in out["note"]
    assert not any(e[0] == "insert" for e in client.log)


def test_note_unknown_recipient_returns_note(monkeypatch):
    _no_slack(monkeypatch)
    client = _Client([dict(e) for e in _ENTS])
    out = notes.write_note(client, object(), project_code="ibx-5192",
                           project_id="pid-1", recipient="Nobody", body="hi")
    assert "recipient 'Nobody'" in out["note"]


def test_note_self_addressed_rejected(monkeypatch):
    _no_slack(monkeypatch)
    client = _Client([dict(e) for e in _ENTS])
    out = notes.write_note(client, object(), project_code="ibx-5192",
                           project_id="pid-1", recipient="Drew",
                           author="Drew", body="hi")
    assert "same person" in out["note"]


def test_note_empty_body_rejected(monkeypatch):
    client = _Client([dict(e) for e in _ENTS])
    out = notes.write_note(client, object(), project_code="ibx-5192",
                           project_id="pid-1", recipient="Marcello", body="   ")
    assert "body is required" in out["note"]


def test_note_dm_failure_records_failed(monkeypatch):
    import cp_engine.slack as slack_mod
    monkeypatch.setattr(slack_mod, "load_slack_token", lambda cfg: "xoxb-test")
    monkeypatch.setattr("slack_sdk.WebClient", lambda token=None: object())

    def _boom(client, *, user_id, text, blocks=None):
        raise RuntimeError("slack down")
    monkeypatch.setattr(slack_mod, "post_dm", _boom)

    client = _Client([dict(e) for e in _ENTS])
    out = notes.write_note(client, object(), project_code="ibx-5192",
                           project_id="pid-1", recipient="Marcello", body="hi")
    # DM failed, but the note still landed in-app.
    assert out["slack_delivery"] == "failed"
    assert any(e[0] == "insert" and e[1] == "notes" for e in client.log)


# ── MCP tool boundary ────────────────────────────────────────────────────────





