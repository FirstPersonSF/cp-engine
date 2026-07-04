"""CLI test for `cp spine-frame` (Task 3.3): thin wiring args → promote_card."""

from pathlib import Path

from click.testing import CliRunner

from cp_engine.cli import main


def _tenant(tmp_path: Path) -> None:
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.26"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n',
        encoding="utf-8",
    )


class _FakeTable:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._op = None
        self._filter = None

    def select(self, cols):
        assert "*" not in cols
        self._op = ("select",)
        return self

    def update(self, values):
        self._op = ("update", values)
        return self

    def eq(self, col, val):
        self._filter = (col, val)
        return self

    def execute(self):
        rows = self.store.setdefault(self.name, [])
        if self._op[0] == "select":
            col, val = self._filter
            return type("R", (), {"data": [r for r in rows if r.get(col) == val]})()
        if self._op[0] == "update":
            col, val = self._filter
            for r in rows:
                if r.get(col) == val:
                    r.update(self._op[1])
            return type("R", (), {"data": []})()


class _FakeClient:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return _FakeTable(self.store, name)


def test_spine_frame_promotes_and_prints_path(tmp_path, monkeypatch):
    _tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    card_id = "ibx-5153/inbox/mtg-42"
    store = {
        "spine_inbox": [
            {
                "id": card_id, "project_id": "u-123", "project_code": "ibx-5153",
                "source_ref": "mtg-42", "raw_distillation": "raw body",
                "guessed_est_item_id": "d1", "guessed_type": "deliverable",
                "status": "proposed", "framing": None,
            }
        ]
    }
    client = _FakeClient(store)

    # MC-2 connect → our fake client.
    monkeypatch.setattr(
        "cp_engine.mc2_db.get_client", lambda config=None, **kw: client
    )

    # Estimate resolves the item name + phase.
    from cp_engine.estimate import Estimate, EstimateItem, EstimatePhase

    item = EstimateItem(id="d1", phase_id="ph0", kind="deliverable",
                        name="Messaging system", short_description=None,
                        position=0, library_item_id=None)
    est = Estimate(id="e1", mc_project_id="u-123", name="E1",
                   phases=(EstimatePhase(id="ph0", name="Phase 0", overview=None,
                                         position=0, items=(item,)),))
    monkeypatch.setattr("cp_engine.estimate.fetch_estimate", lambda c, pid: est)

    proj_dir = tmp_path / "1p/infoblox/ibx-5153"
    proj_dir.mkdir(parents=True)
    monkeypatch.setattr("cp_engine.spine.find_spine_dir", lambda root, code: proj_dir)

    monkeypatch.setattr(
        "cp_engine.plan_from_transcript._call_claude",
        lambda prompt, *, model, api_key=None: "the framed distilled body",
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["spine-frame", card_id, "--framing", "lock the two-track thesis"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output

    written = proj_dir / "spine" / "phase-0" / "messaging-system.md"
    assert written.exists()
    assert str(written) in result.output

    from cp_engine.substance import parse_substance

    parsed = parse_substance(written)
    assert parsed.est_item_id == "d1"
    assert parsed.phase == "Phase 0"
    live = parsed.live_version()
    assert live.framing == "lock the two-track thesis"
    assert live.body == "the framed distilled body"
    assert live.sources == ("mtg-42",)  # defaulted to card.source_ref

    # card status flipped to promoted
    assert store["spine_inbox"][0]["status"] == "promoted"


def test_spine_frame_errors_on_missing_card(tmp_path, monkeypatch):
    _tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = _FakeClient({"spine_inbox": []})
    monkeypatch.setattr(
        "cp_engine.mc2_db.get_client", lambda config=None, **kw: client
    )
    runner = CliRunner()
    result = runner.invoke(
        main, ["spine-frame", "nope/inbox/x", "--framing", "f"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "no inbox card" in result.output
