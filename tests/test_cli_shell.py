from datetime import date
from pathlib import Path

from click.testing import CliRunner

from cp_engine.cli import main
from cp_engine.shell import load_shell, render_sweep


def _write(p: Path, **fm) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines += ["---", "body"]
    p.write_text("\n".join(lines), encoding="utf-8")


def _tenant_with_ibx(tmp_path: Path) -> Path:
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.18"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n',
        encoding="utf-8",
    )
    proj = tmp_path / "1p" / "infoblox" / "ibx-5153-ai-campaign"
    shell = proj / "shell"
    _write(
        shell / "Deliverables" / "positioning-narrative.md",
        id="ibx-5153/deliverable/positioning-narrative",
        project="ibx-5153",
        layer="Deliverables",
        title="Positioning narrative",
        stage="revised",
        status="active",
        last_touched="2026-06-13",
        target_date="2026-06-19",
    )
    _write(
        shell / "Brief" / "april-brief.md",
        id="ibx-5153/brief/april-brief",
        project="ibx-5153",
        layer="Brief",
        title="April input brief",
        status="reference",
        last_touched="2026-04-10",
    )
    return tmp_path


def test_render_sweep_ranks_live_deliverable_above_cold_brief(tmp_path: Path) -> None:
    proj = _tenant_with_ibx(tmp_path) / "1p" / "infoblox" / "ibx-5153-ai-campaign"
    out = render_sweep("ibx-5153", load_shell(proj), today=date(2026, 6, 13))
    assert out.index("Positioning narrative") < out.index("April input brief")
    assert "due 2026-06-19" in out


def test_render_sweep_shows_serves_continuation(tmp_path: Path) -> None:
    proj = tmp_path / "1p" / "infoblox" / "ibx-5153-ai-campaign"
    shell = proj / "shell"
    _write(
        shell / "Deliverables" / "x.md",
        id="ibx-5153/deliverable/x",
        project="ibx-5153",
        layer="Deliverables",
        title="Deliverable X",
        stage="revised",
        status="active",
        last_touched="2026-06-13",
    )
    _write(
        shell / "Research" / "market-scan.md",
        id="ibx-5153/research/market-scan",
        project="ibx-5153",
        layer="Research",
        title="Market scan",
        status="active",
        last_touched="2026-06-10",
        serves="[ibx-5153/deliverable/x]",
    )
    out = render_sweep("ibx-5153", load_shell(proj), today=date(2026, 6, 13))
    assert "← serves: ibx-5153/deliverable/x" in out


def test_render_sweep_marks_overdue(tmp_path: Path) -> None:
    proj = tmp_path / "1p" / "infoblox" / "ibx-5153-ai-campaign"
    shell = proj / "shell"
    _write(
        shell / "Deliverables" / "past.md",
        id="ibx-5153/deliverable/past",
        project="ibx-5153",
        layer="Deliverables",
        title="Past deliverable",
        stage="revised",
        status="active",
        last_touched="2026-06-13",
        target_date="2026-05-01",
    )
    _write(
        shell / "Deliverables" / "future.md",
        id="ibx-5153/deliverable/future",
        project="ibx-5153",
        layer="Deliverables",
        title="Future deliverable",
        stage="revised",
        status="active",
        last_touched="2026-06-13",
        target_date="2026-06-26",
    )
    out = render_sweep("ibx-5153", load_shell(proj), today=date(2026, 6, 13))
    assert "due 2026-05-01 (overdue)" in out
    assert "due 2026-06-26" in out
    assert "due 2026-06-26 (overdue)" not in out


def test_render_sweep_empty_shell(tmp_path: Path) -> None:
    out = render_sweep("ibx-5153", (), today=date(2026, 6, 13))
    assert "ibx-5153" in out
    assert "0 elements" in out


def test_cli_shell_prints_sweep(tmp_path, monkeypatch) -> None:
    _tenant_with_ibx(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["shell", "ibx-5153"])
    assert result.exit_code == 0, result.output
    assert "ibx-5153" in result.output
    assert "Positioning narrative" in result.output
    assert "April input brief" in result.output


def test_cli_shell_falls_back_to_disk_when_mc2_unavailable(tmp_path, monkeypatch) -> None:
    """No SUPABASE creds + no mc-2 clone → connect() raises BackendUnavailable,
    and the command falls back to reading the on-disk markdown frontmatter."""
    _tenant_with_ibx(tmp_path)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["shell", "ibx-5153"])
    assert result.exit_code == 0, result.output
    assert "MC-2 unavailable" in result.output
    assert "last-known markdown-derived state, unverified" in result.output
    assert "Positioning narrative" in result.output


def test_cli_shell_empty_mc2_does_not_fall_back_to_disk(tmp_path, monkeypatch) -> None:
    """An empty MC-2 result is the authoritative answer — we must NOT silently
    fall through to disk and mask it. No unverified banner; no disk content."""
    _tenant_with_ibx(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cp_engine.sync_mc2.MC2Backend.connect", lambda self, cfg: object())
    monkeypatch.setattr("cp_engine.shell.load_shell_from_mc2", lambda client, code: ())
    result = CliRunner().invoke(main, ["shell", "ibx-5153"])
    assert result.exit_code == 0, result.output
    assert "unverified" not in result.output  # MC-2 served the read
    assert "Positioning narrative" not in result.output  # disk NOT consulted


def test_cli_shell_prefers_mc2_rows(tmp_path, monkeypatch) -> None:
    """When MC-2 returns rows, the command renders those, not the disk shell."""
    _tenant_with_ibx(tmp_path)
    monkeypatch.chdir(tmp_path)

    from cp_engine.shell import ShellElement

    def _fake_load_from_mc2(client, code):
        return (
            ShellElement(
                id="ibx-5153/deliverable/from-mc2",
                project="ibx-5153",
                layer="Deliverables",
                title="From MC-2 spine",
                status="active",
                last_touched="2026-06-13",
                path=Path("x"),
                body="",
                stage="revised",
            ),
        )

    monkeypatch.setattr("cp_engine.sync_mc2.MC2Backend.connect", lambda self, cfg: object())
    monkeypatch.setattr("cp_engine.shell.load_shell_from_mc2", _fake_load_from_mc2)
    result = CliRunner().invoke(main, ["shell", "ibx-5153"])
    assert result.exit_code == 0, result.output
    assert "From MC-2 spine" in result.output
    assert "Positioning narrative" not in result.output  # disk path NOT used


def test_cli_shell_unknown_code_errors(tmp_path, monkeypatch) -> None:
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n[engine]\nversion = "~= 0.18"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["shell", "nope-9999"])
    assert result.exit_code == 1
    assert "No working dir" in result.output
