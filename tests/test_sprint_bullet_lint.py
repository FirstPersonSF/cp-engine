"""The lint that would have caught #272 weeks earlier.

#272: `_parse_decisions` required a bare `[decision` marker, so backticked
bullets — the form auto-ingest and hand-written entries actually use — were
silently dropped. 17 across 4 files; `ibx-5153`'s W39 sprint file parsed to
ZERO decisions while visibly containing two.

Nothing surfaced it. The bullets render correctly in Markdown, so a human
reading the file sees them; every consumer of the parser read a truncated
record without any signal that it was truncated. It was found only because an
unrelated feature (#260's Last-activity column) happened to read the same
parser.

THE CONTROL THAT MATTERS is `test_fires_on_the_exact_272_shape`: it feeds the
lint a file whose bullets the parser refuses and requires a warning. Run
against a parser that accepts them, it must go quiet — which is what
`test_silent_when_everything_parses` asserts from the other side.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cp_engine.sprint_bullet_lint import unparsed_bullet_warnings

_FRONTMATTER = (
    "---\n"
    "Project: {code} — Test\n"
    "Filename: sprints/{week}/{code}.md\n"
    "Sprint: {week}\n"
    "PriorSprint: \n"
    "---\n\n"
    "# {code} · Sprint {week}\n\n"
)


def _sprint(root: Path, week: str, code: str, decisions: str) -> Path:
    d = root / "sprints" / week
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{code}.md"
    p.write_text(
        _FRONTMATTER.format(code=code, week=week)
        + "## Meeting notes & decisions\n\n### Decisions\n\n"
        + decisions
        + "\n"
    )
    return p


def test_silent_when_everything_parses(tmp_path):
    _sprint(
        tmp_path,
        "2026-W38",
        "ibx-5153",
        "- `[decision · 2026-09-14]` The parser accepts this today.",
    )
    assert unparsed_bullet_warnings(tmp_path) == []


def test_fires_on_the_exact_272_shape(tmp_path):
    """A dated bullet in a section the parser does not read.

    Same shape as #272: the line is unmistakably a dated bullet, renders fine,
    and yields no entry. The lint's whole job is to notice that gap without
    knowing WHY the parser refused.
    """
    d = tmp_path / "sprints" / "2026-W38"
    d.mkdir(parents=True)
    (d / "ibx-5153.md").write_text(
        _FRONTMATTER.format(code="ibx-5153", week="2026-W38")
        + "## Discussion notes\n\n"
        + "- `[decision · 2026-09-15]` Written under the wrong heading.\n"
    )
    warnings = unparsed_bullet_warnings(tmp_path)
    assert len(warnings) == 1
    assert "1 dated bullet(s)" in warnings[0]
    assert "2026-W38/ibx-5153.md" in warnings[0]


def test_quotes_the_first_offender(tmp_path):
    """A false positive must be obviously dismissible, not mysterious."""
    d = tmp_path / "sprints" / "2026-W38"
    d.mkdir(parents=True)
    (d / "x.md").write_text(
        _FRONTMATTER.format(code="x", week="2026-W38")
        + "## Discussion notes\n\n"
        + "- `[decision · 2026-09-15]` The quoted one.\n"
        + "- `[decision · 2026-09-16]` The second one.\n"
    )
    (warning,) = unparsed_bullet_warnings(tmp_path)
    assert "The quoted one." in warning
    assert "2 dated bullet(s)" in warning


def test_one_line_per_file_not_per_bullet(tmp_path):
    """A format change breaks every bullet at once; 200 lines is a wall."""
    for code in ("a", "b", "c"):
        d = tmp_path / "sprints" / "2026-W38"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{code}.md").write_text(
            _FRONTMATTER.format(code=code, week="2026-W38")
            + "## Discussion notes\n\n"
            + "".join(
                f"- `[decision · 2026-09-1{n}]` Entry {n}.\n" for n in range(5)
            )
        )
    warnings = unparsed_bullet_warnings(tmp_path)
    assert len(warnings) == 3  # three files, not fifteen bullets


def test_prose_mentioning_a_marker_is_not_flagged(tmp_path):
    """The marker must START the line. A reference inside a sentence is prose.

    A false positive trains the reader to ignore the warning — which is how
    #272 survived in the first place.
    """
    d = tmp_path / "sprints" / "2026-W38"
    d.mkdir(parents=True)
    (d / "x.md").write_text(
        _FRONTMATTER.format(code="x", week="2026-W38")
        + "## Discussion notes\n\n"
        + "- See the `[decision · 2026-09-14]` recorded above for context.\n"
        + "Prose that cites [decision · 2026-09-14] mid-sentence.\n"
    )
    assert unparsed_bullet_warnings(tmp_path) == []


def test_undated_bullets_are_not_flagged(tmp_path):
    """Only DATED bullets are in scope; freeform ones are legitimate."""
    d = tmp_path / "sprints" / "2026-W38"
    d.mkdir(parents=True)
    (d / "x.md").write_text(
        _FRONTMATTER.format(code="x", week="2026-W38")
        + "## Discussion notes\n\n- A plain observation with no marker.\n"
    )
    assert unparsed_bullet_warnings(tmp_path) == []


def test_planning_and_log_files_are_skipped(tmp_path):
    """`_planning.md` is not a project sprint file and carries other shapes."""
    d = tmp_path / "sprints" / "2026-W38"
    d.mkdir(parents=True)
    (d / "_planning.md").write_text(
        "# Planning\n\n- `[decision · 2026-09-15]` Not a project file.\n"
    )
    assert unparsed_bullet_warnings(tmp_path) == []


def test_no_sprints_directory_is_not_an_error(tmp_path):
    assert unparsed_bullet_warnings(tmp_path) == []


def test_a_malformed_file_does_not_double_warn(tmp_path):
    """`sync` already reports an unparseable file; this must stay quiet."""
    d = tmp_path / "sprints" / "2026-W38"
    d.mkdir(parents=True)
    (d / "x.md").write_text("- `[decision · 2026-09-15]` No frontmatter at all.\n")
    assert unparsed_bullet_warnings(tmp_path) == []


def test_team_communication_heading_is_parsed(tmp_path):
    """Initiatives head this section `## Team communication` (#273).

    The engine's own `initiative-sprint.md.j2` writes that heading while the
    parser read only `## Client communication`, so every open ask in 135
    tenant files was silently unparsed. Found by this lint on its first run
    against real data — which is the argument for the lint.
    """
    from cp_engine.sprints import parse_sprint_file

    d = tmp_path / "sprints" / "2026-W20"
    d.mkdir(parents=True)
    p = d / "mission-control.md"
    p.write_text(
        _FRONTMATTER.format(code="mission-control", week="2026-W20")
        + "## Team communication\n\n### Open asks\n\n"
        + "- [open · 2026-05-15 · tony] Tony to explain the workflow.\n"
    )
    assert len(parse_sprint_file(p).client_open_asks) == 1
    assert unparsed_bullet_warnings(tmp_path) == []


def test_carry_forward_bullets_are_not_false_positives(tmp_path):
    """`[ask · …]` and `[risk · severity · category · date]` parse elsewhere.

    The carry-forward region has its own parser, its own container and its own
    date field names (`asked_date`, `raised_date`). Counting only the
    list-shaped sections reported all of them as unparsed — 65 false positives
    tenant-wide across two rounds of this lint.
    """
    d = tmp_path / "sprints" / "2026-W22"
    d.mkdir(parents=True)
    (d / "storyos.md").write_text(
        _FRONTMATTER.format(code="storyos", week="2026-W22")
        + "<!-- cp-engine:start carry-forward -->\n"
        + "## Carried over from 2026-W21\n"
        + "- [ask · 2026-05-14 · Tony] Send the revision list.\n"
        + "- [risk · watching · scope · 2026-05-15] Accreting scope.\n"
        + "<!-- cp-engine:end carry-forward -->\n"
    )
    assert unparsed_bullet_warnings(tmp_path) == []


def test_a_sprint_week_target_is_a_date(tmp_path):
    """#280. Horizon items are dated `by W39` — the file's own vocabulary.

    An ISO-only test reported every one of them as unparsed, which is a false
    positive on healthy data. Seven files flagged tenant-wide for this.
    """
    d = tmp_path / "sprints" / "2026-W39"
    d.mkdir(parents=True)
    (d / "x.md").write_text(
        _FRONTMATTER.format(code="x", week="2026-W39")
        + "<!-- cp-engine:start carry-forward -->\n"
        + "## Carried over from 2026-W38\n"
        + "- [decision · by W39] Reopen The Truth, or leave it retired?\n"
        + "<!-- cp-engine:end carry-forward -->\n"
    )
    assert unparsed_bullet_warnings(tmp_path) == []


def test_an_entry_with_an_empty_date_field_still_counts(tmp_path):
    """#280, the other half. A horizon `opportunity` carries no date at all.

    Requiring a filled date field reported it as unparsed — but the lint asks
    whether the bullet PARSED, not whether someone dated it. A parsed entry
    with text counts either way.

    Asserted at the predicate, because the end-to-end path cannot produce an
    undated entry without a fixture that also trips other rules.
    """
    from cp_engine.sprint_bullet_lint import _has_date

    class Undated:
        target_date = None
        text = "The 100-line wall is a reusable asset beyond the pitch."

    class Empty:
        target_date = None
        text = ""

    assert _has_date(Undated()), "a parsed entry with text is parsed"
    assert not _has_date(Empty()), "no date and no text is not evidence"


# ---------------------------------------------------------------------------
# #369 — "Sprint parser drops [risk · escalated · …] bullets"
# ---------------------------------------------------------------------------

# The exact bullet `cxp render --check` quoted for sap-5198 W43/W44/W45.
_ISSUE_369_RISK = (
    "- [risk · escalated · schedule · 2026-10-16] Brief arrived later than "
    "typical production cycle for a Jan 2027 in-market launch. Tara "
    "acknowledged this; mid-December production wrap is tight and final "
    "delivery is likely to slip into January, compressing any contingency. "
    "[attribution unverified] <!-- cp:hash=58c4141f -->"
)


def _sap_5198_w43(root: Path) -> Path:
    """The shape of sap-5198's W43 file that the lint misreported: a carried
    escalated risk first, then carried open questions, open asks and two
    `### Inbound` entries."""
    d = root / "sprints" / "2026-W43"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "sap-5198-2027-ad-videos.md"
    p.write_text(
        _FRONTMATTER.format(code="sap-5198-2027-ad-videos", week="2026-W43")
        + "<!-- cp-engine:start carry-forward -->\n"
        + "## Carried over from 2026-W42\n"
        + _ISSUE_369_RISK + "\n"
        + "- [risk · watching · scope · 2026-10-16] Positioning still open. "
          "<!-- cp:hash=06fb694f -->\n"
        + "- [open question · 2026-10-16] Final creative concept is undefined. "
          "<!-- cp:hash=1fc4a7d9 -->\n"
        + "- [open question · 2026-10-16] SMB vs enterprise balance. "
          "<!-- cp:hash=8b283ac9 -->\n"
        + "- [open question · 2026-10-16] Exact production timeline. "
          "<!-- cp:hash=13643303 -->\n"
        + "<!-- cp-engine:end carry-forward -->\n\n"
        + "## Client communication\n\n### Open asks\n\n"
        + "- [open · 2026-10-16 · Tara Haney] Send the updated library.\n\n"
        + "### Inbound\n\n"
        + "- [2026-10-16 · source ingest] New source ingested.\n"
        + "- [2026-10-19 · Marcello (internal)] SAP awarded the campaign.\n\n"
        + "## Dependencies & risks\n\n"
        + "## Meeting notes & decisions\n\n### Decisions\n\n### Open questions\n"
    )
    return p


def test_369_the_escalated_risk_parses(tmp_path):
    """The bullet the warning quoted was never the problem: it parses."""
    from cp_engine.sprints import parse_sprint_file

    sf = parse_sprint_file(_sap_5198_w43(tmp_path))
    (risk,) = [r for r in sf.carry_forward.risks if r.severity == "escalated"]
    assert risk.category == "schedule"
    assert risk.raised_date == "2026-10-16"
    assert risk.text.startswith("Brief arrived later than typical production")


def test_369_sap_5198_shape_is_not_flagged(tmp_path):
    """The real gap was open questions the lint never counted (masked in part
    by inbound entries it counted but never asked about). With each marker
    kind settled against its own entries the file is clean.

    CONTROL: against the pre-fix lint this file reports "1 dated bullet(s)
    … First: - [risk · escalated · schedule · 2026-10-16]" — the #369 text.
    """
    _sap_5198_w43(tmp_path)
    assert unparsed_bullet_warnings(tmp_path) == []


def test_369_open_questions_in_their_section_are_counted(tmp_path):
    d = tmp_path / "sprints" / "2026-W45"
    d.mkdir(parents=True)
    (d / "x.md").write_text(
        _FRONTMATTER.format(code="x", week="2026-W45")
        + "## Meeting notes & decisions\n\n### Decisions\n\n### Open questions\n\n"
        + "- [open question · 2026-10-29] Which creative partner leads?\n"
    )
    assert unparsed_bullet_warnings(tmp_path) == []


def test_369_a_surplus_in_one_kind_cannot_hide_a_loss_in_another(tmp_path):
    """Two inbound entries used to cancel two dropped decisions."""
    d = tmp_path / "sprints" / "2026-W45"
    d.mkdir(parents=True)
    (d / "x.md").write_text(
        _FRONTMATTER.format(code="x", week="2026-W45")
        + "## Client communication\n\n### Inbound\n\n"
        + "- [2026-10-27 · Tara] One.\n- [2026-10-28 · Tara] Two.\n\n"
        + "## Discussion notes\n\n"
        + "- `[decision · 2026-10-28]` Lost under the wrong heading.\n"
    )
    (warning,) = unparsed_bullet_warnings(tmp_path)
    assert "1 dated bullet(s)" in warning
    assert "Lost under the wrong heading." in warning


def test_369_quotes_the_line_that_was_lost_not_the_first_bullet(tmp_path):
    """The quote is the bug report. Quoting a healthy line sent #369 after a
    risk-grammar defect that did not exist."""
    d = tmp_path / "sprints" / "2026-W45"
    d.mkdir(parents=True)
    (d / "x.md").write_text(
        _FRONTMATTER.format(code="x", week="2026-W45")
        + "## Meeting notes & decisions\n\n### Decisions\n\n"
        + "- [decision · 2026-10-27] This one parses.\n\n"
        + "## Discussion notes\n\n"
        + "- [decision · 2026-10-28] This one is lost.\n"
    )
    (warning,) = unparsed_bullet_warnings(tmp_path)
    assert "This one is lost." in warning
    assert "This one parses." not in warning



@pytest.mark.parametrize("severity", ["escalated", "watching", "dependency", "resolved"])
@pytest.mark.parametrize("prefix", ["risk · ", ""])
def test_369_live_section_reads_every_emitted_severity(tmp_path, severity, prefix):
    """`## Dependencies & risks` takes both bracket shapes.

    `record-risk` writes `[<sev> · <cat> · <date>]`; carry-forward writes
    `[risk · <sev> · <cat> · <date>]`, and that shape gets copied back into the
    live section. The section parser read the `risk` token as the severity,
    shifting every field: severity `risk`, category `escalated`, raised date
    `schedule` — never open, never carried. The severities are every one the
    engine emits: escalated/watching (ingest), dependency (state.Risk),
    resolved (resolve-risk).
    """
    from cp_engine.prep_planning import _parse_risks_from_body
    from cp_engine.sprints import parse_sprint_file

    d = tmp_path / "sprints" / "2026-W45"
    d.mkdir(parents=True)
    p = d / "x.md"
    body = (
        _FRONTMATTER.format(code="x", week="2026-W45")
        + "## Dependencies & risks\n\n"
        + f"- [{prefix}{severity} · schedule · 2026-10-16] Brief arrived late.\n"
    )
    p.write_text(body)
    (risk,) = parse_sprint_file(p).risks
    assert (risk.severity, risk.category, risk.raised_date) == (
        severity, "schedule", "2026-10-16",
    )
    (prep_risk,) = _parse_risks_from_body(body)
    assert prep_risk["severity"] == severity
    assert unparsed_bullet_warnings(tmp_path) == []


def test_369_exact_bullet_in_the_live_section_is_an_active_risk(tmp_path):
    from cp_engine.sprints import _active_risks, parse_sprint_file

    d = tmp_path / "sprints" / "2026-W45"
    d.mkdir(parents=True)
    p = d / "x.md"
    p.write_text(
        _FRONTMATTER.format(code="x", week="2026-W45")
        + "## Dependencies & risks\n\n" + _ISSUE_369_RISK + "\n"
    )
    (risk,) = _active_risks(parse_sprint_file(p))
    assert risk.severity == "escalated"
    assert risk.raised_date == "2026-10-16"
