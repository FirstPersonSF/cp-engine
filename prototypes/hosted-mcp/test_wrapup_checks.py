"""The wrap-up checks, reachable from a hosted session (#280).

WHY THESE VERBS EXIST. A hosted session had `capture_project_state` — the verb
that WRITES an Exec Summary — and none of the checks that say whether the write
was any good. `spine-lint`, `commitments-sweep` and `seal-sweep` were CLI-only,
so a teammate working through cp-hosted could refresh a summary and had no way
to ask whether the project's spine was sound, what was owed, or what needed
sealing.

Measured on ggl-5136, 2026-09-17: its Exec Summary carried a current `Status`
and a 09-15 stamp while `Next up` listed three deadlines from July and named a
collaborator the Status line said had left. A stamp-only refresh is worse than
none, because the staleness check reads the stamp.

THE CONSTRAINT WAS NEVER TECHNICAL. `spine_lint`, `seal_sweep` and
`commitments_sweep` contain ZERO `Path`/`read_text`/`open()` references between
them — they are pure functions over rows. Only the ASSEMBLY (which tables,
which columns, the one-live-per-element discipline) lived in the CLI command
bodies. `run_all_lints` was extracted so both surfaces share it rather than
drifting, which is the #172/#178 lesson.

These tests assert the contract at the seam — serialization shape and the
shared-assembly guarantee — rather than booting a server.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture
def server(monkeypatch):
    """The module, with the env it refuses to import without.

    Same shape as `test_health.py`: the server exits at import when its
    credentials are absent, which is correct for a deployed service and means
    every test that touches it needs stub values.
    """
    for k, v in {
        "MC2_API_BASE": "http://example.invalid",
        "SUPABASE_URL": "http://example.invalid",
        "SUPABASE_ANON_KEY": "x",
    }.items():
        monkeypatch.setenv(k, v)
    import server as mod

    return mod


# ──────────────────────────────────────────────────────────────────────
#  The shared assembly — one implementation, two callers
# ──────────────────────────────────────────────────────────────────────


def test_run_all_lints_is_the_single_assembly():
    """THE POINT OF #280. The CLI must not carry its own copy.

    Two copies of "which tables, which columns, which checks" drift — that is
    what #172 (`Output` vs `Deliverables`) and #178 cost. The CLI command body
    should call `run_all_lints`, not rebuild the query.
    """
    from pathlib import Path

    cli = Path(__file__).resolve().parents[2] / "src/cp_engine/cli_cmds/spine.py"
    src = cli.read_text(encoding="utf-8")
    assert "run_all_lints(client, codes" in src, (
        "the CLI should delegate to the shared assembly"
    )
    # The old inline query must be gone, or it is still a second copy.
    assert src.count("SPINE_LINT_COLUMNS") == 0, (
        "the CLI still builds its own spine-lint query"
    )


def test_the_lint_functions_touch_no_filesystem():
    """The premise these verbs rest on, asserted rather than assumed.

    If a check ever grows a `Path` or an `open()`, it stops being hostable and
    this package quietly becomes CLI-only again — with no test to notice.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src/cp_engine"
    for module in ("spine_lint.py", "seal_sweep.py", "commitments_sweep.py"):
        src = (root / module).read_text(encoding="utf-8")
        # `run_all_lints` takes cp.md TEXT, never a path — that is the seam.
        for banned in ("read_text(", "open(", "Path("):
            assert banned not in src, (
                f"{module} gained {banned!r} — it is no longer a pure "
                "row-level function, and the hosted verb wrapping it will "
                "fail or silently degrade"
            )


# ──────────────────────────────────────────────────────────────────────
#  Serialization — dataclasses out, JSON in
# ──────────────────────────────────────────────────────────────────────


@dataclass
class _Row:
    id: str = "c1"
    description: str = "Send the estimate"
    owner: str = "Drew"
    source_kind: str = "meeting_ingest"
    due_date: date | None = None
    date_status: str = "undated"
    age_days: int = 13
    ttl: str | None = "warn"


def test_a_commitment_row_serializes_its_expiry_clock(server):
    """`ttl` and `undated` are the fields the wrap-up ritual acts on.

    An UNDATED commitment expires at 14 days. A serializer that dropped `ttl`
    would turn "expires tomorrow" into an ordinary open row.
    """
    out = server._sweep_row_dict(_Row())
    assert out["undated"] is True
    assert out["ttl"] == "warn"
    assert out["age_days"] == 13
    assert out["due_date"] is None


def test_a_dated_commitment_serializes_an_iso_string(server):
    """JSON has no date type; the caller must still be able to sort."""
    out = server._sweep_row_dict(_Row(due_date=date(2026, 9, 17), date_status="due"))
    assert out["due_date"] == "2026-09-17"
    assert out["undated"] is False


@dataclass
class _Cand:
    est_item_id: str = "_authored/brief"
    framing: str = "Inputs & Briefing"
    layer: str = "Brief"
    kinds: list | None = None
    via: str = ""

    def __post_init__(self):
        if self.kinds is None:
            self.kinds = ["reference"]


@dataclass
class _Round:
    est_item_id: str = "_authored/deck"
    framing: str = "The deck"
    version_label: str = "v3"
    version_date: date | None = date(2026, 9, 1)
    candidates: list | None = None
    already_absorbed: int = 2

    def __post_init__(self):
        if self.candidates is None:
            self.candidates = [_Cand(), _Cand(via="_authored/workshop")]


def test_a_seal_round_preserves_via_per_candidate(server):
    """`via` is the difference between direct and indirect evidence.

    An indirect candidate reached the deliverable THROUGH an activity — real,
    because that is how routing works, but weaker: the human bound the source
    to an activity, not to this deliverable. A caller deciding what to seal has
    to see which is which, or it asserts provenance that did not happen.
    """
    out = server._round_dict(_Round())
    assert out["version_date"] == "2026-09-01"
    assert out["already_absorbed"] == 2
    vias = [c["via"] for c in out["candidates"]]
    assert "" in vias and "_authored/workshop" in vias


def test_a_round_with_no_candidates_is_not_an_error(server):
    """A shipped deliverable that consumed nothing traceable is a real state —
    it reports an empty list, not a failure."""
    out = server._round_dict(_Round(candidates=[], version_date=None))
    assert out["candidates"] == []
    assert out["version_date"] is None


# ──────────────────────────────────────────────────────────────────────
#  Tier 2 — word-count REPORTING, not rotation
# ──────────────────────────────────────────────────────────────────────


def test_word_count_matches_the_cli_on_the_same_text():
    """The hosted verb must not become a second implementation.

    Both surfaces call `lint_word_count(text, label)`; the only difference is
    where the text came from. Measured on the live ibx-5153 cp.md, 2026-09-17:
    3,242 words, both paths.
    """
    from cp_engine.word_count_lint import lint_word_count

    text = "word " * 2600
    findings = lint_word_count(text, "x/cp.md")
    assert findings, "2,600 words should trip the 2,500 audit threshold"
    assert "2,600 words" in findings[0]


def test_word_count_is_silent_under_the_threshold():
    """A clean file produces nothing — the discipline is warn-only and a
    warning on every project would be noise, not signal."""
    from cp_engine.word_count_lint import lint_word_count

    assert lint_word_count("word " * 100, "x/cp.md") == []


def test_the_hosted_verb_reports_and_does_not_rotate(server):
    """THE BOUNDARY. Rotation is a WRITE — two files in one commit — and this
    server holds no write access by construction.

    A verb that quietly rotated would be the write-deploy-key shortcut arriving
    through the back door, which #280 argues against on attribution grounds.
    """
    import inspect

    src = inspect.getsource(server.word_count_check)
    assert "REPORTING ONLY" in src
    for banned in ("write_text", "_commit_with_message", "rotate"):
        assert banned not in src, (
            f"word_count_check gained {banned!r} — it must report, not write"
        )
