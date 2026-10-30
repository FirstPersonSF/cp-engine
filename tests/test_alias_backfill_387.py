"""#387 follow-up — seed the alias-candidate queue from stored meetings.

Ingest only queues candidates for meetings it sees from now on. The tenant
already holds hundreds of meetings under its `meetings/` folders; `cxp
aliases --scan` runs the same detection over them once. It prints by
default, appends to `alias-candidates.md` only with `--write`, and never
edits `.cp-engine.toml`: accepting stays `cxp aliases --accept`.
"""
from __future__ import annotations

import json
from datetime import date

TODAY = date(2026, 10, 30)
KNOWN = '[terms]\nknown = ["Joule", "StoryOS", "StoryOps"]\n'


def _tenant(tmp_path, extra=KNOWN):
    from tests.test_config import write_committed, write_local

    tmp_path.mkdir(parents=True, exist_ok=True)
    write_committed(tmp_path, projects=[], extra=extra)
    write_local(tmp_path, {})
    (tmp_path / ".cp-engine").mkdir(exist_ok=True)
    (tmp_path / ".cp-engine" / "paths.json").write_text(json.dumps({"workstreams": {
        "sap-5198-ad-videos": {"path": "1p/sap/sap-5198-ad-videos"},
        "sap-5300-account": {"path": "1p/sap"},
    }}))
    return tmp_path


def _meeting(root, rel_dir, stem, transcript=None, *, project=None, meeting_id=None,
             summary=""):
    d = root / rel_dir / "meetings"
    d.mkdir(parents=True, exist_ok=True)
    if transcript is not None:
        (d / f"{stem}.txt").write_text(transcript)
    if project or meeting_id:
        anchor = ["---"]
        if project:
            anchor.append(f"Project: {project}")
        anchor += ["Provenance: Version 01 | 2026-10-01", f"Filename: {stem}.md",
                   "Author: cp-engine"]
        if meeting_id:
            anchor.append(f"Meeting-ID: {meeting_id}")
        anchor.append("---")
        (d / f"{stem}.md").write_text("\n".join(anchor) + f"\n\n# Title\n\n{summary}\n")


def _cands(text):
    from tests.test_alias_candidates_387 import _cands as cands

    return cands(text, known_terms=("Joule",))


def _scan(root, **kw):
    from cp_engine import alias_candidates as ac
    from cp_engine.config import load

    return ac.scan(load(root), today=TODAY, **kw)


def test_dry_run_plans_lines_with_meeting_metadata_and_writes_nothing(tmp_path):
    from cp_engine import alias_candidates as ac

    root = _tenant(tmp_path)
    _meeting(root, "1p/sap/sap-5198-ad-videos", "2026-10-01-sync",
             "00:00:01 - Drew Fiero\n  The Juul demo is ready. Juul again.\n",
             project="sap-5198-ad-videos", meeting_id="abcdef12-3456",
             summary="Agreed the Juul script.")
    cfg_before = (root / ".cp-engine.toml").read_text()

    result = _scan(root)

    assert result.meetings == 1 and result.workstreams == {"sap-5198-ad-videos"}
    assert len(result.lines) == 1
    line = result.lines[0]
    # Dated by the meeting, not the scan; same shape as an ingest line.
    assert line.startswith('- 2026-10-01 · term · "Juul" → Joule · ×3 · '
                           "sap-5198-ad-videos · meeting abcdef12 · ")
    assert [(g.heard, g.target) for g in result.groups()] == [("Juul", "Joule")]
    assert result.written is False
    assert not (root / ac.FILENAME).exists()
    assert (root / ".cp-engine.toml").read_text() == cfg_before


def test_storyops_is_not_proposed_for_storyos_when_both_are_known(tmp_path):
    text = "00:00:01 - Tony Welch\n  StoryOps runs the process; StoryOS is the product.\n"
    root = _tenant(tmp_path / "both")
    _meeting(root, "1p/sap/sap-5198-ad-videos", "2026-10-01-sync", text)
    assert _scan(root).lines == []

    # Control: with only StoryOS known, the same text IS proposed, so the
    # assertion above is the known list at work and not a dead detector.
    root = _tenant(tmp_path / "one", '[terms]\nknown = ["StoryOS"]\n')
    _meeting(root, "1p/sap/sap-5198-ad-videos", "2026-10-01-sync", text)
    assert [(g.heard, g.target) for g in _scan(root).groups()] == [("StoryOps", "StoryOS")]


def test_write_appends_is_idempotent_and_respects_the_queue(tmp_path):
    from cp_engine import alias_candidates as ac

    root = _tenant(tmp_path)
    # Ingest already queued Juul from meeting m1, and a person rejected Jule.
    for text, meeting in (("Juul.", "m1aaaaaa-1"), ("Jule.", "m0")):
        ac.record(root, _cands(text), code="sap-5198-ad-videos", meeting_id=meeting,
                  today=TODAY)
    ac.review(root, reject=["Jule"], today=TODAY)
    cfg_before = (root / ".cp-engine.toml").read_text()

    _meeting(root, "1p/sap/sap-5198-ad-videos", "2026-10-01-a", "Juul and Jule.",
             project="sap-5198-ad-videos", meeting_id="m1aaaaaa-1")
    _meeting(root, "1p/sap/sap-5198-ad-videos", "2026-10-08-b", "Jool and Jule.",
             project="sap-5198-ad-videos", meeting_id="m2bbbbbb-2")

    result = _scan(root, write=True)
    assert result.written is True
    # m1/Juul was already queued; Jule was rejected; only m2/Jool is new.
    assert [(e.heard, e.meeting) for e in ac.parse_open_lines(result.lines)] == [
        ("Jool", "m2bbbbbb")]
    opened = ac.ReviewFile.read(root).open_entries()
    assert sorted((e.heard, e.meeting) for e in opened) == [
        ("Jool", "m2bbbbbb"), ("Juul", "m1aaaaaa")]
    assert '"Jule" → Joule' in (root / ac.FILENAME).read_text().split("## Rejected")[1]

    # A second scan adds nothing, and config is never touched.
    again = _scan(root, write=True)
    assert again.lines == [] and again.written is False
    assert (root / ".cp-engine.toml").read_text() == cfg_before


def test_workstream_and_meeting_id_without_a_summary_file(tmp_path):
    from cp_engine.alias_candidates import parse_open_lines

    root = _tenant(tmp_path)
    # A transcript with no .md, in a folder below the account that is not a
    # workstream itself: it belongs to the nearest enclosing workstream.
    _meeting(root, "1p/sap/inactive", "2026-09-01-old", "Juul pilot.")
    result = _scan(root)
    [entry] = parse_open_lines(result.lines)
    assert entry.code == "sap-5300-account"
    assert entry.meeting.startswith("f-") and len(entry.meeting) == 8
    assert entry.date == "2026-09-01"
    # Stable across runs, so a later --write dedups against it.
    assert _scan(root).lines == result.lines


def test_the_per_meeting_cap_applies(tmp_path, monkeypatch):
    from cp_engine import alias_candidates as ac

    monkeypatch.setattr(ac, "_MAX_PER_MEETING", 1)
    root = _tenant(tmp_path)
    _meeting(root, "1p/sap/sap-5198-ad-videos", "2026-10-01-a", "Juul. Jool. Jule.")
    _meeting(root, "1p/sap/sap-5198-ad-videos", "2026-10-02-b", "Juul. Jool. Jule.")
    assert len(_scan(root).lines) == 2


def test_phrase_matches_skip_known_terms_and_digit_names():
    """Found by the first real-tenant scan: "Ship Canonic" was proposed as
    SAP Concur, and every "1P Conference" as a mis-hearing of itself."""
    from tests.test_alias_candidates_387 import AHMANN, _find, _Proj

    roster = [_Proj("1P Conference", "First Person"), _Proj("Ad Videos", "SAP Concur"),
              _Proj("SAP 5238 Enablement", "SAP Concur")]
    found = _find("Ship Canonic V1 tight. See you at the 1P Conference. "
                  "The SAP Alignment call.",
                  known_terms=("Canonic",), roster=roster)
    assert "Ship Canonic" not in found and "P Conference" not in found
    # A project name with a number is not a phrase target ("SAP 5238
    # Enablement" read as "SAP Enablement" matched "SAP Alignment").
    assert "SAP Alignment" not in found
    # Still a phrase match when no known term is involved.
    assert _find("Jeff Allman called.", cards=AHMANN) == {
        "Jeff Allman": ("Geoff Ahmann", "name")}


def test_cxp_aliases_scan(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from cp_engine import alias_candidates as ac
    from cp_engine.cli import main
    from cp_engine.cli_cmds import core

    root = _tenant(tmp_path)
    _meeting(root, "1p/sap/sap-5198-ad-videos", "2026-10-01-a", "Juul demo.",
             project="sap-5198-ad-videos", meeting_id="m1aaaaaa")
    monkeypatch.setattr(core, "_alias_scan_roster", lambda config: [])
    monkeypatch.chdir(root)
    runner = CliRunner()

    for args in (["aliases", "--scan"], ["aliases", "--scan", "--dry-run"]):
        out = runner.invoke(main, args)
        assert out.exit_code == 0, out.output
        assert "Scanned 1 meetings in 1 workstreams." in out.output
        assert "Would add 1 lines (1 pairs)" in out.output
        assert '"Juul" → Joule' in out.output
        assert not (root / ac.FILENAME).exists()

    out = runner.invoke(main, ["aliases", "--scan", "--write"])
    assert out.exit_code == 0, out.output
    assert "Added 1 lines" in out.output
    assert [g.heard for g in ac.grouped(root)] == ["Juul"]
    out = runner.invoke(main, ["aliases", "--scan", "--write"])
    assert "No new alias candidates." in out.output

    for bad in (["aliases", "--write"], ["aliases", "--scan", "--write", "--dry-run"],
                ["aliases", "--scan", "--accept", "Juul"]):
        out = runner.invoke(main, bad)
        assert out.exit_code == 2, (bad, out.output)


def test_a_failed_roster_read_still_scans(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from cp_engine.cli import main
    from cp_engine.cli_cmds import core

    root = _tenant(tmp_path)
    _meeting(root, "1p/sap/sap-5198-ad-videos", "2026-10-01-a", "Juul demo.")

    def boom(config):
        raise RuntimeError("offline")

    monkeypatch.setattr(core, "_alias_scan_roster", boom)
    monkeypatch.chdir(root)
    out = CliRunner().invoke(main, ["aliases", "--scan"])
    assert out.exit_code == 0, out.output
    assert "MC-2 roster not read (offline)" in out.output
    assert '"Juul" → Joule' in out.output


def test_git_dir_is_not_scanned(tmp_path):
    root = _tenant(tmp_path)
    d = root / ".git" / "meetings"
    d.mkdir(parents=True)
    (d / "2026-10-01-x.txt").write_text("Juul.")
    assert _scan(root).meetings == 0
