from pathlib import Path

import frontmatter
from click.testing import CliRunner

from cp_engine.cli import main


def _write(p: Path, body: str = "Original body text.", **fm) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines += ["---", body]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _tenant_with_deliverable(tmp_path: Path) -> Path:
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.18"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n',
        encoding="utf-8",
    )
    proj = tmp_path / "1p" / "infoblox" / "ibx-5153-ai-campaign"
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


class _FakeTable:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._rows = None
        self._conflict = None

    def upsert(self, rows, on_conflict=None):
        self._rows = rows if isinstance(rows, list) else [rows]
        self._conflict = on_conflict
        return self

    def execute(self):
        bucket = self.store.setdefault(self.name, [])
        for r in self._rows:
            bucket[:] = [x for x in bucket if x["id"] != r["id"]]
            bucket.append(dict(r))
        return type("R", (), {"data": self._rows})()


class _FakeClient:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return _FakeTable(self.store, name)


def _snap_dir(tmp_path: Path) -> Path:
    return (
        tmp_path
        / "1p"
        / "infoblox"
        / "ibx-5153-ai-campaign"
        / "spine"
        / "Deliverables"
        / "pos.snapshots"
    )


def test_snapshot_writes_file_and_row(tmp_path, monkeypatch) -> None:
    _tenant_with_deliverable(tmp_path)
    monkeypatch.chdir(tmp_path)

    fake = _FakeClient()
    monkeypatch.setattr(
        "cp_engine.mc2_db.get_client", lambda config=None, **kw: fake
    )

    result = CliRunner().invoke(
        main,
        [
            "snapshot",
            "ibx-5153/deliverable/pos",
            "--label",
            "Before IBX workshop",
            "--reason",
            "Freezing pre-workshop state.",
        ],
    )
    assert result.exit_code == 0, result.output

    # File written under the .snapshots sibling folder with a date-prefixed,
    # slugified label name.
    matches = list(_snap_dir(tmp_path).glob("*-before-ibx-workshop.md"))
    assert len(matches) == 1, list(_snap_dir(tmp_path).iterdir())
    frozen = matches[0]

    post = frontmatter.loads(frozen.read_text())
    assert "The positioning story so far." in post.content
    snap = post.metadata["snapshot"]
    assert snap["label"] == "Before IBX workshop"
    assert snap["reason"] == "Freezing pre-workshop state."
    assert snap["of"] == "ibx-5153/deliverable/pos"

    # Index row upserted with id locked to the on-disk filename.
    rows = fake.store["spine_snapshots"]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == f"ibx-5153/deliverable/pos@{frozen.stem}"
    assert row["deliverable_id"] == "ibx-5153/deliverable/pos"
    assert row["project_code"] == "ibx-5153"
    assert row["label"] == "Before IBX workshop"
    assert row["rel_path"] == str(frozen.relative_to(tmp_path))


def test_snapshot_same_day_collision_appends_suffix(tmp_path, monkeypatch) -> None:
    _tenant_with_deliverable(tmp_path)
    monkeypatch.chdir(tmp_path)

    fake = _FakeClient()
    monkeypatch.setattr(
        "cp_engine.mc2_db.get_client", lambda config=None, **kw: fake
    )

    args = [
        "snapshot",
        "ibx-5153/deliverable/pos",
        "--label",
        "Before IBX workshop",
    ]
    r1 = CliRunner().invoke(main, args)
    assert r1.exit_code == 0, r1.output
    r2 = CliRunner().invoke(main, args)
    assert r2.exit_code == 0, r2.output

    names = sorted(p.name for p in _snap_dir(tmp_path).glob("*.md"))
    assert len(names) == 2
    assert any(n.endswith("-before-ibx-workshop.md") for n in names)
    assert any(n.endswith("-before-ibx-workshop-2.md") for n in names)

    # The -2 file's index row id must end in -2 (lockstep with the filename).
    rows = fake.store["spine_snapshots"]
    ids = sorted(r["id"] for r in rows)
    assert len(ids) == 2
    assert any(i.endswith("-before-ibx-workshop-2") for i in ids)
    # The -2 row's rel_path points at the -2 file.
    row2 = next(r for r in rows if r["id"].endswith("-2"))
    assert row2["rel_path"].endswith("-before-ibx-workshop-2.md")


def test_snapshot_missing_deliverable_errors(tmp_path, monkeypatch) -> None:
    _tenant_with_deliverable(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        main,
        ["snapshot", "ibx-5153/deliverable/nope", "--label", "x"],
    )
    assert result.exit_code == 1
    assert "nope" in result.output


def test_snapshot_missing_project_errors(tmp_path, monkeypatch) -> None:
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n[engine]\nversion = "~= 0.18"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        main,
        ["snapshot", "nope-9999/deliverable/x", "--label", "x"],
    )
    assert result.exit_code == 1
    assert "No working dir" in result.output or "nope-9999" in result.output


def test_snapshot_mc2_unavailable_still_writes_file(tmp_path, monkeypatch) -> None:
    """No creds → connect raises → file still saved on disk, skip note printed."""
    _tenant_with_deliverable(tmp_path)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        main,
        ["snapshot", "ibx-5153/deliverable/pos", "--label", "Offline snap"],
    )
    assert result.exit_code == 0, result.output
    assert "MC-2 index skipped" in result.output
    matches = list(_snap_dir(tmp_path).glob("*-offline-snap.md"))
    assert len(matches) == 1


# --- cp snapshots (list) ---


def _write_frozen(snap_dir: Path, fname: str, *, created: str, label: str,
                  reason: str | None = None, commit: str | None = None) -> None:
    snap_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "project: ibx-5153",
        "snapshot:",
        "  of: ibx-5153/deliverable/pos",
        f"  label: {label}",
        f"  reason: {reason}",
        f"  created: {created}",
        f"  commit: {commit}",
        "  working_copy_dirty: false",
        "---",
        "frozen body",
    ]
    (snap_dir / fname).write_text("\n".join(lines) + "\n")


def test_snapshots_lists_newest_first(tmp_path, monkeypatch) -> None:
    _tenant_with_deliverable(tmp_path)
    monkeypatch.chdir(tmp_path)
    sd = _snap_dir(tmp_path)
    _write_frozen(sd, "2026-06-10-old.md", created="2026-06-10",
                  label="Older", reason="r1", commit="aaa1111")
    _write_frozen(sd, "2026-06-13-new.md", created="2026-06-13",
                  label="Newer", reason="r2", commit="bbb2222")

    result = CliRunner().invoke(main, ["snapshots", "ibx-5153/deliverable/pos"])
    assert result.exit_code == 0, result.output
    assert "2 snapshot(s)" in result.output
    # Newest first: "Newer" line precedes "Older".
    assert result.output.index("Newer") < result.output.index("Older")
    assert "aaa1111" in result.output and "bbb2222" in result.output


def test_snapshots_same_day_collision_does_not_crash(tmp_path, monkeypatch) -> None:
    # Regression: two snapshots with the SAME created date previously crashed
    # snapshots_cmd because sort(reverse=True) fell through to comparing the
    # meta dicts (TypeError: '<' not supported between dict and dict).
    _tenant_with_deliverable(tmp_path)
    monkeypatch.chdir(tmp_path)
    sd = _snap_dir(tmp_path)
    _write_frozen(sd, "2026-06-10-before.md", created="2026-06-10",
                  label="First", reason="r1", commit="aaa1111")
    _write_frozen(sd, "2026-06-10-before-2.md", created="2026-06-10",
                  label="Second", reason="r2", commit="bbb2222")

    result = CliRunner().invoke(main, ["snapshots", "ibx-5153/deliverable/pos"])
    assert result.exit_code == 0, result.output
    assert "2 snapshot(s)" in result.output
    assert "aaa1111" in result.output and "bbb2222" in result.output


def test_snapshots_empty_message(tmp_path, monkeypatch) -> None:
    _tenant_with_deliverable(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["snapshots", "ibx-5153/deliverable/pos"])
    assert result.exit_code == 0, result.output
    assert "No snapshots" in result.output


def test_snapshots_missing_deliverable_exits_1(tmp_path, monkeypatch) -> None:
    _tenant_with_deliverable(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["snapshots", "ibx-5153/deliverable/nope"])
    assert result.exit_code == 1
    assert "nope" in result.output
