"""`cxp rerun-failed-ingests` — the #194 recovery verb.

The risky parts here are not the writes (execute_plan is well covered) but
the SELECTION: which stranded runs get replayed. Replaying too much is not a
neutral mistake — it re-asks the model, producing differently-worded
near-duplicates that content-hash dedupe cannot catch. So the filters are
what these tests pin down.
"""

from __future__ import annotations

import pytest

from cp_engine.cli_cmds.ingest import (
    _normalize_meeting_transcript,
    _week_iso_for,
)


# ──────────────────────────────────────────────────────────────────────
#  _week_iso_for — a replay must file content in the week that OWNS it
# ──────────────────────────────────────────────────────────────────────


def test_week_iso_comes_from_the_meeting_not_today() -> None:
    """A replay runs weeks later; the MEETING date decides the sprint week.

    Without this the recovered bullets land in the current sprint, which
    would be a second data-placement bug on top of the one being fixed.
    """
    assert _week_iso_for("2026-08-10") == "2026-W33"
    assert _week_iso_for("2026-08-18") == "2026-W34"


def test_week_iso_handles_year_boundaries() -> None:
    """ISO weeks are not calendar weeks — Jan 1 can belong to W01 or W53."""
    assert _week_iso_for("2026-01-01") == "2026-W01"
    assert _week_iso_for("2026-12-31") == "2026-W53"


# ──────────────────────────────────────────────────────────────────────
#  _normalize_meeting_transcript — Fathom stores JSONB segments
# ──────────────────────────────────────────────────────────────────────


def test_transcript_segments_flatten_to_speaker_prefixed_lines() -> None:
    got = _normalize_meeting_transcript(
        [{"speaker": "Drew", "text": "hi"}, {"speaker": "Tony", "text": "yo"}]
    )
    assert got == "Drew: hi\nTony: yo"


def test_transcript_accepts_plain_strings_and_bare_segments() -> None:
    assert _normalize_meeting_transcript("already text") == "already text"
    assert _normalize_meeting_transcript(["a", "b"]) == "a\nb"


def test_missing_transcript_is_empty_not_an_exception() -> None:
    """A run whose meeting lost its transcript must skip, not crash the batch."""
    assert _normalize_meeting_transcript(None) == ""


@pytest.mark.parametrize(
    "field",
    [
        [{"speaker": "", "text": "no speaker"}],
        [{"text": "only text"}],
    ],
)
def test_segments_without_a_speaker_still_yield_their_text(field) -> None:
    assert "no speaker" in _normalize_meeting_transcript(field) or (
        "only text" in _normalize_meeting_transcript(field)
    )


# ──────────────────────────────────────────────────────────────────────
#  #220 — recovered runs must not be re-offered
# ──────────────────────────────────────────────────────────────────────
#
# The runs table records what the pipeline ATTEMPTED, never what has since
# been RESTORED, so a recovered run stayed in scope forever. Replaying it is
# NOT the no-op it looks like: a replay re-asks the model, so the wording
# differs, the hash differs, and content-hash dedupe cannot catch the
# duplicate. These pin the selection default and its override.

from unittest.mock import MagicMock  # noqa: E402

from click.testing import CliRunner  # noqa: E402

from cp_engine.cli_cmds.ingest import rerun_failed_ingests_cmd  # noqa: E402

_STRANDED_UNRECOVERED = {
    "id": "run-open",
    "created_at": "2026-08-20T00:00:00+00:00",
    "status": "success",
    "errors": ["slt-5195: sprint file missing"],
    "project_codes": ["slt-5195"],
    "meeting_id": "m-open",
    "plan_summary": {"slt-5195": {"asks": 3}},
    "recovered_at": None,
    "recovered_by": None,
}
_STRANDED_RECOVERED = {
    "id": "run-done",
    "created_at": "2026-08-20T00:00:00+00:00",
    "status": "success",
    "errors": ["ibx-5192: sprint file missing"],
    "project_codes": ["ibx-5192"],
    "meeting_id": "m-done",
    "plan_summary": {"ibx-5192": {"asks": 9}},
    "recovered_at": "2026-08-23T00:00:00+00:00",
    "recovered_by": "replay",
}
_MEETINGS = [
    {"id": "m-open", "title": "Still owed", "meeting_date": "2026-08-20T10:00:00+00:00",
     "transcript": "x", "action_items": []},
    {"id": "m-done", "title": "Already recovered", "meeting_date": "2026-08-20T10:00:00+00:00",
     "transcript": "x", "action_items": []},
]


def _client(rows):
    client = MagicMock()

    def table(name):
        t = MagicMock()
        resp = MagicMock()
        resp.data = rows if name == "auto_ingest_runs" else _MEETINGS
        # both the plain and the .in_()-filtered read land on execute()
        t.select.return_value.order.return_value.execute.return_value = resp
        t.select.return_value.in_.return_value.execute.return_value = resp
        return t

    client.table.side_effect = table
    return client


def _run(monkeypatch, rows, *args):
    import cp_engine.cli_cmds.ingest as mod

    monkeypatch.setattr(mod._cli, "_load_config_or_die", lambda: MagicMock(root="/tmp"))
    monkeypatch.setattr("cp_engine.mc2_db.get_client", lambda config=None: _client(rows))
    return CliRunner().invoke(rerun_failed_ingests_cmd, ["--dry-run", *args])


def test_recovered_runs_are_excluded_by_default(monkeypatch) -> None:
    """The whole point of #220: a stamped run stops being offered."""
    res = _run(monkeypatch, [_STRANDED_UNRECOVERED, _STRANDED_RECOVERED])
    assert res.exit_code == 0, res.output
    assert "slt-5195" in res.output, "the unrecovered run must still be offered"
    assert "ibx-5192" not in res.output, "a recovered run must not be re-offered"
    assert "Already recovered:             1 (excluded)" in res.output


def test_include_recovered_overrides_the_exclusion(monkeypatch) -> None:
    """The override exists, and it says out loud that it is overriding."""
    res = _run(monkeypatch, [_STRANDED_UNRECOVERED, _STRANDED_RECOVERED],
               "--include-recovered")
    assert res.exit_code == 0, res.output
    assert "ibx-5192" in res.output
    assert "INCLUDED (override)" in res.output


def test_all_recovered_reports_nothing_to_do_rather_than_an_empty_list(
    monkeypatch,
) -> None:
    """'Nothing to replay' must be distinguishable from 'no stranded runs'.

    Same lesson as #218/#219: two different states that look alike are how
    these bugs hide. The message names the override.
    """
    res = _run(monkeypatch, [_STRANDED_RECOVERED])
    assert res.exit_code == 0, res.output
    assert "already marked recovered" in res.output
    assert "--include-recovered" in res.output
