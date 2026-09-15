"""A bare-string `account_summary` must not discard the whole plan.

THE REPORT (2026-09-15). The 1p sprint-planning ingest failed with:

    plan failed validation: plan.account_summary[0] must be a mapping
    (or pass a single mapping for one entry)

The model had returned `account_summary: ["..."]` — a list of strings — where
the validator required a mapping. The prompt shows `account_summary:` followed
by an indented `text:` and calls it "ONE entry", which reads naturally as a
list of one.

WHAT IT COST. The entire plan was rejected: 18 projects of correctly-routed
content thrown away, after a ~55-second model call, because one field had the
wrong wrapper. `_write_account_summary` reads only `text` (company and week are
injected server-side), so the intent of a string was never ambiguous — refusing
it was pedantry with a full regeneration as the price.

Both ends were fixed: the prompt now says "A MAPPING, not a list", and the
validator coerces a bare string. These tests pin the coercion.
"""

from __future__ import annotations

import pytest

from cp_engine.ingest import IngestPlanError, _validate_plan


def _plan(account_summary):
    return {
        "projects": {"ggl-5136": {"add-decision": [{"text": "x", "date": "2026-09-15"}]}},
        "account_summary": account_summary,
    }


class TestTheReportedFailure:
    def test_a_list_of_strings_is_accepted(self):
        """The exact shape that failed on 2026-09-15."""
        plan = _plan(["Maria gave a status across all five GGL projects."])
        _validate_plan(plan)
        assert plan["account_summary"] == [
            {"text": "Maria gave a status across all five GGL projects."}
        ]

    def test_a_bare_string_is_accepted(self):
        """A scalar in, a scalar out — the caller's outer shape is preserved."""
        plan = _plan("One paragraph of gestalt.")
        _validate_plan(plan)
        assert plan["account_summary"] == {"text": "One paragraph of gestalt."}

    def test_the_rest_of_the_plan_survives(self):
        """The point: one bad wrapper must not discard 18 projects of routing."""
        plan = _plan(["summary text"])
        _validate_plan(plan)
        assert plan["projects"]["ggl-5136"]["add-decision"][0]["text"] == "x"


class TestTheDocumentedShapesStillWork:
    def test_a_single_mapping(self):
        """Unchanged — `generate_account_plan` stamps company/week onto a dict."""
        plan = _plan({"text": "the gestalt"})
        _validate_plan(plan)
        assert plan["account_summary"] == {"text": "the gestalt"}

    def test_a_list_of_mappings(self):
        plan = _plan([{"text": "a"}, {"text": "b"}])
        _validate_plan(plan)
        assert plan["account_summary"] == [{"text": "a"}, {"text": "b"}]

    def test_absent_is_fine(self):
        plan = {"projects": {}}
        _validate_plan(plan)
        assert "account_summary" not in plan


class TestWhatIsStillRefused:
    def test_an_empty_string_still_raises(self):
        """Coercion is for a shape mistake, not for missing content."""
        with pytest.raises(IngestPlanError, match="empty string"):
            _validate_plan(_plan(["   "]))

    def test_a_number_still_raises(self):
        with pytest.raises(IngestPlanError, match="must be a mapping or a string"):
            _validate_plan(_plan([42]))


def test_normalization_reaches_the_executor():
    """`_validate_plan` mutates in place, so `execute_plan` sees the dict form.

    It is called on the SAME object inside `execute_plan` (ingest.py:311), so
    a coercion that only lived in a local would be lost at write time — and
    `_write_account_summary` would then get a string and fail on `.get`.
    """
    plan = _plan(["text here"])
    _validate_plan(plan)
    assert isinstance(plan["account_summary"][0], dict)
