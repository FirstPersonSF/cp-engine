from pathlib import Path

from click.testing import CliRunner

from cp_engine.cli import main
from cp_engine.clock import tenant_today

FAKE_SYNTHESIS = "## Synthesis\n\nThe whole project, swept into one readout."


def _write(p: Path, body: str = "body", **fm) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines += ["---", body]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _tenant_with_ibx(tmp_path: Path) -> Path:
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.18"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n',
        encoding="utf-8",
    )
    proj = tmp_path / "1p" / "infoblox" / "ibx-5153-ai-campaign"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "cp.md").write_text("---\nMC-id: pid-1\n---\n", encoding="utf-8")
    spine = proj / "spine"
    _write(
        spine / "Deliverables" / "pos.md",
        body="The positioning story so far.",
        id="ibx-5153/deliverable/pos",
        project="ibx-5153",
        layer="Deliverables",
        title="Positioning narrative",
        stage="revised",
        status="active",
        last_touched="2026-06-13",
    )
    return tmp_path


def _empty_tenant(tmp_path: Path) -> Path:
    """A tenant whose project dir exists (so find_spine_dir resolves) but the
    spine has no elements."""
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.18"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n',
        encoding="utf-8",
    )
    proj = tmp_path / "1p" / "infoblox" / "ibx-5153-ai-campaign"
    (proj / "spine").mkdir(parents=True, exist_ok=True)
    # A cp.md so the dir is discoverable as a project working dir.
    (proj / "cp.md").write_text("placeholder\n", encoding="utf-8")
    return tmp_path


def _syn_dir(tmp_path: Path) -> Path:
    """Where a sweep's rendered file lands (step 4c: an authored element)."""
    return (
        tmp_path
        / "1p"
        / "infoblox"
        / "ibx-5153-ai-campaign"
        / "spine"
        / "_authored"
    )


def _fake_mc2(monkeypatch, *, serve_reads=False):
    """MC-2 for the WRITE (step 4c: the readout lands in MC-2 first). Reads
    keep the disk fallback unless `serve_reads` — the fixture's element is a
    legacy disk file."""
    from tests._spine_fake import FakeClient

    client = FakeClient(spine_substance=[], spine_steps=[])
    monkeypatch.setattr("cp_engine.mc2_db.get_client", lambda config=None, **kw: client)
    if not serve_reads:
        def _unreadable(c, code):
            raise RuntimeError("read path not under test")
        monkeypatch.setattr("cp_engine.spine.load_spine_from_mc2", _unreadable)
    return client


def test_sweep_writes_synthesis_element(tmp_path, monkeypatch) -> None:
    _tenant_with_ibx(tmp_path)
    monkeypatch.chdir(tmp_path)
    mc2 = _fake_mc2(monkeypatch)

    def fake(prompt, *, model, api_key=None):
        return FAKE_SYNTHESIS

    monkeypatch.setattr("cp_engine.plan_from_transcript._call_claude", fake)

    result = CliRunner().invoke(main, ["sweep", "ibx-5153"])
    assert result.exit_code == 0, result.output

    today = tenant_today().isoformat()
    # MC-2 first: one authored Synthesis element, today's.
    (row,) = mc2.store["spine_substance"]
    assert row["id"] == f"ibx-5153-ai-campaign/_authored/{today}-sweep/v1"
    assert row["project_id"] == "pid-1"
    assert row["layer"] == "Synthesis"
    assert row["origin"] == "authored"
    assert "The whole project, swept into one readout." in row["body"]
    # The sweep readout serves the active deliverable(s) so it scores hot on the
    # next Lens pass (the fixture's pos.md is an active, non-final deliverable).
    assert row["serves"] == ["ibx-5153/deliverable/pos"]

    # …then rendered from those rows into the generated view.
    (f,) = list(_syn_dir(tmp_path).glob("*-sweep.md"))
    assert f.name == f"{today}-sweep.md"
    from cp_engine.substance import parse_substance

    item = parse_substance(f)
    assert item.layer == "Synthesis"
    assert "swept into one readout" in item.live_version().body
    assert not (tmp_path / "1p/infoblox/ibx-5153-ai-campaign/spine/Synthesis").exists()

    # Ranked table echoed to stdout.
    assert "Positioning narrative" in result.output


def test_sweep_idempotent_same_day_overwrites(tmp_path, monkeypatch) -> None:
    _tenant_with_ibx(tmp_path)
    monkeypatch.chdir(tmp_path)
    mc2 = _fake_mc2(monkeypatch)

    def fake(prompt, *, model, api_key=None):
        return FAKE_SYNTHESIS

    monkeypatch.setattr("cp_engine.plan_from_transcript._call_claude", fake)

    r1 = CliRunner().invoke(main, ["sweep", "ibx-5153"])
    assert r1.exit_code == 0, r1.output
    r2 = CliRunner().invoke(main, ["sweep", "ibx-5153"])
    assert r2.exit_code == 0, r2.output

    assert len(mc2.store["spine_substance"]) == 1
    matches = list(_syn_dir(tmp_path).glob("*-sweep.md"))
    assert len(matches) == 1, [p.name for p in matches]


def test_sweep_without_mc2_writes_nothing(tmp_path, monkeypatch) -> None:
    """Step 4c: there is no disk-only write left — spine/ is rendered from
    MC-2, so a readout MC-2 cannot take is reported, not dropped in spine/."""
    _tenant_with_ibx(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cp_engine.plan_from_transcript._call_claude",
                        lambda prompt, *, model, api_key=None: FAKE_SYNTHESIS)
    result = CliRunner().invoke(main, ["sweep", "ibx-5153"])
    assert result.exit_code == 1
    assert "NOT written" in result.output
    assert not _syn_dir(tmp_path).exists()


def test_sweep_llm_failure_helpful_message(tmp_path, monkeypatch) -> None:
    _tenant_with_ibx(tmp_path)
    monkeypatch.chdir(tmp_path)

    def boom(prompt, *, model, api_key=None):
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr("cp_engine.plan_from_transcript._call_claude", boom)

    result = CliRunner().invoke(main, ["sweep", "ibx-5153"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output
    assert "failed" in result.output.lower()
    # No file written on failure.
    assert not _syn_dir(tmp_path).exists() or not list(
        _syn_dir(tmp_path).glob("*-sweep.md")
    )


def test_sweep_mc2_path_reresolves_and_writes(tmp_path, monkeypatch) -> None:
    """When elements come from MC-2, _load_spine_elements returns project_dir=None;
    sweep_cmd must re-resolve the dir on disk before rendering."""
    from pathlib import Path

    from cp_engine.spine import SpineElement

    _tenant_with_ibx(tmp_path)  # gives a real on-disk project dir (cp.md + spine)
    monkeypatch.chdir(tmp_path)
    mc2 = _fake_mc2(monkeypatch, serve_reads=True)

    el = SpineElement(
        id="ibx-5153/deliverable/pos",
        project="ibx-5153",
        layer="Deliverables",
        title="Positioning narrative",
        status="active",
        last_touched="2026-06-13",
        path=Path("/dev/null"),
        body="From MC-2.",
        stage="revised",
    )
    monkeypatch.setattr("cp_engine.cli._load_spine_elements",
                        lambda config, code: ((el,), None))
    monkeypatch.setattr("cp_engine.plan_from_transcript._call_claude",
                        lambda prompt, *, model, api_key=None: FAKE_SYNTHESIS)

    result = CliRunner().invoke(main, ["sweep", "ibx-5153"])
    assert result.exit_code == 0, result.output

    today = tenant_today().isoformat()
    (f,) = list(_syn_dir(tmp_path).glob("*-sweep.md"))
    assert f.name == f"{today}-sweep.md"
    (row,) = mc2.store["spine_substance"]
    assert row["layer"] == "Synthesis"
    assert row["serves"] == ["ibx-5153/deliverable/pos"]


def test_sweep_empty_spine_writes_nothing(tmp_path, monkeypatch) -> None:
    _empty_tenant(tmp_path)
    monkeypatch.chdir(tmp_path)

    called = {"llm": False}

    def fake(prompt, *, model, api_key=None):
        called["llm"] = True
        return FAKE_SYNTHESIS

    monkeypatch.setattr("cp_engine.plan_from_transcript._call_claude", fake)

    result = CliRunner().invoke(main, ["sweep", "ibx-5153"])
    assert result.exit_code == 0, result.output
    assert "No spine elements to sweep for ibx-5153." in result.output
    assert called["llm"] is False
    # No Synthesis element written.
    assert not list(_syn_dir(tmp_path).glob("*-sweep.md"))


# --- Task 4.2: drift flags written to MC-2 ---------------------------------

class _DriftFakeTable:
    """Minimal PostgREST-shaped fake supporting select/eq/limit/update/execute."""

    def __init__(self, store, name):
        self.store, self.name = store, name
        self._op = None
        self._payload = None
        self._filter = None

    def select(self, cols):
        self._op = "select"; return self

    def update(self, values):
        self._op = "update"; self._payload = values; return self

    def eq(self, col, val):
        self._filter = (col, val); return self

    def limit(self, n):
        return self

    def execute(self):
        rows = self.store.setdefault(self.name, [])
        if self._op == "select":
            col, val = self._filter
            data = [x for x in rows if x.get(col) == val]
            return type("R", (), {"data": data})()
        if self._op == "update":
            col, val = self._filter
            for x in rows:
                if x.get(col) == val:
                    x.update(self._payload)
            return type("R", (), {"data": []})()
        raise AssertionError(f"unexpected op {self._op}")


class _DriftFakeClient:
    def __init__(self, rows):
        # `spine_substance` since mc-2 migration 072 dropped `spine_elements`;
        # the row key is `id` (the substance row id), which IS the sweep's
        # element_id — see mc2_db.fetch_element_review_flags.
        self.store = {"spine_substance": rows}

    def table(self, name):
        return _DriftFakeTable(self.store, name)


def test_write_drift_flags_lands_flag_with_source_sweep():
    from cp_engine.cli import _write_drift_flags

    rows = [{"id": "ibx-5153/deliverable/pos", "review_flags": []}]
    client = _DriftFakeClient(rows)
    drift = [{
        "element_id": "ibx-5153/deliverable/pos",
        "field": "stage",
        "observation": "stage looks stale.",
    }]
    n = _write_drift_flags(client, drift, "2026-06-15")
    assert n == 1
    flags = rows[0]["review_flags"]
    assert len(flags) == 1
    assert flags[0]["field"] == "stage"
    assert flags[0]["source"] == "sweep"
    assert flags[0]["now"] == "stage looks stale."
    assert flags[0]["at"] == "2026-06-15"


def test_write_drift_flags_keeps_one_per_field():
    from cp_engine.cli import _write_drift_flags

    rows = [{"id": "ibx-5153/deliverable/pos", "review_flags": []}]
    client = _DriftFakeClient(rows)
    drift = [{
        "element_id": "ibx-5153/deliverable/pos",
        "field": "stage",
        "observation": "first observation.",
    }]
    _write_drift_flags(client, drift, "2026-06-15")
    drift2 = [{
        "element_id": "ibx-5153/deliverable/pos",
        "field": "stage",
        "observation": "second observation.",
    }]
    _write_drift_flags(client, drift2, "2026-06-16")
    flags = rows[0]["review_flags"]
    stage_flags = [f for f in flags if f["field"] == "stage"]
    assert len(stage_flags) == 1
    assert stage_flags[0]["now"] == "second observation."
