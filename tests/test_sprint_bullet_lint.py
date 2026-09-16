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
