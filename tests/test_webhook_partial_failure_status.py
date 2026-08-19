"""A run that commits a transcript but drops a project's plan is a FAILURE.

The commit-and-push branch hardcoded `status="success"`. Something always
commits there — the transcript and meeting-history writes happen regardless of
whether any per-project plan landed — so that branch fired on every run and
stamped success over real per-project errors.

Result: 117 of the 123 runs carrying a "sprint file missing" error were
recorded as `success` between 2026-05-14 and 2026-08-19. That is why 1,375
discarded bullets went unnoticed for three months: git history looked normal,
the runs table looked clean, and the only trace was an `errors` array nobody
reads.
"""

import sys
from pathlib import Path

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

from pipeline import _status_from_ingested  # noqa: E402


def test_partial_failure_is_not_success():
    """The incident shape: one project errored, files still got written."""
    ingested = [
        {"code": "storyos", "errors": [], "files_written": ["a.md"]},
        {
            "code": "slt-5196",
            "errors": [
                "slt-5196: sprint file missing for slt-5196 (week 2026-W34) "
                "and no prior sprint file to scaffold from: …"
            ],
            "files_written": [],
        },
    ]
    assert _status_from_ingested(ingested, anything_wrote=True) == "failed"


def test_clean_run_is_still_success():
    ingested = [{"code": "storyos", "errors": [], "files_written": ["a.md"]}]
    assert _status_from_ingested(ingested, anything_wrote=True) == "success"


def test_nothing_wrote_nothing_errored_is_a_no_op():
    ingested = [{"code": "storyos", "errors": [], "files_written": []}]
    assert _status_from_ingested(ingested, anything_wrote=False) == "skipped_no_op"


def test_errors_outrank_no_op():
    """An error with nothing written is a failure, not a quiet skip."""
    ingested = [{"code": "slt-5196", "errors": ["boom"], "files_written": []}]
    assert _status_from_ingested(ingested, anything_wrote=False) == "failed"
